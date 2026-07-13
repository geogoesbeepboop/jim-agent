"""Typed convenience wrappers over the raw A2A ``Store`` methods + ``A2ACrypto``.

Later payment/monitor/push stages call these so they never touch ciphertext or
the raw ``save_a2a_*`` / ``*_ciphertext`` plumbing directly — encryption on write
and decryption on read happen here, in one place, keeping the invariant obvious:

- ``PaymentAuths``: verify-then-settle-once. The signed PaymentPayload is only
  ever persisted encrypted; ``cas`` is the exactly-once settlement transition.
- ``WithheldArtifacts``: pre-payment memo content exists ONLY here, encrypted —
  ``peek_meta`` is the most any unpaid surface may see (never decrypts).
- ``PushConfigs``: URL + token + auth encrypted together as one blob.
- ``DeadLetters``: append-only push failure audit, no event body ever stored.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from jim.a2a.crypto import A2ACrypto
    from jim.store.repo import Store


@dataclass
class AuthView:
    """A decrypted view of one payment-authorization row. ``payload`` is the
    decrypted signed PaymentPayload (``None`` until a payer submits one)."""

    task_id: str
    kind: str
    product: str
    identifier: str
    mode: str
    amount_usd: float
    requirements: dict
    payload: dict | None
    payer: str | None
    status: str
    expires_at: datetime | None
    tx_hash: str | None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class PaymentAuths:
    """Payment authorizations: verify-then-settle-once (ADR-0008 extended to A2A).

    The signed PaymentPayload is persisted ONLY as ciphertext — plaintext never
    touches the row. ``cas`` is the exactly-once settlement guard: exactly one
    caller wins a status transition, so a task settles at most once."""

    def __init__(self, store: Store, crypto: A2ACrypto) -> None:
        self._store = store
        self._crypto = crypto

    async def create_required(
        self,
        *,
        task_id: str,
        kind: str,
        product: str,
        identifier: str,
        mode: str,
        amount_usd: float,
        requirements: dict,
        expires_at: datetime | None = None,
    ) -> None:
        """Persist a fresh ``required`` auth carrying the advertised requirements
        (the price-swap defense checkpoint). No payload/payer yet."""
        await self._store.save_a2a_auth(
            task_id=task_id,
            kind=kind,
            product=product,
            identifier=identifier,
            mode=mode,
            amount_usd=amount_usd,
            requirements=requirements,
            payload_ciphertext=None,
            payer=None,
            status="required",
            expires_at=expires_at,
        )

    async def attach_payload(self, task_id: str, payload: dict, payer: str | None) -> None:
        """Encrypt + persist the submitted signed PaymentPayload and its payer.
        Status is left to the caller to advance via ``cas``/``mark``."""
        ct = self._crypto.encrypt_json(payload)
        await self._store.update_a2a_auth(task_id, payload_ciphertext=ct, payer=payer)

    async def get(self, task_id: str) -> AuthView | None:
        row = await self._store.get_a2a_auth(task_id)
        return self._view(row) if row is not None else None

    async def cas(self, task_id: str, from_status: str, to_status: str) -> bool:
        """Atomic status transition; True only for the single winning caller."""
        return await self._store.cas_a2a_auth_status(task_id, from_status, to_status)

    async def mark(self, task_id: str, **fields) -> None:
        """Partial update (``status=``, ``tx_hash=``, ``payer=``, ``payload_ciphertext=``)."""
        await self._store.update_a2a_auth(task_id, **fields)

    async def sweep(self, status: str) -> list[AuthView]:
        """Restart-recovery sweep: all auths in ``status``, payloads decrypted."""
        rows = await self._store.list_a2a_auths(status=status)
        return [self._view(r) for r in rows]

    def _view(self, row: dict) -> AuthView:
        ct = row.get("payload_ciphertext")
        payload = self._crypto.decrypt_json(ct) if ct else None
        return AuthView(
            task_id=row["task_id"],
            kind=row["kind"],
            product=row["product"],
            identifier=row["identifier"],
            mode=row["mode"],
            amount_usd=row["amount_usd"],
            requirements=row["requirements"],
            payload=payload,
            payer=row["payer"],
            status=row["status"],
            expires_at=row["expires_at"],
            tx_hash=row["tx_hash"],
            created_at=row.get("created_at"),
            updated_at=row.get("updated_at"),
        )


class WithheldArtifacts:
    """Pre-payment memo content exists ONLY here, encrypted — ``peek_meta`` is the
    most any unpaid surface may see (severity/as_of/price/monitor_id, never the
    memo). ``release`` is the paid unlock: decrypt, return, then delete."""

    def __init__(self, store: Store, crypto: A2ACrypto) -> None:
        self._store = store
        self._crypto = crypto

    async def hold(
        self,
        task_id: str,
        monitor_id: str,
        severity: str,
        as_of: str | None,
        price_usd: float,
        payload: dict,
    ) -> None:
        ct = self._crypto.encrypt_json(payload)
        await self._store.save_withheld(
            task_id=task_id,
            monitor_id=monitor_id,
            severity=severity,
            as_of=as_of,
            price_usd=price_usd,
            payload_ciphertext=ct,
        )

    async def peek_meta(self, task_id: str) -> dict | None:
        """Metadata ONLY — deliberately never decrypts the memo payload."""
        row = await self._store.get_withheld(task_id)
        if row is None:
            return None
        return {
            "task_id": row["task_id"],
            "monitor_id": row["monitor_id"],
            "severity": row["severity"],
            "as_of": row["as_of"],
            "price_usd": row["price_usd"],
        }

    async def release(self, task_id: str) -> dict | None:
        """Decrypt + return the memo payload, then delete it (paid, one-shot)."""
        row = await self._store.get_withheld(task_id)
        if row is None:
            return None
        payload = self._crypto.decrypt_json(row["payload_ciphertext"])
        await self._store.delete_withheld(task_id)
        return payload

    async def discard(self, task_id: str) -> None:
        """Drop a withheld artifact unpaid (idempotent)."""
        await self._store.delete_withheld(task_id)


class PushConfigs:
    """Push-notification configs stored as one encrypted blob (URL+token+auth)."""

    def __init__(self, store: Store, crypto: A2ACrypto) -> None:
        self._store = store
        self._crypto = crypto

    async def save(self, task_id: str, config_id: str, config: dict) -> None:
        ct = self._crypto.encrypt_json(config)
        await self._store.save_a2a_push_config(
            task_id=task_id, config_id=config_id, config_ciphertext=ct
        )

    async def list(self, task_id: str) -> list[tuple[str, dict]]:
        rows = await self._store.get_a2a_push_configs(task_id)
        return [(r["config_id"], self._crypto.decrypt_json(r["config_ciphertext"])) for r in rows]

    async def delete(self, task_id: str, config_id: str) -> None:
        await self._store.delete_a2a_push_config(task_id, config_id)


class DeadLetters:
    """Append-only push dead-letter audit. Records that a delivery failed after N
    attempts — never the event body (a dead letter must not become a second copy
    of a paid artifact). No crypto needed: there is nothing secret to store."""

    def __init__(self, store: Store) -> None:
        self._store = store

    async def record(
        self,
        *,
        task_id: str,
        config_id: str,
        event_type: str,
        attempts: int,
        last_error: str | None = None,
        last_status_code: int | None = None,
    ) -> None:
        await self._store.record_push_deadletter(
            task_id=task_id,
            config_id=config_id,
            event_type=event_type,
            attempts=attempts,
            last_error=last_error,
            last_status_code=last_status_code,
        )

    async def list(self, *, task_id: str | None = None) -> list[dict]:
        return await self._store.list_push_deadletters(task_id=task_id)

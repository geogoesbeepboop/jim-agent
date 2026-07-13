"""The A2A x402 payment coordinator — ADR-0008's billing invariant, extended to A2A.

A durable A2A research task pauses at ``input-required`` carrying an x402 payment
challenge, the client pays over the wire, and jim collects a cited memo. This
module is the seam that guarantees the money half of that dance:

- **Verified before execution.** A submitted payment is verified against the
  *stored* requirements (never the client's copy — the price-swap defense) before
  the task is allowed to do paid work.
- **Settled only after the gates pass, exactly once.** Settlement is a CAS-guarded
  transition (``verified → settling``); exactly one caller moves money, and only
  after jim's deterministic research gates approved the run. The never-bill-
  rejected invariant (ADR-0008) becomes: a discarded/rejected task never settles.
- **Never for rejected research.** ``discard`` marks a task terminal so a later
  ``settle`` refuses without ever touching the rail — the coordinator-level
  enforcement of "gate-rejected research is refused, never billed".

The split mirrors the seller: the model/executor proposes work, deterministic
code (the CAS + expiry checks here) disposes of the money.

``PaymentRail`` is the swappable seam over the x402 package. ``X402Rail`` is the
production implementation (wraps ``x402ResourceServer`` exactly like
``jim.seller.app.build_app``); tests inject a ``FakeRail`` so the default suite
never touches the network.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import TYPE_CHECKING, Any, Protocol

from jim.a2a.extension import (
    MD_PAYMENT_ERROR,
    MD_PAYMENT_RECEIPTS,
    MD_PAYMENT_REQUIRED,
    MD_PAYMENT_STATUS,
    PaymentStatus,
)
from jim.marketplace.pricing import price_for
from jim.research.products import usd

if TYPE_CHECKING:
    from jim.a2a.stores import AuthView, PaymentAuths
    from jim.config import Settings
    from jim.store import Store

_USDC_DECIMALS = 6


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --- the rail seam ----------------------------------------------------------


@dataclass(frozen=True)
class RailResult:
    """The outcome of a rail verify/settle call.

    ``ok`` / ``payer`` / ``tx_hash`` / ``error`` are the core fields the
    coordinator branches on. ``amount`` (settled atomic USDC), ``network``, and
    ``raw`` (the full serialized facilitator response) carry the settlement
    receipt body the coordinator records and echoes back as the x402 receipt.
    """

    ok: bool
    payer: str | None = None
    tx_hash: str | None = None
    error: str | None = None
    amount: str | None = None
    network: str | None = None
    raw: dict[str, Any] | None = None


class PaymentRail(Protocol):
    """Server-side x402 operations, abstracted so tests never hit the network."""

    async def build_requirements(
        self, *, price_usd: float, resource: str, description: str
    ) -> dict:
        """Return the x402 ``PaymentRequirements`` as a JSON-ready dict (the value
        that goes under ``x402.payment.required`` on the paused task)."""
        ...

    async def verify(self, *, payload: dict, requirements: dict) -> RailResult:
        """Verify a submitted signed ``PaymentPayload`` against ``requirements``."""
        ...

    async def settle(self, *, payload: dict, requirements: dict) -> RailResult:
        """Settle a verified payment. THE money move."""
        ...


class X402Rail:
    """Production ``PaymentRail`` over the x402 package (mirrors ``seller.app``).

    Wraps an ``x402ResourceServer`` bound to jim's facilitator with the EXACT-EVM
    scheme registered for ``settings.network`` — the same construction the HTTP
    paywall uses. Requirements are built through the package's own
    ``build_payment_requirements`` (which owns USD→USDC-atomic conversion via the
    scheme's ``parse_price``), so a memo priced ``$0.225`` serializes to the exact
    ``amount: "225000"`` a standard x402 client already knows how to pay.

    Network I/O (facilitator ``/supported`` on init, verify/settle) happens only
    on the production path; the default test suite injects ``FakeRail`` and only
    ever instantiates this class to assert construction.
    """

    def __init__(self, settings: Settings, *, server: Any = None) -> None:
        self._settings = settings
        if server is None:
            from x402.mechanisms.evm.exact import ExactEvmServerScheme
            from x402.server import x402ResourceServer

            from jim.marketplace.facilitator import build_facilitator_client

            server = x402ResourceServer(build_facilitator_client(settings))
            server.register(settings.network, ExactEvmServerScheme())
        self._server = server
        self._initialized = False

    def _ensure_initialized(self) -> None:
        # Lazy: the server must fetch facilitator-supported kinds before it can
        # build requirements. Deferred to first use so construction stays offline
        # (the LoggingFacilitatorClient degrades to exact-EVM when /supported is
        # unreachable). Idempotent via the flag.
        if not self._initialized:
            self._server.initialize()
            self._initialized = True

    async def build_requirements(
        self, *, price_usd: float, resource: str, description: str
    ) -> dict:
        from x402.server import ResourceConfig

        self._ensure_initialized()
        config = ResourceConfig(
            scheme="exact",
            pay_to=self._settings.evm_address,
            price=price_usd,  # x402 Money accepts a float; the scheme converts to atomic USDC
            network=self._settings.network,
            max_timeout_seconds=self._settings.a2a_payment_timeout_seconds,
        )
        requirements = self._server.build_payment_requirements(config)[0]
        # Serialize exactly as the 402 challenge encodes it (camelCase aliases,
        # no None fields) so the stored dict round-trips through model_validate and
        # a standard x402 client can pay it verbatim.
        return requirements.model_dump(mode="json", by_alias=True, exclude_none=True)

    async def verify(self, *, payload: dict, requirements: dict) -> RailResult:
        from x402.schemas import PaymentPayload, PaymentRequirements

        self._ensure_initialized()  # robust to a recovery-path verify with no prior quote
        pr = PaymentRequirements.model_validate(requirements)
        pp = PaymentPayload.model_validate(payload)
        resp = await self._server.verify_payment(pp, pr)
        v = resp.verify
        if v.is_valid:
            return RailResult(ok=True, payer=v.payer, raw=_dump(v))
        return RailResult(
            ok=False,
            payer=v.payer,
            error=v.invalid_reason or v.invalid_message or "verification failed",
            raw=_dump(v),
        )

    async def settle(self, *, payload: dict, requirements: dict) -> RailResult:
        from x402.schemas import PaymentPayload, PaymentRequirements

        self._ensure_initialized()  # robust to a recovery-path settle with no prior quote
        pr = PaymentRequirements.model_validate(requirements)
        pp = PaymentPayload.model_validate(payload)
        resp = await self._server.settle_payment(pp, pr)
        if resp.success:
            return RailResult(
                ok=True,
                payer=resp.payer,
                tx_hash=resp.transaction or None,
                amount=resp.amount,
                network=resp.network,
                raw=_dump(resp),
            )
        return RailResult(
            ok=False,
            payer=resp.payer,
            tx_hash=resp.transaction or None,
            network=resp.network,
            error=resp.error_reason or resp.error_message or "settlement failed",
            raw=_dump(resp),
        )


def _dump(model: Any) -> dict:
    """Serialize an x402 pydantic response to a JSON-ready dict."""
    return model.model_dump(mode="json", by_alias=True, exclude_none=True)


# --- coordinator outcomes ---------------------------------------------------


class SubmitStatus(str, Enum):
    VERIFIED = "verified"
    FAILED = "failed"
    EXPIRED = "expired"


class SettleStatus(str, Enum):
    SETTLED = "settled"
    ALREADY_SETTLING = "already_settling"
    FAILED = "failed"
    EXPIRED = "expired"


@dataclass(frozen=True)
class SubmitOutcome:
    """Result of ``submit``. ``metadata`` is the x402 status the executor stamps
    onto the task; ``verified`` → the task may start paid work."""

    status: SubmitStatus
    payer: str | None = None
    reason: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def verified(self) -> bool:
        return self.status is SubmitStatus.VERIFIED


@dataclass(frozen=True)
class SettleOutcome:
    """Result of ``settle``. ``settled`` carries the receipts metadata + tx;
    ``already_settling`` means another caller owns the settlement (idempotent, no
    error — the executor re-serves the current task snapshot)."""

    status: SettleStatus
    tx_hash: str | None = None
    reason: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def settled(self) -> bool:
        return self.status is SettleStatus.SETTLED


# --- the coordinator --------------------------------------------------------


class X402PaymentCoordinator:
    """Quote → submit(verify) → settle(once) → receipt for A2A durable paid tasks.

    Composes ``PaymentAuths`` (the persisted state machine + settle-once CAS),
    a ``PaymentRail`` (the x402 verify/settle), and the ``Store`` (settlement
    receipts). Every money-moving decision is deterministic here — the executor
    only ever asks this coordinator to advance the payment state.
    """

    def __init__(
        self, *, auths: PaymentAuths, rail: PaymentRail, store: Store, settings: Settings
    ) -> None:
        self._auths = auths
        self._rail = rail
        self._store = store
        self._settings = settings

    # -- price resolution (reuses the published schedule; no duplicated math) --

    def _resolve_price(self, *, kind: str, product: str, mode: str) -> float:
        if kind == "research":
            tier = "agent" if mode == "agent" else "oneshot"
            return price_for(product, tier)
        if kind == "monitor_activation":
            return usd(self._settings.monitor_activation_price)
        if kind == "monitor_release":
            return usd(self._settings.monitor_update_price)
        raise ValueError(f"unknown quote kind: {kind!r}")

    # -- quote -----------------------------------------------------------------

    async def quote(
        self, *, task_id: str, kind: str, product: str, identifier: str, mode: str
    ) -> dict:
        """Price the task, build + persist the x402 requirements as a fresh
        ``required`` auth, and return the metadata for the ``input-required``
        pause message. The stored requirements are the price-swap checkpoint:
        every later verify/settle validates against THEM, not the client's copy."""
        price = self._resolve_price(kind=kind, product=product, mode=mode)
        resource = f"{self._settings.public_url}/a2a/tasks/{task_id}"
        description = self._describe(kind=kind, product=product, identifier=identifier, mode=mode)
        requirements = await self._rail.build_requirements(
            price_usd=price, resource=resource, description=description
        )
        expires_at = _utcnow() + timedelta(seconds=self._settings.a2a_payment_timeout_seconds)
        await self._auths.create_required(
            task_id=task_id,
            kind=kind,
            product=product,
            identifier=identifier,
            mode=mode,
            amount_usd=price,
            requirements=requirements,
            expires_at=expires_at,
        )
        return {
            MD_PAYMENT_STATUS: PaymentStatus.REQUIRED.value,
            MD_PAYMENT_REQUIRED: requirements,
        }

    @staticmethod
    def _describe(*, kind: str, product: str, identifier: str, mode: str) -> str:
        if kind == "research":
            return f"jim {product} research for {identifier} ({mode})"
        if kind == "monitor_activation":
            return f"jim monitor activation for {identifier}"
        if kind == "monitor_release":
            return f"jim monitor update for {identifier}"
        return f"jim {kind} for {identifier}"

    # -- submit (verify) -------------------------------------------------------

    async def submit(self, *, task_id: str, payload: dict) -> SubmitOutcome:
        """Verify a submitted payment against the STORED requirements. On success
        the payload is encrypted at rest and the auth advances to ``verified``; on
        failure the auth stays ``required`` so the client can retry with a
        corrected payment. An expired quote is refused before the rail is touched."""
        auth = await self._auths.get(task_id)
        if auth is None:
            return SubmitOutcome(SubmitStatus.FAILED, reason="unknown task")
        if auth.status != "required":
            return SubmitOutcome(
                SubmitStatus.FAILED, reason=f"not awaiting payment (status={auth.status})"
            )
        if self._expired(auth):
            await self._auths.mark(task_id, status="expired")
            return self._expired_submit()

        result = await self._rail.verify(payload=payload, requirements=auth.requirements)
        if not result.ok:
            # Auth stays "required": a bad payment is retryable, not terminal.
            return SubmitOutcome(
                SubmitStatus.FAILED,
                payer=result.payer,
                reason=result.error,
                metadata={
                    MD_PAYMENT_STATUS: PaymentStatus.REJECTED.value,
                    MD_PAYMENT_ERROR: result.error or "payment rejected",
                },
            )
        await self._auths.attach_payload(task_id, payload, result.payer)
        await self._auths.mark(task_id, status="verified")
        return SubmitOutcome(
            SubmitStatus.VERIFIED,
            payer=result.payer,
            metadata={MD_PAYMENT_STATUS: PaymentStatus.VERIFIED.value},
        )

    @staticmethod
    def _expired_submit() -> SubmitOutcome:
        return SubmitOutcome(
            SubmitStatus.EXPIRED,
            reason="payment window expired",
            metadata={
                MD_PAYMENT_STATUS: PaymentStatus.FAILED.value,
                MD_PAYMENT_ERROR: "payment window expired",
            },
        )

    # -- settle (the money move, exactly once) --------------------------------

    async def settle(self, *, task_id: str) -> SettleOutcome:
        """Settle a verified payment exactly once. The CAS (``verified →
        settling``) elects the single caller that moves money; the loser returns
        ``already_settling`` with no error. A non-verified auth (e.g. discarded)
        is refused WITHOUT touching the rail — the never-bill-rejected path."""
        auth = await self._auths.get(task_id)
        if auth is None:
            return SettleOutcome(SettleStatus.FAILED, reason="unknown task")
        if auth.status != "verified":
            # settling/settled → idempotent no-op; anything else (required,
            # discarded, expired, settle_failed) → refuse, never settle.
            if auth.status in ("settling", "settled"):
                return SettleOutcome(SettleStatus.ALREADY_SETTLING)
            return self._refused_settle(auth.status)
        # Re-check expiry immediately before the CAS: a quote that lapsed while
        # the task was working must not settle.
        if self._expired(auth):
            await self._auths.mark(task_id, status="expired")
            return self._expired_settle()

        if not await self._auths.cas(task_id, "verified", "settling"):
            # Another caller won the settle-once election.
            return SettleOutcome(SettleStatus.ALREADY_SETTLING)

        result = await self._rail.settle(payload=auth.payload, requirements=auth.requirements)
        if not result.ok:
            await self._auths.mark(task_id, status="settle_failed")
            return SettleOutcome(
                SettleStatus.FAILED,
                tx_hash=result.tx_hash,
                reason=result.error,
                metadata={
                    MD_PAYMENT_STATUS: PaymentStatus.FAILED.value,
                    MD_PAYMENT_ERROR: result.error or "settlement failed",
                },
            )
        await self._auths.mark(task_id, status="settled", tx_hash=result.tx_hash)
        await self._record_receipt(auth, result, task_id)
        return SettleOutcome(
            SettleStatus.SETTLED,
            tx_hash=result.tx_hash,
            metadata={
                MD_PAYMENT_STATUS: PaymentStatus.COMPLETED.value,
                MD_PAYMENT_RECEIPTS: [result.raw or {}],
            },
        )

    @staticmethod
    def _refused_settle(status: str) -> SettleOutcome:
        return SettleOutcome(
            SettleStatus.FAILED,
            reason=f"not settleable (status={status})",
            metadata={
                MD_PAYMENT_STATUS: PaymentStatus.FAILED.value,
                MD_PAYMENT_ERROR: f"payment not settleable (status={status})",
            },
        )

    @staticmethod
    def _expired_settle() -> SettleOutcome:
        return SettleOutcome(
            SettleStatus.EXPIRED,
            reason="payment window expired",
            metadata={
                MD_PAYMENT_STATUS: PaymentStatus.FAILED.value,
                MD_PAYMENT_ERROR: "payment window expired",
            },
        )

    # -- discard (rejection / cancellation / error) ---------------------------

    async def discard(self, *, task_id: str, reason: str) -> None:
        """Mark a task's payment terminal so it will never settle — the
        coordinator-level never-bill-rejected enforcement (a gate-rejected run,
        a cancel, or an executor error). Idempotent; a no-op if the task is
        absent or money already moved (never clobbers a settled/settling row)."""
        auth = await self._auths.get(task_id)
        if auth is None:
            return
        if auth.status in ("settling", "settled"):
            return  # money is in flight / already moved — do not rewrite it
        await self._auths.mark(task_id, status="discarded")

    # -- helpers ---------------------------------------------------------------

    @staticmethod
    def _expired(auth: AuthView) -> bool:
        return auth.expires_at is not None and _utcnow() >= auth.expires_at

    async def _record_receipt(self, auth: AuthView, result: RailResult, task_id: str) -> None:
        """Append the settlement receipt, mirroring ``seller.audit._record``'s
        field shape. A2A responses carry no PAYMENT-RESPONSE header, so the legacy
        audit middleware never double-records this."""
        await self._store.record_receipt(
            tx_hash=result.tx_hash,
            payer=result.payer or auth.payer,
            pay_to=self._settings.evm_address,
            amount_usdc=_amount_to_usdc(result.amount, auth.amount_usd),
            network=result.network or self._settings.network,
            path=f"/a2a/tasks/{task_id}",
            product=auth.product,
            identifier=(auth.identifier.upper() if auth.identifier else None),
            mode=auth.mode,
            status_code=200,
            success=True,
            receipt=result.raw or {},
        )


def _amount_to_usdc(raw: Any, fallback: float) -> float:
    """Coerce a settle-response ``amount`` (USDC base units, or a decimal string)
    into USDC, mirroring ``seller.audit._amount_to_usdc``. Falls back to the
    quoted price when the rail reported no amount."""
    if raw is None or raw == "":
        return fallback
    try:
        text = str(raw).strip()
        if "." in text:
            return float(text)
        return int(text) / (10**_USDC_DECIMALS)
    except (ValueError, TypeError):
        return fallback

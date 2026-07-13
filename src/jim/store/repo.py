"""The store: cache lookups, purchase/query recording, margin queries.

One ``Store`` interface, two backends:
  - ``SqlStore``    : Postgres + pgvector (set DATABASE_URL).
  - ``MemoryStore`` : in-process dicts + Python cosine (no infra; tests + dev).

``get_store()`` picks SqlStore when DATABASE_URL is set, else MemoryStore — so
the engine and the dashboard are identical regardless of backend.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Protocol

from jim.config import get_settings
from jim.store.embed import cosine

# Sentinel for partial updates: a parameter left as ``_UNSET`` is skipped, so an
# explicit ``None`` can still clear a nullable column (see update_a2a_auth).
_UNSET: object = object()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_dt(value: str | datetime | None) -> datetime:
    """Coerce an ISO string / datetime / None into a tz-aware datetime."""
    if value is None:
        return _utcnow()
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value)


@dataclass
class CachedPurchase:
    payload: dict
    cost_usd: float
    tx_hash: str | None
    age_seconds: float


class Store(Protocol):
    async def get_cached_purchase(self, source: str, key: str) -> CachedPurchase | None: ...
    async def record_purchase(
        self,
        *,
        source: str,
        key: str,
        url: str,
        network: str,
        cost_usd: float,
        tx_hash: str | None,
        payload: dict,
        ttl_seconds: int,
    ) -> None: ...
    async def record_query(
        self,
        *,
        product: str,
        identifier: str,
        mode: str,
        status: str,
        price_out_usd: float,
        cost_in_data_usd: float,
        cost_inference_usd: float,
        cache_hit: bool,
        attempts: int,
    ) -> None: ...
    async def margin_summary(self) -> dict: ...
    async def recent_queries(self, limit: int = 20) -> list[dict]: ...
    # --- settlement audit log (the on-chain revenue trail) ---
    async def record_receipt(
        self,
        *,
        tx_hash: str | None,
        payer: str | None,
        pay_to: str | None,
        amount_usdc: float,
        network: str,
        path: str,
        product: str | None,
        identifier: str | None,
        mode: str | None,
        status_code: int,
        success: bool,
        receipt: dict,
    ) -> None: ...
    async def recent_receipts(self, limit: int = 50) -> list[dict]: ...
    async def receipts_summary(self) -> dict: ...
    async def upsert_insight(self, *, key: str, text: str, embedding: list[float]) -> None: ...
    async def search_insights(self, embedding: list[float], k: int = 5) -> list[dict]: ...
    # --- memo cache (serve a recent identical memo, skip re-synthesis) ---
    async def get_cached_memo(
        self, *, key: str, fingerprint: str, ttl_seconds: int
    ) -> dict | None: ...
    async def put_cached_memo(
        self, *, key: str, fingerprint: str, memo: str, debate: str | None
    ) -> None: ...
    # --- Phase 7: per-source trust (the gate pass-rate as reputation) ---
    async def record_trust_event(self, *, source: str, ok: bool, context: str) -> None: ...
    async def trust_scores(self) -> dict[str, dict]: ...
    # --- Phase 4: monitors ---
    async def save_monitor(self, row: dict) -> None: ...
    async def get_monitor(self, monitor_id: str) -> dict | None: ...
    async def list_monitors(self, *, enabled_only: bool = False) -> list[dict]: ...
    async def delete_monitor(self, monitor_id: str) -> bool: ...
    async def due_monitors(self, now: datetime | None = None) -> list[dict]: ...
    async def record_monitor_run(self, row: dict) -> None: ...
    async def monitor_feed(
        self, *, limit: int = 20, monitor_id: str | None = None, material_only: bool = True
    ) -> list[dict]: ...
    async def monitor_stats(self) -> dict: ...
    # --- A2A payment authorizations (verify-then-settle-once; ADR-0008 → A2A) ---
    async def save_a2a_auth(
        self,
        *,
        task_id: str,
        kind: str,
        product: str,
        identifier: str,
        mode: str,
        amount_usd: float,
        requirements: dict,
        payload_ciphertext: str | None,
        payer: str | None,
        status: str,
        expires_at: datetime | None,
    ) -> None: ...  # upsert-by-task_id (a re-quote before payment overwrites the pending row)
    async def get_a2a_auth(self, task_id: str) -> dict | None: ...
    async def update_a2a_auth(
        self,
        task_id: str,
        *,
        payload_ciphertext: str | None = _UNSET,  # sentinel: omit → skip
        payer: str | None = _UNSET,
        tx_hash: str | None = _UNSET,
        status: str = _UNSET,
    ) -> None: ...
    async def cas_a2a_auth_status(self, task_id: str, from_status: str, to_status: str) -> bool: ...
    async def list_a2a_auths(self, *, status: str | None = None) -> list[dict]: ...
    # --- A2A withheld monitor artifacts (the ONLY place pre-payment content lives) ---
    async def save_withheld(
        self,
        *,
        task_id: str,
        monitor_id: str,
        severity: str,
        as_of: str | None,
        price_usd: float,
        payload_ciphertext: str,
    ) -> None: ...  # upsert-by-task_id
    async def get_withheld(self, task_id: str) -> dict | None: ...
    async def delete_withheld(self, task_id: str) -> None: ...  # idempotent
    # --- A2A push notification configs ---
    async def save_a2a_push_config(
        self, *, task_id: str, config_id: str, config_ciphertext: str
    ) -> None: ...  # upsert-by-(task_id, config_id)
    async def get_a2a_push_configs(self, task_id: str) -> list[dict]: ...
    async def delete_a2a_push_config(self, task_id: str, config_id: str) -> None: ...
    # --- A2A push dead letters (append-only; no event body) ---
    async def record_push_deadletter(
        self,
        *,
        task_id: str,
        config_id: str,
        event_type: str,
        attempts: int,
        last_error: str | None,
        last_status_code: int | None,
    ) -> None: ...
    async def list_push_deadletters(self, *, task_id: str | None = None) -> list[dict]: ...


# --- In-memory backend -------------------------------------------------------


@dataclass
class MemoryStore:
    purchases: dict[tuple[str, str], dict] = field(default_factory=dict)
    queries: list[dict] = field(default_factory=list)
    insights: dict[str, dict] = field(default_factory=dict)
    monitors: dict[str, dict] = field(default_factory=dict)
    monitor_runs: list[dict] = field(default_factory=list)
    receipts: list[dict] = field(default_factory=list)
    memos: dict[str, dict] = field(default_factory=dict)
    trust_events: list[dict] = field(default_factory=list)
    # A2A durable-task tables (mirror the four ORM rows in jim.store.models).
    a2a_auths: dict[str, dict] = field(default_factory=dict)  # task_id -> auth row
    withheld: dict[str, dict] = field(default_factory=dict)  # task_id -> withheld row
    a2a_push_configs: dict[str, dict] = field(default_factory=dict)  # "tid:cid" -> config row
    push_deadletters: list[dict] = field(default_factory=list)  # append-only
    # Guards the compare-and-swap on auth status so settlement is exactly-once even
    # under concurrent settle attempts. Lazily bound to the running loop on first use.
    _a2a_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False, compare=False)

    async def get_cached_purchase(self, source, key):
        row = self.purchases.get((source, key))
        if not row or row["expires_at"] < _utcnow():
            return None
        return CachedPurchase(
            payload=row["payload"],
            cost_usd=row["cost_usd"],
            tx_hash=row["tx_hash"],
            age_seconds=(_utcnow() - row["created_at"]).total_seconds(),
        )

    async def record_purchase(
        self, *, source, key, url, network, cost_usd, tx_hash, payload, ttl_seconds
    ):
        now = _utcnow()
        self.purchases[(source, key)] = {
            "url": url,
            "network": network,
            "cost_usd": cost_usd,
            "tx_hash": tx_hash,
            "payload": payload,
            "created_at": now,
            "expires_at": now + timedelta(seconds=ttl_seconds),
        }

    async def record_query(
        self,
        *,
        product,
        identifier,
        mode,
        status,
        price_out_usd,
        cost_in_data_usd,
        cost_inference_usd,
        cache_hit,
        attempts,
    ):
        margin = price_out_usd - cost_in_data_usd - cost_inference_usd
        self.queries.append(
            {
                "product": product,
                "identifier": identifier,
                "mode": mode,
                "status": status,
                "price_out_usd": price_out_usd,
                "cost_in_data_usd": cost_in_data_usd,
                "cost_inference_usd": cost_inference_usd,
                "margin_usd": margin,
                "cache_hit": cache_hit,
                "attempts": attempts,
                "created_at": _utcnow(),
            }
        )

    async def margin_summary(self):
        return _summarize(self.queries)

    async def recent_queries(self, limit=20):
        rows = sorted(self.queries, key=lambda q: q["created_at"], reverse=True)[:limit]
        return [_query_view(q) for q in rows]

    async def record_receipt(
        self,
        *,
        tx_hash,
        payer,
        pay_to,
        amount_usdc,
        network,
        path,
        product,
        identifier,
        mode,
        status_code,
        success,
        receipt,
    ):
        self.receipts.append(
            {
                "tx_hash": tx_hash,
                "payer": payer,
                "pay_to": pay_to,
                "amount_usdc": amount_usdc,
                "network": network,
                "path": path,
                "product": product,
                "identifier": identifier,
                "mode": mode,
                "status_code": status_code,
                "success": success,
                "receipt": receipt,
                "created_at": _utcnow(),
            }
        )

    async def recent_receipts(self, limit=50):
        rows = sorted(self.receipts, key=lambda r: r["created_at"], reverse=True)[:limit]
        return [_receipt_view(r) for r in rows]

    async def receipts_summary(self):
        return _summarize_receipts(self.receipts)

    async def upsert_insight(self, *, key, text, embedding):
        self.insights[key] = {"text": text, "embedding": embedding}

    async def search_insights(self, embedding, k=5):
        scored = [
            {"cache_key": key, "text": v["text"], "score": cosine(embedding, v["embedding"])}
            for key, v in self.insights.items()
        ]
        scored.sort(key=lambda r: r["score"], reverse=True)
        return scored[:k]

    async def get_cached_memo(self, *, key, fingerprint, ttl_seconds):
        row = self.memos.get(key)
        if not row or row["fingerprint"] != fingerprint:
            return None
        age = (_utcnow() - row["created_at"]).total_seconds()
        if age > ttl_seconds:
            return None
        return {"memo": row["memo"], "debate": row.get("debate"), "age_seconds": age}

    async def put_cached_memo(self, *, key, fingerprint, memo, debate):
        self.memos[key] = {
            "fingerprint": fingerprint,
            "memo": memo,
            "debate": debate,
            "created_at": _utcnow(),
        }

    # --- Phase 7: per-source trust ---
    async def record_trust_event(self, *, source, ok, context):
        self.trust_events.append(
            {"source": source, "ok": bool(ok), "context": context, "created_at": _utcnow()}
        )

    async def trust_scores(self):
        return _summarize_trust(self.trust_events)

    # --- Phase 4: monitors ---
    async def save_monitor(self, row):
        self.monitors[row["id"]] = dict(row)

    async def get_monitor(self, monitor_id):
        row = self.monitors.get(monitor_id)
        return dict(row) if row else None

    async def list_monitors(self, *, enabled_only=False):
        rows = [dict(r) for r in self.monitors.values()]
        if enabled_only:
            rows = [r for r in rows if r.get("enabled", True)]
        return sorted(rows, key=lambda r: r.get("created_at") or "")

    async def delete_monitor(self, monitor_id):
        return self.monitors.pop(monitor_id, None) is not None

    async def due_monitors(self, now=None):
        now = now or _utcnow()
        out = []
        for r in self.monitors.values():
            if not r.get("enabled", True):
                continue
            nxt = r.get("next_run_at")
            if nxt is None or _as_dt(nxt) <= now:
                out.append(dict(r))
        return sorted(out, key=lambda r: r.get("next_run_at") or "")

    async def record_monitor_run(self, row):
        self.monitor_runs.append(dict(row))

    async def monitor_feed(self, *, limit=20, monitor_id=None, material_only=True):
        rows = self.monitor_runs
        if monitor_id:
            rows = [r for r in rows if r["monitor_id"] == monitor_id]
        if material_only:
            rows = [r for r in rows if r.get("material")]
        rows = sorted(rows, key=lambda r: r.get("ran_at") or "", reverse=True)
        return [dict(r) for r in rows[:limit]]

    async def monitor_stats(self):
        return _summarize_monitor_runs(self.monitor_runs)

    # --- A2A payment authorizations ---
    async def save_a2a_auth(
        self,
        *,
        task_id,
        kind,
        product,
        identifier,
        mode,
        amount_usd,
        requirements,
        payload_ciphertext,
        payer,
        status,
        expires_at,
    ):
        existing = self.a2a_auths.get(task_id)
        now = _utcnow()
        self.a2a_auths[task_id] = {
            "task_id": task_id,
            "kind": kind,
            "product": product,
            "identifier": identifier,
            "mode": mode,
            "amount_usd": amount_usd,
            "requirements": requirements,
            "payload_ciphertext": payload_ciphertext,
            "payer": payer,
            "status": status,
            "expires_at": expires_at,
            "tx_hash": None,  # a fresh quote clears any stale settlement tx
            "created_at": existing["created_at"] if existing else now,
            "updated_at": now,
        }

    async def get_a2a_auth(self, task_id):
        row = self.a2a_auths.get(task_id)
        return dict(row) if row else None

    async def update_a2a_auth(
        self, task_id, *, payload_ciphertext=_UNSET, payer=_UNSET, tx_hash=_UNSET, status=_UNSET
    ):
        row = self.a2a_auths.get(task_id)
        if row is None:
            return
        if payload_ciphertext is not _UNSET:
            row["payload_ciphertext"] = payload_ciphertext
        if payer is not _UNSET:
            row["payer"] = payer
        if tx_hash is not _UNSET:
            row["tx_hash"] = tx_hash
        if status is not _UNSET:
            row["status"] = status
        row["updated_at"] = _utcnow()

    async def cas_a2a_auth_status(self, task_id, from_status, to_status):
        # The settle-once guard: hold the lock across check+set so exactly one of
        # N concurrent callers observes ``from_status`` and flips it. Loser → False.
        async with self._a2a_lock:
            row = self.a2a_auths.get(task_id)
            if row is None or row["status"] != from_status:
                return False
            row["status"] = to_status
            row["updated_at"] = _utcnow()
            return True

    async def list_a2a_auths(self, *, status=None):
        rows = [dict(r) for r in self.a2a_auths.values()]
        if status is not None:
            rows = [r for r in rows if r["status"] == status]
        return sorted(rows, key=lambda r: r["created_at"])

    # --- A2A withheld monitor artifacts ---
    async def save_withheld(
        self, *, task_id, monitor_id, severity, as_of, price_usd, payload_ciphertext
    ):
        self.withheld[task_id] = {
            "task_id": task_id,
            "monitor_id": monitor_id,
            "severity": severity,
            "as_of": as_of,
            "price_usd": price_usd,
            "payload_ciphertext": payload_ciphertext,
            "created_at": _utcnow(),
        }

    async def get_withheld(self, task_id):
        row = self.withheld.get(task_id)
        return dict(row) if row else None

    async def delete_withheld(self, task_id):
        self.withheld.pop(task_id, None)  # idempotent

    # --- A2A push notification configs ---
    async def save_a2a_push_config(self, *, task_id, config_id, config_ciphertext):
        self.a2a_push_configs[f"{task_id}:{config_id}"] = {
            "task_id": task_id,
            "config_id": config_id,
            "config_ciphertext": config_ciphertext,
            "created_at": _utcnow(),
        }

    async def get_a2a_push_configs(self, task_id):
        rows = [dict(r) for r in self.a2a_push_configs.values() if r["task_id"] == task_id]
        return sorted(rows, key=lambda r: r["created_at"])

    async def delete_a2a_push_config(self, task_id, config_id):
        self.a2a_push_configs.pop(f"{task_id}:{config_id}", None)  # idempotent

    # --- A2A push dead letters (append-only) ---
    async def record_push_deadletter(
        self, *, task_id, config_id, event_type, attempts, last_error, last_status_code
    ):
        self.push_deadletters.append(
            {
                "task_id": task_id,
                "config_id": config_id,
                "event_type": event_type,
                "attempts": attempts,
                "last_error": last_error,
                "last_status_code": last_status_code,
                "created_at": _utcnow(),
            }
        )

    async def list_push_deadletters(self, *, task_id=None):
        rows = self.push_deadletters
        if task_id is not None:
            rows = [r for r in rows if r["task_id"] == task_id]
        return sorted((dict(r) for r in rows), key=lambda r: r["created_at"])


# --- SQL backend -------------------------------------------------------------


class SqlStore:
    def __init__(self, database_url: str):
        from jim.store.db import get_sessionmaker

        self._sm = get_sessionmaker(database_url)

    async def get_cached_purchase(self, source, key):
        from sqlalchemy import select
        from jim.store.models import DataPurchase

        async with self._sm() as s:
            row = (
                await s.execute(
                    select(DataPurchase)
                    .where(DataPurchase.source == source, DataPurchase.cache_key == key)
                    .order_by(DataPurchase.created_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if not row or row.expires_at < _utcnow():
                return None
            return CachedPurchase(
                payload=row.payload,
                cost_usd=row.cost_usd,
                tx_hash=row.tx_hash,
                age_seconds=(_utcnow() - row.created_at).total_seconds(),
            )

    async def record_purchase(
        self, *, source, key, url, network, cost_usd, tx_hash, payload, ttl_seconds
    ):
        from jim.store.models import DataPurchase

        async with self._sm() as s:
            s.add(
                DataPurchase(
                    source=source,
                    cache_key=key,
                    url=url,
                    network=network,
                    cost_usd=cost_usd,
                    tx_hash=tx_hash,
                    payload=payload,
                    expires_at=_utcnow() + timedelta(seconds=ttl_seconds),
                )
            )
            await s.commit()

    async def record_query(
        self,
        *,
        product,
        identifier,
        mode,
        status,
        price_out_usd,
        cost_in_data_usd,
        cost_inference_usd,
        cache_hit,
        attempts,
    ):
        from jim.store.models import QueryRecord

        margin = price_out_usd - cost_in_data_usd - cost_inference_usd
        async with self._sm() as s:
            s.add(
                QueryRecord(
                    product=product,
                    identifier=identifier,
                    mode=mode,
                    status=status,
                    price_out_usd=price_out_usd,
                    cost_in_data_usd=cost_in_data_usd,
                    cost_inference_usd=cost_inference_usd,
                    margin_usd=margin,
                    cache_hit=cache_hit,
                    attempts=attempts,
                )
            )
            await s.commit()

    async def margin_summary(self):
        from sqlalchemy import select
        from jim.store.models import QueryRecord

        async with self._sm() as s:
            rows = (await s.execute(select(QueryRecord))).scalars().all()
            return _summarize([_record_dict(r) for r in rows])

    async def recent_queries(self, limit=20):
        from sqlalchemy import select
        from jim.store.models import QueryRecord

        async with self._sm() as s:
            rows = (
                (
                    await s.execute(
                        select(QueryRecord).order_by(QueryRecord.created_at.desc()).limit(limit)
                    )
                )
                .scalars()
                .all()
            )
            return [_query_view(_record_dict(r)) for r in rows]

    async def record_receipt(
        self,
        *,
        tx_hash,
        payer,
        pay_to,
        amount_usdc,
        network,
        path,
        product,
        identifier,
        mode,
        status_code,
        success,
        receipt,
    ):
        from jim.store.models import PaymentReceipt

        async with self._sm() as s:
            s.add(
                PaymentReceipt(
                    tx_hash=tx_hash,
                    payer=payer,
                    pay_to=pay_to,
                    amount_usdc=amount_usdc,
                    network=network,
                    path=path,
                    product=product,
                    identifier=identifier,
                    mode=mode,
                    status_code=status_code,
                    success=success,
                    receipt=receipt,
                )
            )
            await s.commit()

    async def recent_receipts(self, limit=50):
        from sqlalchemy import select
        from jim.store.models import PaymentReceipt

        async with self._sm() as s:
            rows = (
                (
                    await s.execute(
                        select(PaymentReceipt)
                        .order_by(PaymentReceipt.created_at.desc())
                        .limit(limit)
                    )
                )
                .scalars()
                .all()
            )
            return [_receipt_view(_receipt_dict(r)) for r in rows]

    async def receipts_summary(self):
        from sqlalchemy import select
        from jim.store.models import PaymentReceipt

        async with self._sm() as s:
            rows = (await s.execute(select(PaymentReceipt))).scalars().all()
            return _summarize_receipts([_receipt_dict(r) for r in rows])

    async def upsert_insight(self, *, key, text, embedding):
        from sqlalchemy import select
        from jim.store.models import Insight

        async with self._sm() as s:
            existing = (
                await s.execute(select(Insight).where(Insight.cache_key == key))
            ).scalar_one_or_none()
            if existing:
                existing.text = text
                existing.embedding = embedding
            else:
                s.add(Insight(cache_key=key, text=text, embedding=embedding))
            await s.commit()

    async def search_insights(self, embedding, k=5):
        from sqlalchemy import select
        from jim.store.models import Insight

        async with self._sm() as s:
            # pgvector cosine distance operator; score = 1 - distance.
            dist = Insight.embedding.cosine_distance(embedding)
            rows = (await s.execute(select(Insight, dist.label("d")).order_by(dist).limit(k))).all()
            return [
                {"cache_key": r[0].cache_key, "text": r[0].text, "score": 1.0 - float(r[1])}
                for r in rows
            ]

    async def get_cached_memo(self, *, key, fingerprint, ttl_seconds):
        from sqlalchemy import select
        from jim.store.models import MemoCacheEntry

        async with self._sm() as s:
            row = (
                await s.execute(select(MemoCacheEntry).where(MemoCacheEntry.cache_key == key))
            ).scalar_one_or_none()
            if not row or row.fingerprint != fingerprint:
                return None
            age = (_utcnow() - row.created_at).total_seconds()
            if age > ttl_seconds:
                return None
            return {"memo": row.memo, "debate": row.debate, "age_seconds": age}

    async def put_cached_memo(self, *, key, fingerprint, memo, debate):
        from sqlalchemy import select
        from jim.store.models import MemoCacheEntry

        async with self._sm() as s:
            existing = (
                await s.execute(select(MemoCacheEntry).where(MemoCacheEntry.cache_key == key))
            ).scalar_one_or_none()
            if existing:
                existing.fingerprint = fingerprint
                existing.memo = memo
                existing.debate = debate
                existing.created_at = _utcnow()  # reset freshness window on new data
            else:
                s.add(
                    MemoCacheEntry(
                        cache_key=key, fingerprint=fingerprint, memo=memo, debate=debate
                    )
                )
            await s.commit()

    # --- Phase 7: per-source trust ---
    async def record_trust_event(self, *, source, ok, context):
        from jim.store.models import SourceTrustEvent

        async with self._sm() as s:
            s.add(SourceTrustEvent(source=source, ok=bool(ok), context=context))
            await s.commit()

    async def trust_scores(self):
        from sqlalchemy import select
        from jim.store.models import SourceTrustEvent

        async with self._sm() as s:
            rows = (await s.execute(select(SourceTrustEvent))).scalars().all()
            return _summarize_trust(
                [
                    {"source": r.source, "ok": r.ok, "created_at": r.created_at}
                    for r in rows
                ]
            )

    # --- Phase 4: monitors ---
    async def save_monitor(self, row):
        from jim.store.models import MonitorRow

        async with self._sm() as s:
            existing = await s.get(MonitorRow, row["id"])
            nxt = _as_dt(row.get("next_run_at")) if row.get("next_run_at") else None
            if existing:
                existing.product = row["product"]
                existing.identifier = row["identifier"]
                existing.enabled = bool(row.get("enabled", True))
                existing.next_run_at = nxt
                existing.data = row
            else:
                s.add(
                    MonitorRow(
                        id=row["id"],
                        product=row["product"],
                        identifier=row["identifier"],
                        enabled=bool(row.get("enabled", True)),
                        next_run_at=nxt,
                        data=row,
                    )
                )
            await s.commit()

    async def get_monitor(self, monitor_id):
        from jim.store.models import MonitorRow

        async with self._sm() as s:
            row = await s.get(MonitorRow, monitor_id)
            return dict(row.data) if row else None

    async def list_monitors(self, *, enabled_only=False):
        from sqlalchemy import select
        from jim.store.models import MonitorRow

        async with self._sm() as s:
            q = select(MonitorRow).order_by(MonitorRow.created_at)
            if enabled_only:
                q = q.where(MonitorRow.enabled.is_(True))
            rows = (await s.execute(q)).scalars().all()
            return [dict(r.data) for r in rows]

    async def delete_monitor(self, monitor_id):
        from jim.store.models import MonitorRow

        async with self._sm() as s:
            row = await s.get(MonitorRow, monitor_id)
            if not row:
                return False
            await s.delete(row)
            await s.commit()
            return True

    async def due_monitors(self, now=None):
        from sqlalchemy import or_, select
        from jim.store.models import MonitorRow

        now = now or _utcnow()
        async with self._sm() as s:
            q = (
                select(MonitorRow)
                .where(
                    MonitorRow.enabled.is_(True),
                    or_(MonitorRow.next_run_at.is_(None), MonitorRow.next_run_at <= now),
                )
                .order_by(MonitorRow.next_run_at)
            )
            rows = (await s.execute(q)).scalars().all()
            return [dict(r.data) for r in rows]

    async def record_monitor_run(self, row):
        from jim.store.models import MonitorRunRow

        async with self._sm() as s:
            s.add(
                MonitorRunRow(
                    monitor_id=row["monitor_id"],
                    identifier=row["identifier"],
                    product=row["product"],
                    status=row["status"],
                    material=bool(row.get("material")),
                    severity=row.get("severity", "info"),
                    n_signals=len(row.get("signals") or []),
                    price_out_usd=row.get("price_out_usd", 0.0),
                    cost_in_data_usd=row.get("cost_in_data_usd", 0.0),
                    cost_inference_usd=row.get("cost_inference_usd", 0.0),
                    margin_usd=row.get("margin_usd", 0.0),
                    cache_hit=bool(row.get("cache_hit")),
                    data=row,
                )
            )
            await s.commit()

    async def monitor_feed(self, *, limit=20, monitor_id=None, material_only=True):
        from sqlalchemy import select
        from jim.store.models import MonitorRunRow

        async with self._sm() as s:
            q = select(MonitorRunRow).order_by(MonitorRunRow.created_at.desc()).limit(limit)
            if monitor_id:
                q = q.where(MonitorRunRow.monitor_id == monitor_id)
            if material_only:
                q = q.where(MonitorRunRow.material.is_(True))
            rows = (await s.execute(q)).scalars().all()
            return [dict(r.data) for r in rows]

    async def monitor_stats(self):
        from sqlalchemy import select
        from jim.store.models import MonitorRunRow

        async with self._sm() as s:
            rows = (await s.execute(select(MonitorRunRow))).scalars().all()
            return _summarize_monitor_runs(
                [
                    {
                        "status": r.status,
                        "material": r.material,
                        "price_out_usd": r.price_out_usd,
                        "cost_in_data_usd": r.cost_in_data_usd,
                        "cost_inference_usd": r.cost_inference_usd,
                        "margin_usd": r.margin_usd,
                        "cache_hit": r.cache_hit,
                    }
                    for r in rows
                ]
            )

    # --- A2A payment authorizations ---
    async def save_a2a_auth(
        self,
        *,
        task_id,
        kind,
        product,
        identifier,
        mode,
        amount_usd,
        requirements,
        payload_ciphertext,
        payer,
        status,
        expires_at,
    ):
        from jim.store.models import A2APaymentAuthRow

        async with self._sm() as s:
            existing = await s.get(A2APaymentAuthRow, task_id)
            if existing:
                existing.kind = kind
                existing.product = product
                existing.identifier = identifier
                existing.mode = mode
                existing.amount_usd = amount_usd
                existing.requirements = requirements
                existing.payload_ciphertext = payload_ciphertext
                existing.payer = payer
                existing.status = status
                existing.expires_at = expires_at
                existing.tx_hash = None  # a fresh quote clears any stale settlement tx
            else:
                s.add(
                    A2APaymentAuthRow(
                        task_id=task_id,
                        kind=kind,
                        product=product,
                        identifier=identifier,
                        mode=mode,
                        amount_usd=amount_usd,
                        requirements=requirements,
                        payload_ciphertext=payload_ciphertext,
                        payer=payer,
                        status=status,
                        expires_at=expires_at,
                    )
                )
            await s.commit()

    async def get_a2a_auth(self, task_id):
        from jim.store.models import A2APaymentAuthRow

        async with self._sm() as s:
            row = await s.get(A2APaymentAuthRow, task_id)
            return _a2a_auth_dict(row) if row else None

    async def update_a2a_auth(
        self, task_id, *, payload_ciphertext=_UNSET, payer=_UNSET, tx_hash=_UNSET, status=_UNSET
    ):
        from jim.store.models import A2APaymentAuthRow

        async with self._sm() as s:
            row = await s.get(A2APaymentAuthRow, task_id)
            if row is None:
                return
            if payload_ciphertext is not _UNSET:
                row.payload_ciphertext = payload_ciphertext
            if payer is not _UNSET:
                row.payer = payer
            if tx_hash is not _UNSET:
                row.tx_hash = tx_hash
            if status is not _UNSET:
                row.status = status
            await s.commit()

    async def cas_a2a_auth_status(self, task_id, from_status, to_status):
        # The settle-once guard: one atomic UPDATE ... WHERE status=from_status. The
        # DB row lock serializes concurrent settlers; rowcount==1 ⇒ this caller won.
        from sqlalchemy import update

        from jim.store.models import A2APaymentAuthRow

        async with self._sm() as s:
            result = await s.execute(
                update(A2APaymentAuthRow)
                .where(
                    A2APaymentAuthRow.task_id == task_id,
                    A2APaymentAuthRow.status == from_status,
                )
                .values(status=to_status, updated_at=_utcnow())
            )
            await s.commit()
            return result.rowcount == 1

    async def list_a2a_auths(self, *, status=None):
        from sqlalchemy import select

        from jim.store.models import A2APaymentAuthRow

        async with self._sm() as s:
            q = select(A2APaymentAuthRow).order_by(A2APaymentAuthRow.created_at)
            if status is not None:
                q = q.where(A2APaymentAuthRow.status == status)
            rows = (await s.execute(q)).scalars().all()
            return [_a2a_auth_dict(r) for r in rows]

    # --- A2A withheld monitor artifacts ---
    async def save_withheld(
        self, *, task_id, monitor_id, severity, as_of, price_usd, payload_ciphertext
    ):
        from jim.store.models import A2AWithheldArtifactRow

        async with self._sm() as s:
            existing = await s.get(A2AWithheldArtifactRow, task_id)
            if existing:
                existing.monitor_id = monitor_id
                existing.severity = severity
                existing.as_of = as_of
                existing.price_usd = price_usd
                existing.payload_ciphertext = payload_ciphertext
            else:
                s.add(
                    A2AWithheldArtifactRow(
                        id=task_id,
                        monitor_id=monitor_id,
                        severity=severity,
                        as_of=as_of,
                        price_usd=price_usd,
                        payload_ciphertext=payload_ciphertext,
                    )
                )
            await s.commit()

    async def get_withheld(self, task_id):
        from jim.store.models import A2AWithheldArtifactRow

        async with self._sm() as s:
            row = await s.get(A2AWithheldArtifactRow, task_id)
            return _withheld_dict(row) if row else None

    async def delete_withheld(self, task_id):
        from jim.store.models import A2AWithheldArtifactRow

        async with self._sm() as s:
            row = await s.get(A2AWithheldArtifactRow, task_id)
            if row:
                await s.delete(row)
                await s.commit()

    # --- A2A push notification configs ---
    async def save_a2a_push_config(self, *, task_id, config_id, config_ciphertext):
        from jim.store.models import A2APushConfigRow

        rid = f"{task_id}:{config_id}"
        async with self._sm() as s:
            existing = await s.get(A2APushConfigRow, rid)
            if existing:
                existing.config_ciphertext = config_ciphertext
            else:
                s.add(
                    A2APushConfigRow(
                        id=rid,
                        task_id=task_id,
                        config_id=config_id,
                        config_ciphertext=config_ciphertext,
                    )
                )
            await s.commit()

    async def get_a2a_push_configs(self, task_id):
        from sqlalchemy import select

        from jim.store.models import A2APushConfigRow

        async with self._sm() as s:
            rows = (
                (
                    await s.execute(
                        select(A2APushConfigRow)
                        .where(A2APushConfigRow.task_id == task_id)
                        .order_by(A2APushConfigRow.created_at)
                    )
                )
                .scalars()
                .all()
            )
            return [_push_config_dict(r) for r in rows]

    async def delete_a2a_push_config(self, task_id, config_id):
        from jim.store.models import A2APushConfigRow

        async with self._sm() as s:
            row = await s.get(A2APushConfigRow, f"{task_id}:{config_id}")
            if row:
                await s.delete(row)
                await s.commit()

    # --- A2A push dead letters (append-only) ---
    async def record_push_deadletter(
        self, *, task_id, config_id, event_type, attempts, last_error, last_status_code
    ):
        from jim.store.models import A2APushDeadLetterRow

        async with self._sm() as s:
            s.add(
                A2APushDeadLetterRow(
                    task_id=task_id,
                    config_id=config_id,
                    event_type=event_type,
                    attempts=attempts,
                    last_error=last_error,
                    last_status_code=last_status_code,
                )
            )
            await s.commit()

    async def list_push_deadletters(self, *, task_id=None):
        from sqlalchemy import select

        from jim.store.models import A2APushDeadLetterRow

        async with self._sm() as s:
            q = select(A2APushDeadLetterRow).order_by(A2APushDeadLetterRow.created_at)
            if task_id is not None:
                q = q.where(A2APushDeadLetterRow.task_id == task_id)
            rows = (await s.execute(q)).scalars().all()
            return [_deadletter_dict(r) for r in rows]


# --- shared helpers ----------------------------------------------------------


def _record_dict(r) -> dict:
    return {
        "product": r.product,
        "identifier": r.identifier,
        "mode": r.mode,
        "status": r.status,
        "price_out_usd": r.price_out_usd,
        "cost_in_data_usd": r.cost_in_data_usd,
        "cost_inference_usd": r.cost_inference_usd,
        "margin_usd": r.margin_usd,
        "cache_hit": r.cache_hit,
        "attempts": r.attempts,
        "created_at": r.created_at,
    }


def _a2a_auth_dict(r) -> dict:
    return {
        "task_id": r.task_id,
        "kind": r.kind,
        "product": r.product,
        "identifier": r.identifier,
        "mode": r.mode,
        "amount_usd": r.amount_usd,
        "requirements": r.requirements,
        "payload_ciphertext": r.payload_ciphertext,
        "payer": r.payer,
        "status": r.status,
        "expires_at": r.expires_at,
        "tx_hash": r.tx_hash,
        "created_at": r.created_at,
        "updated_at": r.updated_at,
    }


def _withheld_dict(r) -> dict:
    return {
        "task_id": r.id,  # id IS the task_id
        "monitor_id": r.monitor_id,
        "severity": r.severity,
        "as_of": r.as_of,
        "price_usd": r.price_usd,
        "payload_ciphertext": r.payload_ciphertext,
        "created_at": r.created_at,
    }


def _push_config_dict(r) -> dict:
    return {
        "task_id": r.task_id,
        "config_id": r.config_id,
        "config_ciphertext": r.config_ciphertext,
        "created_at": r.created_at,
    }


def _deadletter_dict(r) -> dict:
    return {
        "task_id": r.task_id,
        "config_id": r.config_id,
        "event_type": r.event_type,
        "attempts": r.attempts,
        "last_error": r.last_error,
        "last_status_code": r.last_status_code,
        "created_at": r.created_at,
    }


def _receipt_dict(r) -> dict:
    return {
        "tx_hash": r.tx_hash,
        "payer": r.payer,
        "pay_to": r.pay_to,
        "amount_usdc": r.amount_usdc,
        "network": r.network,
        "path": r.path,
        "product": r.product,
        "identifier": r.identifier,
        "mode": r.mode,
        "status_code": r.status_code,
        "success": r.success,
        "receipt": r.receipt,
        "created_at": r.created_at,
    }


def _receipt_view(r: dict) -> dict:
    return {
        "tx_hash": r["tx_hash"],
        "payer": r["payer"],
        "pay_to": r["pay_to"],
        "amount_usdc": round(r["amount_usdc"], 6),
        "network": r["network"],
        "path": r["path"],
        "product": r["product"],
        "identifier": r["identifier"],
        "mode": r["mode"],
        "status_code": r["status_code"],
        "success": r["success"],
        "created_at": r["created_at"].isoformat()
        if hasattr(r["created_at"], "isoformat")
        else r["created_at"],
    }


def _summarize_receipts(receipts: list[dict]) -> dict:
    """Settlement-side rollup for the admin audit view: settled revenue, the
    set of distinct buyer addresses, and a per-product breakdown. Only
    successful settlements count toward revenue."""
    settled = [r for r in receipts if r.get("success")]
    revenue = sum(r.get("amount_usdc", 0.0) for r in settled)
    buyers: dict[str, dict] = {}
    by_product: dict[str, dict] = {}
    for r in settled:
        payer = (r.get("payer") or "unknown").lower()
        b = buyers.setdefault(payer, {"address": payer, "payments": 0, "spent_usdc": 0.0})
        b["payments"] += 1
        b["spent_usdc"] = round(b["spent_usdc"] + r.get("amount_usdc", 0.0), 6)
        prod = r.get("product") or "other"
        p = by_product.setdefault(prod, {"product": prod, "payments": 0, "revenue_usdc": 0.0})
        p["payments"] += 1
        p["revenue_usdc"] = round(p["revenue_usdc"] + r.get("amount_usdc", 0.0), 6)
    top_buyers = sorted(buyers.values(), key=lambda b: b["spent_usdc"], reverse=True)
    return {
        "settlements": len(settled),
        "total_receipts": len(receipts),
        "revenue_usdc": round(revenue, 6),
        "unique_buyers": len(buyers),
        "avg_payment_usdc": round(revenue / len(settled), 6) if settled else 0.0,
        "by_product": sorted(by_product.values(), key=lambda p: p["revenue_usdc"], reverse=True),
        "top_buyers": top_buyers[:10],
    }


def _query_view(q: dict) -> dict:
    return {
        "product": q["product"],
        "identifier": q["identifier"],
        "status": q["status"],
        "price_out_usd": round(q["price_out_usd"], 6),
        "cost_in_data_usd": round(q["cost_in_data_usd"], 6),
        "cost_inference_usd": round(q["cost_inference_usd"], 6),
        "margin_usd": round(q["margin_usd"], 6),
        "cache_hit": q["cache_hit"],
        "created_at": q["created_at"].isoformat()
        if hasattr(q["created_at"], "isoformat")
        else q["created_at"],
    }


def _summarize_monitor_runs(runs: list[dict]) -> dict:
    """Monitor economics: most polls are quiet (free); updates are the billable
    events. ``inference_saved_usd`` estimates what the materiality gate avoided by
    *not* writing on quiet polls (quiet polls × avg inference per real update)."""
    total = len(runs)
    material = [r for r in runs if r.get("material")]
    quiet = [r for r in runs if r.get("status") == "quiet"]
    n_mat = len(material)
    revenue = sum(r.get("price_out_usd", 0.0) for r in material)
    data = sum(r.get("cost_in_data_usd", 0.0) for r in runs)
    inf = sum(r.get("cost_inference_usd", 0.0) for r in runs)
    margin = sum(r.get("margin_usd", 0.0) for r in runs)
    avg_inf_per_update = (sum(r.get("cost_inference_usd", 0.0) for r in material) / n_mat) if n_mat else 0.0
    return {
        "total_runs": total,
        "updates_delivered": n_mat,
        "quiet_runs": len(quiet),
        "baseline_runs": sum(1 for r in runs if r.get("status") == "baseline"),
        "error_runs": sum(1 for r in runs if r.get("status") == "error"),
        "materiality_rate": round(n_mat / total, 4) if total else 0.0,
        "revenue_usd": round(revenue, 6),
        "data_cost_usd": round(data, 6),
        "inference_cost_usd": round(inf, 6),
        "total_margin_usd": round(margin, 6),
        "inference_saved_usd": round(len(quiet) * avg_inf_per_update, 6),
    }


def _summarize_trust(events: list[dict]) -> dict[str, dict]:
    """Per-source trust rollup: gate pass/fail counts + Laplace-smoothed score.

    The score IS the routing signal (jim.interop.trust): a peer below the trust
    floor is refused on the buy path; the dashboard surfaces the same numbers.
    """
    from jim.interop.trust import laplace_score

    by_source: dict[str, dict] = {}
    for e in events:
        row = by_source.setdefault(
            e["source"], {"source": e["source"], "ok": 0, "fail": 0, "last_event_at": None}
        )
        row["ok" if e.get("ok") else "fail"] += 1
        ts = e.get("created_at")
        ts = ts.isoformat() if hasattr(ts, "isoformat") else ts
        if ts and (row["last_event_at"] is None or ts > row["last_event_at"]):
            row["last_event_at"] = ts
    for row in by_source.values():
        row["score"] = round(laplace_score(row["ok"], row["fail"]), 4)
    return by_source


def _summarize(queries: list[dict]) -> dict:
    billable = [q for q in queries if q["status"] == "ok"]
    n = len(billable)
    rev = sum(q["price_out_usd"] for q in billable)
    data = sum(q["cost_in_data_usd"] for q in billable)
    inf = sum(q["cost_inference_usd"] for q in billable)
    margin = rev - data - inf
    hits = sum(1 for q in billable if q["cache_hit"])
    return {
        "billable_queries": n,
        "total_queries": len(queries),
        "revenue_usd": round(rev, 6),
        "data_cost_usd": round(data, 6),
        "inference_cost_usd": round(inf, 6),
        "total_margin_usd": round(margin, 6),
        "avg_margin_usd": round(margin / n, 6) if n else 0.0,
        "margin_pct": round(margin / rev * 100, 2) if rev else 0.0,
        "cache_hit_rate": round(hits / n, 4) if n else 0.0,
    }


# --- factory -----------------------------------------------------------------

_store: Store | None = None


def get_store() -> Store:
    global _store
    if _store is None:
        settings = get_settings()
        _store = SqlStore(settings.database_url) if settings.database_url else MemoryStore()
    return _store


def reset_store() -> None:
    """Drop the cached store (tests)."""
    global _store
    _store = None

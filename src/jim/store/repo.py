"""The store: cache lookups, purchase/query recording, margin queries.

One ``Store`` interface, two backends:
  - ``SqlStore``    : Postgres + pgvector (set DATABASE_URL).
  - ``MemoryStore`` : in-process dicts + Python cosine (no infra; tests + dev).

``get_store()`` picks SqlStore when DATABASE_URL is set, else MemoryStore — so
the engine and the dashboard are identical regardless of backend.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Protocol

from jim.config import get_settings
from jim.store.embed import cosine


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
    async def upsert_insight(self, *, key: str, text: str, embedding: list[float]) -> None: ...
    async def search_insights(self, embedding: list[float], k: int = 5) -> list[dict]: ...
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


# --- In-memory backend -------------------------------------------------------


@dataclass
class MemoryStore:
    purchases: dict[tuple[str, str], dict] = field(default_factory=dict)
    queries: list[dict] = field(default_factory=list)
    insights: dict[str, dict] = field(default_factory=dict)
    monitors: dict[str, dict] = field(default_factory=dict)
    monitor_runs: list[dict] = field(default_factory=list)

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

    async def upsert_insight(self, *, key, text, embedding):
        self.insights[key] = {"text": text, "embedding": embedding}

    async def search_insights(self, embedding, k=5):
        scored = [
            {"cache_key": key, "text": v["text"], "score": cosine(embedding, v["embedding"])}
            for key, v in self.insights.items()
        ]
        scored.sort(key=lambda r: r["score"], reverse=True)
        return scored[:k]

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

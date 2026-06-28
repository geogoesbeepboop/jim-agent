"""SQLAlchemy ORM models for the cache + repackage + margin ledger.

Three tables:
  - data_purchases : every x402 datum we bought (the cache; buy once, reuse).
  - query_records  : per research query, the full cost_in / price_out / margin.
  - insights       : derived insights with a pgvector embedding (semantic reuse).
"""

from __future__ import annotations

from datetime import datetime, timezone

from pgvector.sqlalchemy import Vector
from sqlalchemy import JSON, Boolean, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from jim.store.embed import EMBED_DIM


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class DataPurchase(Base):
    """One paid x402 data fetch. Cached and reused until it expires."""

    __tablename__ = "data_purchases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(64), index=True)  # "thegraph", ...
    cache_key: Mapped[str] = mapped_column(String(256), index=True)
    url: Mapped[str] = mapped_column(Text)
    network: Mapped[str] = mapped_column(String(32))
    cost_usd: Mapped[float] = mapped_column(Float)  # what we paid (cost_in)
    tx_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    payload: Mapped[dict] = mapped_column(JSON)  # the data we bought
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class QueryRecord(Base):
    """One research query's economics. This is what the margin dashboard reads."""

    __tablename__ = "query_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    product: Mapped[str] = mapped_column(String(32), index=True)  # "fundamentals"|"token"
    identifier: Mapped[str] = mapped_column(String(64), index=True)  # ticker/token
    mode: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16))  # ok|rejected|error
    price_out_usd: Mapped[float] = mapped_column(Float)  # what the customer paid
    cost_in_data_usd: Mapped[float] = mapped_column(Float)  # x402 data spend
    cost_inference_usd: Mapped[float] = mapped_column(Float)  # LLM spend
    margin_usd: Mapped[float] = mapped_column(Float)  # price_out - data - inference
    cache_hit: Mapped[bool] = mapped_column(Boolean, default=False)
    attempts: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class Insight(Base):
    """A derived insight, embedded for semantic reuse / popular-ticker precompute."""

    __tablename__ = "insights"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    cache_key: Mapped[str] = mapped_column(String(256), unique=True, index=True)
    text: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBED_DIM))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class MonitorRow(Base):
    """A saved monitor (Phase 4). The full directive + rolling state lives in
    ``data`` (Monitor.to_row()); ``enabled`` + ``next_run_at`` are surfaced as
    columns so the scheduler can query for due monitors cheaply."""

    __tablename__ = "monitors"

    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    product: Mapped[str] = mapped_column(String(32), index=True)
    identifier: Mapped[str] = mapped_column(String(64), index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    next_run_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    data: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class MonitorRunRow(Base):
    """One monitor execution (Phase 4). Economics columns feed the monitor stats;
    the full run (signals + memo) lives in ``data`` (MonitorRun.to_row())."""

    __tablename__ = "monitor_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    monitor_id: Mapped[str] = mapped_column(String(96), index=True)
    identifier: Mapped[str] = mapped_column(String(64), index=True)
    product: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16), index=True)  # baseline|quiet|material|error
    material: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    severity: Mapped[str] = mapped_column(String(16), default="info")
    n_signals: Mapped[int] = mapped_column(Integer, default=0)
    price_out_usd: Mapped[float] = mapped_column(Float, default=0.0)
    cost_in_data_usd: Mapped[float] = mapped_column(Float, default=0.0)
    cost_inference_usd: Mapped[float] = mapped_column(Float, default=0.0)
    margin_usd: Mapped[float] = mapped_column(Float, default=0.0)
    cache_hit: Mapped[bool] = mapped_column(Boolean, default=False)
    data: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)

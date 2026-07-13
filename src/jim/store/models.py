"""SQLAlchemy ORM models for the cache + repackage + margin ledger.

Tables:
  - data_purchases   : every x402 datum we bought (the cache; buy once, reuse).
  - query_records    : per research query, the full cost_in / price_out / margin.
  - insights         : derived insights with a pgvector embedding (semantic reuse).
  - payment_receipts : the on-chain settlement audit log — one row per x402
                       payment that settled at our paywall (buyer address + tx
                       hash + amount). Distinct from query_records: this is the
                       *revenue/settlement* side (who paid us, on which tx),
                       whereas query_records is the *economics* side (margin).
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


class PaymentReceipt(Base):
    """One settled x402 payment at our paywall — the on-chain audit trail.

    Populated by :class:`jim.seller.audit.PaymentAuditMiddleware` from the
    ``PAYMENT-RESPONSE`` settlement header, so it records what *actually settled*
    (payer, tx hash, amount) rather than what we intended to charge. Append-only:
    an audit log is never mutated."""

    __tablename__ = "payment_receipts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tx_hash: Mapped[str | None] = mapped_column(String(128), index=True)  # settlement tx
    payer: Mapped[str | None] = mapped_column(String(64), index=True)  # buyer address
    pay_to: Mapped[str | None] = mapped_column(String(64), nullable=True)  # our address
    amount_usdc: Mapped[float] = mapped_column(Float, default=0.0)  # settled USDC
    network: Mapped[str] = mapped_column(String(32))  # CAIP-2
    path: Mapped[str] = mapped_column(String(128), index=True)  # request path
    product: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    identifier: Mapped[str | None] = mapped_column(String(64), nullable=True)  # ticker/token
    mode: Mapped[str | None] = mapped_column(String(16), nullable=True)
    status_code: Mapped[int] = mapped_column(Integer, default=200)
    success: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    receipt: Mapped[dict] = mapped_column(JSON)  # the raw decoded settle response
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, index=True
    )


class MemoCacheEntry(Base):
    """A synthesized memo cached for exact reuse (research-quality memo cache).

    Keyed by ``{product}:{identifier}:{mode}`` and stamped with the snapshot
    ``fingerprint`` it was written from. A later query reuses it only when the
    freshly-gathered snapshot fingerprint matches and the entry is within TTL —
    so identical repeat queries skip synthesis entirely. Upsert-by-key: one row
    per key, overwritten when the data changes."""

    __tablename__ = "memo_cache"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    cache_key: Mapped[str] = mapped_column(String(256), unique=True, index=True)
    fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    memo: Mapped[str] = mapped_column(Text)
    debate: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


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


class SourceTrustEvent(Base):
    """One gate-outcome attribution for one source (Phase 7 trust ledger).

    Append-only: every gated run credits (``ok=True``) or debits (``ok=False``)
    the sources whose facts it used, per the deterministic attribution rule in
    ``jim.interop.trust``. A source's trust score is the Laplace-smoothed
    pass-rate over its events — reputation by verification, auditable row by
    row like every other ledger in jim."""

    __tablename__ = "source_trust_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(96), index=True)  # "fundamentals", "peer:x", ...
    ok: Mapped[bool] = mapped_column(Boolean, index=True)
    context: Mapped[str] = mapped_column(String(160))  # "fundamentals:AAPL", ...
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, index=True
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


# --- A2A 1.0: durable paid tasks (see docs/adr/0010) -------------------------


class A2APaymentAuthRow(Base):
    """One payment authorization for an A2A durable task — the verify-then-settle-
    once record (ADR-0008 extended to A2A).

    ``requirements`` pins the *exact* PaymentRequirements we advertised so a later
    submitted payload can be checked against it (price-swap defense). The signed
    ``PaymentPayload`` is persisted ONLY as ``payload_ciphertext`` (Fernet) — it
    is a bearer settlement instrument and never lives in plaintext. ``status`` is
    the state machine (required→verified→settling→settled | discarded | expired |
    settle_failed); the settle-once transition rides a compare-and-swap on it."""

    __tablename__ = "a2a_payment_auths"

    task_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    kind: Mapped[str] = mapped_column(String(32))  # research|monitor_activation|monitor_release
    product: Mapped[str] = mapped_column(String(32))
    identifier: Mapped[str] = mapped_column(String(80))
    mode: Mapped[str] = mapped_column(String(16))
    amount_usd: Mapped[float] = mapped_column(Float)
    requirements: Mapped[dict] = mapped_column(JSON)  # the advertised PaymentRequirements
    payload_ciphertext: Mapped[str | None] = mapped_column(Text, nullable=True)  # Fernet(PaymentPayload)
    payer: Mapped[str | None] = mapped_column(String(80), nullable=True)
    status: Mapped[str] = mapped_column(String(16), index=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    tx_hash: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class A2AWithheldArtifactRow(Base):
    """A monitor update synthesized but WITHHELD pending payment (Phase 6 / A2A).

    This is the only place pre-payment memo content may live, and it lives here
    encrypted (``payload_ciphertext``). One withheld artifact per task at a time,
    so ``id`` IS the task_id. Metadata columns (severity/as_of/price) are the most
    an unpaid surface may read; the memo itself is only decryptable on release."""

    __tablename__ = "a2a_withheld_artifacts"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # == task_id
    monitor_id: Mapped[str] = mapped_column(String(64), index=True)
    severity: Mapped[str] = mapped_column(String(16))
    as_of: Mapped[str | None] = mapped_column(String(64), nullable=True)
    price_usd: Mapped[float] = mapped_column(Float)
    payload_ciphertext: Mapped[str] = mapped_column(Text)  # Fernet(memo payload)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class A2APushConfigRow(Base):
    """A push-notification config for a task (Phase 5 / A2A). URL + token + auth
    are encrypted together as one ``config_ciphertext`` blob.

    Composite uniqueness on (task_id, config_id) is realized as a synthetic PK
    ``id = f"{task_id}:{config_id}"`` so both store backends stay trivial."""

    __tablename__ = "a2a_push_configs"

    id: Mapped[str] = mapped_column(String(160), primary_key=True)  # f"{task_id}:{config_id}"
    task_id: Mapped[str] = mapped_column(String(64), index=True)
    config_id: Mapped[str] = mapped_column(String(64))
    config_ciphertext: Mapped[str] = mapped_column(Text)  # Fernet(url+token+auth)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class A2APushDeadLetterRow(Base):
    """One exhausted push delivery (Phase 5 / A2A). Append-only audit — like every
    other ledger in jim, a row is never mutated.

    Deliberately carries NO event body: a dead letter records that a delivery to
    ``config_id`` for ``task_id`` failed after ``attempts`` tries (with the last
    error / HTTP status), but must not become a second, unencrypted copy of a paid
    artifact."""

    __tablename__ = "a2a_push_deadletters"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(String(64), index=True)
    config_id: Mapped[str] = mapped_column(String(64))
    event_type: Mapped[str] = mapped_column(String(32))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, index=True
    )

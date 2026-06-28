"""Data model for continuous monitors (Phase 4 — the "motley crew").

A :class:`Monitor` is a saved, scheduled research directive: re-run product P on
identifier I every N seconds, diff the fresh facts against the last baseline, and
let a *deterministic* crew of triggers decide whether anything material changed.
Only a material change pays for an LLM update memo and pushes to subscribers.

Everything here is plain, JSON-serializable data so the store stays decoupled
from the engine: a :class:`Monitor` round-trips through :meth:`Monitor.to_row` /
:meth:`Monitor.from_row` (the dict shape the store persists).
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

# Severity ladder. The materiality gate publishes signals at or above a floor,
# and the highest signal severity sets the update's overall severity.
SEVERITIES = ("info", "notable", "critical")


def severity_rank(severity: str) -> int:
    try:
        return SEVERITIES.index(severity)
    except ValueError:
        return 0


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _parse_dt(value: str | datetime | None) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value)


def new_monitor_id(product: str, identifier: str) -> str:
    """A short, stable-ish id: product-identifier-<6 hex of a time salt>."""
    salt = hashlib.sha1(f"{product}:{identifier}:{_utcnow().isoformat()}".encode()).hexdigest()[:6]
    return f"{product[:4]}-{identifier.lower()}-{salt}"


@dataclass
class TriggerSpec:
    """One watcher's configuration: a kind plus its (deterministic) params.

    The kind names an evaluator in :mod:`jim.monitors.triggers`; params carry its
    thresholds (e.g. ``{"pct": 5.0}``). Kept as pure data so a monitor serializes
    to JSON and an LLM can *propose* a spec that code validates (propose/dispose).
    """

    kind: str
    params: dict = field(default_factory=dict)

    def to_row(self) -> dict:
        return {"kind": self.kind, "params": dict(self.params)}

    @classmethod
    def from_row(cls, row: dict) -> "TriggerSpec":
        return cls(kind=row["kind"], params=dict(row.get("params") or {}))


@dataclass
class Signal:
    """A single deterministic finding emitted by a trigger over a snapshot diff.

    Every signal is impersonal and traceable: ``citation_ids`` are the ``[C#]``
    facts that back it, so a downstream update memo can cite them and the sourcing
    gate can verify them.
    """

    kind: str  # the trigger kind that produced it
    key: str  # stable dedup/cooldown key, e.g. "threshold:RSI:overbought"
    label: str  # the metric this concerns, e.g. "RSI", "Price", "10-K"
    severity: str  # one of SEVERITIES
    summary: str  # one-line, impersonal, cites [C#] — gate-safe by construction
    citation_ids: list[str] = field(default_factory=list)
    old_value: float | None = None
    new_value: float | None = None
    pct_change: float | None = None
    direction: str | None = None  # up | down | cross_up | cross_down | new

    def to_row(self) -> dict:
        return asdict(self)

    @classmethod
    def from_row(cls, row: dict) -> "Signal":
        return cls(**row)


@dataclass
class Monitor:
    """A scheduled, diff-driven research directive plus its rolling state."""

    id: str
    product: str  # "fundamentals" | "token"
    identifier: str  # "AAPL" / "WETH"
    mode: str = "agent"
    interval_seconds: int = 86_400
    triggers: list[TriggerSpec] = field(default_factory=list)
    channels: list[str] = field(default_factory=lambda: ["store"])
    cooldown_seconds: int = 21_600
    severity_floor: str = "info"
    enabled: bool = True
    label: str | None = None  # human label / the natural-language request

    # --- rolling state (updated each run) ---
    baseline: dict = field(default_factory=dict)  # label -> {value, unit, as_of, accession, id}
    cooldowns: dict = field(default_factory=dict)  # signal key -> iso ts last fired
    created_at: datetime = field(default_factory=_utcnow)
    last_run_at: datetime | None = None
    next_run_at: datetime | None = None
    last_status: str | None = None  # status of the most recent run

    def due(self, now: datetime | None = None) -> bool:
        if not self.enabled:
            return False
        if self.next_run_at is None:
            return True
        return self.next_run_at <= (now or _utcnow())

    def to_row(self) -> dict:
        return {
            "id": self.id,
            "product": self.product,
            "identifier": self.identifier,
            "mode": self.mode,
            "interval_seconds": self.interval_seconds,
            "triggers": [t.to_row() for t in self.triggers],
            "channels": list(self.channels),
            "cooldown_seconds": self.cooldown_seconds,
            "severity_floor": self.severity_floor,
            "enabled": self.enabled,
            "label": self.label,
            "baseline": dict(self.baseline),
            "cooldowns": dict(self.cooldowns),
            "created_at": _iso(self.created_at),
            "last_run_at": _iso(self.last_run_at),
            "next_run_at": _iso(self.next_run_at),
            "last_status": self.last_status,
        }

    @classmethod
    def from_row(cls, row: dict) -> "Monitor":
        return cls(
            id=row["id"],
            product=row["product"],
            identifier=row["identifier"],
            mode=row.get("mode", "agent"),
            interval_seconds=int(row.get("interval_seconds", 86_400)),
            triggers=[TriggerSpec.from_row(t) for t in row.get("triggers", [])],
            channels=list(row.get("channels") or ["store"]),
            cooldown_seconds=int(row.get("cooldown_seconds", 21_600)),
            severity_floor=row.get("severity_floor", "info"),
            enabled=bool(row.get("enabled", True)),
            label=row.get("label"),
            baseline=dict(row.get("baseline") or {}),
            cooldowns=dict(row.get("cooldowns") or {}),
            created_at=_parse_dt(row.get("created_at")) or _utcnow(),
            last_run_at=_parse_dt(row.get("last_run_at")),
            next_run_at=_parse_dt(row.get("next_run_at")),
            last_status=row.get("last_status"),
        )


@dataclass
class MonitorRun:
    """The outcome of one monitor execution (what the feed + stats read)."""

    monitor_id: str
    identifier: str
    product: str
    status: str  # baseline | quiet | material | error
    material: bool = False
    signals: list[Signal] = field(default_factory=list)
    memo: str | None = None
    severity: str = "info"
    delivered_to: list[str] = field(default_factory=list)
    # economics of this run
    price_out_usd: float = 0.0
    cost_in_data_usd: float = 0.0
    cost_inference_usd: float = 0.0
    cache_hit: bool = False
    gate_passed: bool | None = None
    error: str | None = None
    ran_at: datetime = field(default_factory=_utcnow)

    @property
    def margin_usd(self) -> float:
        return round(self.price_out_usd - self.cost_in_data_usd - self.cost_inference_usd, 6)

    def to_row(self) -> dict:
        return {
            "monitor_id": self.monitor_id,
            "identifier": self.identifier,
            "product": self.product,
            "status": self.status,
            "material": self.material,
            "signals": [s.to_row() for s in self.signals],
            "memo": self.memo,
            "severity": self.severity,
            "delivered_to": list(self.delivered_to),
            "price_out_usd": self.price_out_usd,
            "cost_in_data_usd": self.cost_in_data_usd,
            "cost_inference_usd": self.cost_inference_usd,
            "margin_usd": self.margin_usd,
            "cache_hit": self.cache_hit,
            "gate_passed": self.gate_passed,
            "error": self.error,
            "ran_at": _iso(self.ran_at),
        }

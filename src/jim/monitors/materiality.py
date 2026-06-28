"""The materiality gate — deterministic "is there anything worth saying?".

The sourcing gate decides what may *ship*; this gate decides whether to *speak at
all*. Given the crew's raw signals plus the monitor's cooldown memory, it applies
two reproducible filters — a severity floor and a per-signal cooldown — and
returns the set to publish. No model: a quiet poll costs no inference and pushes
nothing, which is exactly the economic point of monitoring (most polls are quiet).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from jim.monitors.models import Signal, severity_rank


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class MaterialityVerdict:
    material: bool
    severity: str  # highest severity among published, else "info"
    published: list[Signal] = field(default_factory=list)
    suppressed: list[Signal] = field(default_factory=list)
    cooldowns: dict = field(default_factory=dict)  # updated key -> iso ts


def assess(
    signals: list[Signal],
    *,
    severity_floor: str = "info",
    cooldown_seconds: int = 0,
    cooldowns: dict | None = None,
    now: datetime | None = None,
) -> MaterialityVerdict:
    """Filter signals by severity floor + cooldown; report whether any survive."""
    now = now or _utcnow()
    cooldowns = dict(cooldowns or {})
    floor = severity_rank(severity_floor)

    published: list[Signal] = []
    suppressed: list[Signal] = []
    for sig in signals:
        if severity_rank(sig.severity) < floor:
            suppressed.append(sig)
            continue
        last = cooldowns.get(sig.key)
        if last and cooldown_seconds > 0:
            try:
                age = (now - datetime.fromisoformat(last)).total_seconds()
            except ValueError:
                age = cooldown_seconds + 1  # unparseable → treat as expired
            if age < cooldown_seconds:
                suppressed.append(sig)
                continue
        published.append(sig)
        cooldowns[sig.key] = now.isoformat()

    severity = "info"
    for sig in published:
        if severity_rank(sig.severity) > severity_rank(severity):
            severity = sig.severity

    return MaterialityVerdict(
        material=bool(published),
        severity=severity,
        published=published,
        suppressed=suppressed,
        cooldowns=cooldowns,
    )

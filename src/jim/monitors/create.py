"""Monitor construction — the one place CLI and API build a :class:`Monitor`.

Three ways to specify the watch crew, in precedence order:
  1. explicit ``watch`` mini-specs ("price:5", "rsi:70/30", "filing", …),
  2. a natural-language ``describe`` (parsed + validated via :mod:`jim.monitors.nl`),
  3. otherwise the product's default crew.
"""

from __future__ import annotations

from jim.config import get_settings
from jim.monitors.models import Monitor, TriggerSpec, new_monitor_id
from jim.monitors.nl import (
    default_triggers,
    detect_product,
    duration_seconds,
    parse_interval,
    propose_triggers,
)


def parse_watch_spec(spec: str, product: str) -> TriggerSpec | None:
    """Parse one ``--watch`` mini-spec into a TriggerSpec (None if unrecognized)."""
    s = get_settings()
    parts = spec.split(":")
    head = parts[0].strip().lower()
    price_label = "Price (USD)" if product == "token" else "Price"

    if head in ("price", "price_move"):
        pct = float(parts[1]) if len(parts) > 1 and parts[1] else s.monitor_price_move_pct
        return TriggerSpec("price_move", {"label": price_label, "pct": pct})
    if head == "rsi":
        above, below = s.monitor_rsi_overbought, s.monitor_rsi_oversold
        if len(parts) > 1 and parts[1]:
            bounds = parts[1].split("/")
            above = float(bounds[0]) if bounds[0] else above
            below = float(bounds[1]) if len(bounds) > 1 and bounds[1] else below
        return TriggerSpec("threshold", {"label": "RSI (14-day)", "above": above, "below": below})
    if head in ("ma", "macross", "ma_cross"):
        return TriggerSpec(
            "ma_cross", {"fast": "50-day moving average", "slow": "200-day moving average"}
        )
    if head in ("filing", "new_filing"):
        return TriggerSpec("new_filing", {})
    if head in ("metric", "metric_change") and len(parts) >= 2:
        label = parts[1]
        pct = float(parts[2]) if len(parts) > 2 and parts[2] else s.monitor_metric_change_pct
        return TriggerSpec("metric_change", {"labels": [label], "pct": pct})
    if head == "threshold" and len(parts) >= 3:
        label = parts[1]
        params: dict = {"label": label}
        if parts[2]:
            params["above"] = float(parts[2])
        if len(parts) > 3 and parts[3]:
            params["below"] = float(parts[3])
        return TriggerSpec("threshold", params)
    return None


async def create_monitor(
    identifier: str,
    *,
    product: str | None = None,
    mode: str | None = None,
    every: str | int | None = None,
    watch: list[str] | None = None,
    describe: str | None = None,
    channels: list[str] | None = None,
    severity_floor: str | None = None,
    cooldown: str | int | None = None,
) -> Monitor:
    """Build (but do not persist) a Monitor from CLI/API-style arguments."""
    settings = get_settings()
    product = product or (detect_product(describe) if describe else "fundamentals")
    identifier = identifier.upper()

    # interval
    if every is not None:
        interval = int(every) if isinstance(every, int) else duration_seconds(str(every))
    elif describe and parse_interval(describe):
        interval = parse_interval(describe)
    else:
        interval = settings.monitor_default_interval_seconds

    # cooldown
    if cooldown is not None:
        cooldown_s = int(cooldown) if isinstance(cooldown, int) else duration_seconds(str(cooldown))
    else:
        cooldown_s = settings.monitor_cooldown_seconds

    # triggers
    if watch:
        triggers = [t for t in (parse_watch_spec(w, product) for w in watch) if t]
        triggers = triggers or default_triggers(product)
    elif describe:
        triggers, _ = await propose_triggers(describe, product)
    else:
        triggers = default_triggers(product)

    return Monitor(
        id=new_monitor_id(product, identifier),
        product=product,
        identifier=identifier,
        mode=mode or settings.monitor_default_mode,
        interval_seconds=interval,
        triggers=triggers,
        channels=channels or ["console"],
        cooldown_seconds=cooldown_s,
        severity_floor=severity_floor or "info",
        label=describe,
    )

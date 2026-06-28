"""The motley crew — deterministic watchers over a snapshot diff.

Each trigger is a pure function of ``(params, diff, snapshot)`` that emits zero or
more :class:`~jim.monitors.models.Signal`s. There is **no model here**: just like
the sourcing gate decides what may *ship*, the crew decides what is *worth
saying*, reproducibly. A monitor wires up a list of these, and the materiality
gate downstream filters them by severity + cooldown.

Crew roster (by ``kind``):
  - ``price_move``   : a watched price moved ≥ X% since the baseline.
  - ``metric_change``: any named metric moved ≥ X% (or ≥ an absolute amount).
  - ``threshold``    : a metric crossed a level (RSI 70/30, P/E > N, …), with
                       direction (only fires on the *crossing*, not while parked).
  - ``ma_cross``     : a fast MA crossed its slow MA (golden / death cross).
  - ``new_filing``   : a new SEC accession appeared / the reporting date advanced.

Signal ``summary`` text is human-facing (feed / webhook / LLM context) and may
quote deltas; the *gate-safe* update prose is rebuilt from each signal's
structured fields elsewhere (see :mod:`jim.monitors.update`).
"""

from __future__ import annotations

from typing import Callable

from jim.monitors.diff import FactDelta, SnapshotDiff
from jim.monitors.models import Signal
from jim.research.facts import Snapshot, _fmt_value

Evaluator = Callable[[dict, SnapshotDiff, Snapshot], list[Signal]]


# --- helpers ----------------------------------------------------------------


def _severity(magnitude: float, base: float, *, hi_mult: float = 2.0) -> str:
    """notable at ≥ base, critical at ≥ base·hi_mult."""
    if base <= 0:
        return "notable"
    if magnitude >= base * hi_mult:
        return "critical"
    return "notable"


def _arrow(delta: FactDelta) -> str:
    if delta.abs_change is None or delta.abs_change == 0:
        return "unchanged"
    return "rose" if delta.abs_change > 0 else "fell"


def _pct_text(delta: FactDelta) -> str:
    p = delta.pct_change
    return f"{p:+.1f}%" if p is not None else "n/a"


# --- evaluators -------------------------------------------------------------


def _eval_price_move(params: dict, diff: SnapshotDiff, snapshot: Snapshot) -> list[Signal]:
    label = params.get("label", "Price")
    pct = float(params.get("pct", 5.0))
    d = diff.get(label)
    if d is None or d.is_new or d.pct_change is None:
        return []
    if abs(d.pct_change) < pct:
        return []
    sev = _severity(abs(d.pct_change), pct)
    return [
        Signal(
            kind="price_move",
            key=f"price_move:{label}",
            label=label,
            severity=sev,
            summary=(
                f"{label} {_arrow(d)} {_pct_text(d)} to "
                f"{_fmt_value(d.new_value, d.unit)} [{d.fact_id}]."
            ),
            citation_ids=[d.fact_id],
            old_value=d.old_value,
            new_value=d.new_value,
            pct_change=d.pct_change,
            direction="up" if d.abs_change and d.abs_change > 0 else "down",
        )
    ]


def _eval_metric_change(params: dict, diff: SnapshotDiff, snapshot: Snapshot) -> list[Signal]:
    pct = float(params.get("pct", 10.0))
    abs_min = params.get("abs")  # optional absolute floor
    labels = params.get("labels") or ([params["label"]] if params.get("label") else None)
    candidates = labels if labels is not None else list(diff.deltas.keys())

    signals: list[Signal] = []
    for label in candidates:
        d = diff.get(label)
        if d is None or d.is_new or d.abs_change is None:
            continue
        big_pct = d.pct_change is not None and abs(d.pct_change) >= pct
        big_abs = abs_min is not None and abs(d.abs_change) >= float(abs_min)
        if not (big_pct or big_abs):
            continue
        sev = _severity(abs(d.pct_change or pct), pct)
        signals.append(
            Signal(
                kind="metric_change",
                key=f"metric_change:{label}",
                label=label,
                severity=sev,
                summary=(
                    f"{label} {_arrow(d)} {_pct_text(d)} to "
                    f"{_fmt_value(d.new_value, d.unit)} [{d.fact_id}]."
                ),
                citation_ids=[d.fact_id],
                old_value=d.old_value,
                new_value=d.new_value,
                pct_change=d.pct_change,
                direction="up" if d.abs_change > 0 else "down",
            )
        )
    return signals


def _eval_threshold(params: dict, diff: SnapshotDiff, snapshot: Snapshot) -> list[Signal]:
    """Fire only on the crossing of ``above``/``below`` — direction-aware.

    A level already breached at the baseline does NOT re-fire (that's what makes
    this a *signal*, not a perpetual klaxon). With no baseline value we treat a
    breached level as a fresh cross so a freshly-added metric still alerts once.
    """
    label = params.get("label")
    if not label:
        return []
    d = diff.get(label)
    if d is None:
        return []
    above = params.get("above")
    below = params.get("below")
    new, old = d.new_value, d.old_value
    signals: list[Signal] = []

    if above is not None and new >= float(above) and (old is None or old < float(above)):
        signals.append(
            Signal(
                kind="threshold",
                key=f"threshold:{label}:above",
                label=label,
                severity="notable",
                summary=(
                    f"{label} crossed above {float(above):g}, now "
                    f"{_fmt_value(new, d.unit)} [{d.fact_id}]."
                ),
                citation_ids=[d.fact_id],
                old_value=old,
                new_value=new,
                direction="cross_up",
            )
        )
    if below is not None and new <= float(below) and (old is None or old > float(below)):
        signals.append(
            Signal(
                kind="threshold",
                key=f"threshold:{label}:below",
                label=label,
                severity="notable",
                summary=(
                    f"{label} crossed below {float(below):g}, now "
                    f"{_fmt_value(new, d.unit)} [{d.fact_id}]."
                ),
                citation_ids=[d.fact_id],
                old_value=old,
                new_value=new,
                direction="cross_down",
            )
        )
    return signals


def _eval_ma_cross(params: dict, diff: SnapshotDiff, snapshot: Snapshot) -> list[Signal]:
    fast_label = params.get("fast", "50-day moving average")
    slow_label = params.get("slow", "200-day moving average")
    f, s = diff.get(fast_label), diff.get(slow_label)
    if f is None or s is None or f.old_value is None or s.old_value is None:
        return []
    was_below = f.old_value <= s.old_value
    now_above = f.new_value > s.new_value
    if was_below and now_above:
        kind_word, key_dir = "golden cross (50-day rose above 200-day)", "golden"
    elif (not was_below) and (not now_above):
        kind_word, key_dir = "death cross (50-day fell below 200-day)", "death"
    else:
        return []
    return [
        Signal(
            kind="ma_cross",
            key=f"ma_cross:{fast_label}:{slow_label}:{key_dir}",
            label=f"{fast_label} / {slow_label}",
            severity="notable",
            summary=(
                f"Moving-average {kind_word}: 50-day now "
                f"{_fmt_value(f.new_value, f.unit)} [{f.fact_id}] vs 200-day "
                f"{_fmt_value(s.new_value, s.unit)} [{s.fact_id}]."
            ),
            citation_ids=[f.fact_id, s.fact_id],
            old_value=f.old_value,
            new_value=f.new_value,
            direction=key_dir,
        )
    ]


def _eval_new_filing(params: dict, diff: SnapshotDiff, snapshot: Snapshot) -> list[Signal]:
    if not diff.new_accessions and not diff.as_of_advanced:
        return []
    acc = diff.new_accessions[0] if diff.new_accessions else None
    detail = f" (accession {acc})" if acc else ""
    when = f" Reporting date advanced to {diff.new_as_of}." if diff.as_of_advanced else ""
    return [
        Signal(
            kind="new_filing",
            key=f"new_filing:{acc or diff.new_as_of}",
            label="SEC filing",
            severity="notable",
            summary=f"A new primary-source filing was observed{detail}.{when}",
            citation_ids=[],
            direction="new",
        )
    ]


EVALUATORS: dict[str, Evaluator] = {
    "price_move": _eval_price_move,
    "metric_change": _eval_metric_change,
    "threshold": _eval_threshold,
    "ma_cross": _eval_ma_cross,
    "new_filing": _eval_new_filing,
}


# --- public API -------------------------------------------------------------


def evaluate(spec, diff: SnapshotDiff, snapshot: Snapshot) -> list[Signal]:
    fn = EVALUATORS.get(spec.kind)
    if fn is None:
        return []
    return fn(spec.params, diff, snapshot)


def evaluate_all(specs, diff: SnapshotDiff, snapshot: Snapshot) -> list[Signal]:
    out: list[Signal] = []
    for spec in specs:
        out.extend(evaluate(spec, diff, snapshot))
    return out


def describe(spec) -> str:
    """One-line, human-readable description of a configured trigger."""
    p = spec.params
    if spec.kind == "price_move":
        return f"price move ≥ {p.get('pct', 5.0):g}% on {p.get('label', 'Price')}"
    if spec.kind == "metric_change":
        scope = p.get("label") or (", ".join(p["labels"]) if p.get("labels") else "any metric")
        return f"{scope} change ≥ {p.get('pct', 10.0):g}%"
    if spec.kind == "threshold":
        bits = []
        if p.get("above") is not None:
            bits.append(f"> {float(p['above']):g}")
        if p.get("below") is not None:
            bits.append(f"< {float(p['below']):g}")
        return f"{p.get('label', '?')} crosses {' or '.join(bits) or '(unset)'}"
    if spec.kind == "ma_cross":
        return f"{p.get('fast', '50-day moving average')} × {p.get('slow', '200-day moving average')} cross"
    if spec.kind == "new_filing":
        return "new SEC filing / reporting date advance"
    return spec.kind


def default_triggers(product: str) -> list:
    """A sensible default crew for a product, using config thresholds."""
    from jim.config import get_settings
    from jim.monitors.models import TriggerSpec

    s = get_settings()
    if product == "token":
        price_label = "Price (USD)"
        return [
            TriggerSpec("price_move", {"label": price_label, "pct": s.monitor_price_move_pct}),
            TriggerSpec(
                "metric_change",
                {
                    "labels": ["Liquidity / TVL (USD)", "Cumulative volume (USD)"],
                    "pct": s.monitor_metric_change_pct,
                },
            ),
        ]
    # fundamentals / equities
    return [
        TriggerSpec("price_move", {"label": "Price", "pct": s.monitor_price_move_pct}),
        TriggerSpec(
            "threshold",
            {
                "label": "RSI (14-day)",
                "above": s.monitor_rsi_overbought,
                "below": s.monitor_rsi_oversold,
            },
        ),
        TriggerSpec(
            "ma_cross",
            {"fast": "50-day moving average", "slow": "200-day moving average"},
        ),
        TriggerSpec(
            "metric_change",
            {"labels": ["Diluted EPS", "Net income"], "pct": s.monitor_metric_change_pct},
        ),
        TriggerSpec("new_filing", {}),
    ]

"""The motley crew — each deterministic watcher fires when (and only when) it
should. Offline, no key."""

from __future__ import annotations

from jim.monitors.diff import diff_snapshots, snapshot_to_baseline
from jim.monitors.models import TriggerSpec
from jim.monitors.triggers import evaluate, evaluate_all
from jim.research.facts import INDEX, USD, Fact, Snapshot


def _snap(price=100.0, rsi=50.0, sma50=95.0, sma200=100.0, eps=6.0, *, acc="a", as_of="2025-01-01"):
    return Snapshot(
        ticker="AAPL",
        cik="0000320193",
        entity_name="Apple Inc.",
        as_of=as_of,
        facts=[
            Fact(id="C1", label="Price", value=price, unit=USD, accession=acc),
            Fact(id="C2", label="RSI (14-day)", value=rsi, unit=INDEX),
            Fact(id="C3", label="50-day moving average", value=sma50, unit=USD),
            Fact(id="C4", label="200-day moving average", value=sma200, unit=USD),
            Fact(id="C5", label="Diluted EPS", value=eps, unit="USD/shares", accession=acc),
        ],
    )


def _diff(base_snap, cur_snap):
    return diff_snapshots(snapshot_to_baseline(base_snap), cur_snap)


def test_price_move_fires_and_scales_severity():
    spec = TriggerSpec("price_move", {"label": "Price", "pct": 5.0})
    # +6% → notable
    sig = evaluate(spec, _diff(_snap(price=100), _snap(price=106)), _snap(price=106))
    assert len(sig) == 1 and sig[0].severity == "notable" and sig[0].direction == "up"
    assert "[C1]" in sig[0].summary
    # +12% → critical (≥ 2× threshold)
    sig = evaluate(spec, _diff(_snap(price=100), _snap(price=112)), _snap(price=112))
    assert sig[0].severity == "critical"
    # +3% → nothing
    assert evaluate(spec, _diff(_snap(price=100), _snap(price=103)), _snap(price=103)) == []


def test_threshold_only_fires_on_the_crossing():
    spec = TriggerSpec("threshold", {"label": "RSI (14-day)", "above": 70, "below": 30})
    # 55 → 72 crosses above
    sig = evaluate(spec, _diff(_snap(rsi=55), _snap(rsi=72)), _snap(rsi=72))
    assert len(sig) == 1 and sig[0].direction == "cross_up"
    # 72 → 75 already above: no re-fire
    assert evaluate(spec, _diff(_snap(rsi=72), _snap(rsi=75)), _snap(rsi=75)) == []
    # 35 → 28 crosses below
    sig = evaluate(spec, _diff(_snap(rsi=35), _snap(rsi=28)), _snap(rsi=28))
    assert len(sig) == 1 and sig[0].direction == "cross_down"


def test_ma_cross_golden_and_death():
    spec = TriggerSpec(
        "ma_cross", {"fast": "50-day moving average", "slow": "200-day moving average"}
    )
    # fast was below, now above → golden
    sig = evaluate(
        spec,
        _diff(_snap(sma50=95, sma200=100), _snap(sma50=101, sma200=100)),
        _snap(sma50=101, sma200=100),
    )
    assert len(sig) == 1 and sig[0].direction == "golden"
    # fast was above, now below → death
    sig = evaluate(
        spec,
        _diff(_snap(sma50=105, sma200=100), _snap(sma50=99, sma200=100)),
        _snap(sma50=99, sma200=100),
    )
    assert sig[0].direction == "death"
    # stayed above → nothing
    assert (
        evaluate(
            spec,
            _diff(_snap(sma50=105, sma200=100), _snap(sma50=110, sma200=100)),
            _snap(sma50=110, sma200=100),
        )
        == []
    )


def test_metric_change_pct_and_abs():
    pct = TriggerSpec("metric_change", {"labels": ["Diluted EPS"], "pct": 10.0})
    sig = evaluate(pct, _diff(_snap(eps=6.0), _snap(eps=6.9)), _snap(eps=6.9))  # +15%
    assert len(sig) == 1 and "Diluted EPS" in sig[0].label
    assert evaluate(pct, _diff(_snap(eps=6.0), _snap(eps=6.3)), _snap(eps=6.3)) == []  # +5%
    abs_spec = TriggerSpec("metric_change", {"labels": ["Diluted EPS"], "pct": 999, "abs": 0.5})
    assert len(evaluate(abs_spec, _diff(_snap(eps=6.0), _snap(eps=6.6)), _snap(eps=6.6))) == 1


def test_new_filing_trigger():
    spec = TriggerSpec("new_filing", {})
    base, cur = _snap(acc="old", as_of="2025-01-01"), _snap(acc="new", as_of="2025-02-01")
    sig = evaluate(spec, _diff(base, cur), cur)
    assert len(sig) == 1 and sig[0].kind == "new_filing" and sig[0].citation_ids == []
    # nothing new → silent
    assert evaluate(spec, _diff(base, _snap(acc="old", as_of="2025-01-01")), base) == []


def test_unknown_kind_is_silent():
    assert evaluate(TriggerSpec("nope", {}), _diff(_snap(), _snap()), _snap()) == []


def test_evaluate_all_unions_signals():
    specs = [
        TriggerSpec("price_move", {"label": "Price", "pct": 5.0}),
        TriggerSpec("threshold", {"label": "RSI (14-day)", "above": 70}),
    ]
    cur = _snap(price=120, rsi=72)
    sigs = evaluate_all(specs, _diff(_snap(price=100, rsi=55), cur), cur)
    kinds = {s.kind for s in sigs}
    assert kinds == {"price_move", "threshold"}

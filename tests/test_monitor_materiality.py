"""The materiality gate (severity floor + cooldown) and the impersonal guard.
Both deterministic, both offline."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from jim.monitors.impersonal import check_impersonal
from jim.monitors.materiality import assess
from jim.monitors.models import Signal
from jim.research.synthesize import DISCLAIMER


def _sig(key, severity="notable"):
    return Signal(kind="x", key=key, label="L", severity=severity, summary="s", citation_ids=["C1"])


def test_severity_floor_filters():
    sigs = [_sig("a", "info"), _sig("b", "notable"), _sig("c", "critical")]
    v = assess(sigs, severity_floor="notable")
    keys = {s.key for s in v.published}
    assert keys == {"b", "c"} and v.severity == "critical" and v.material


def test_quiet_when_nothing_clears_floor():
    v = assess([_sig("a", "info")], severity_floor="notable")
    assert not v.material and v.published == [] and v.severity == "info"


def test_cooldown_suppresses_repeat_then_expires():
    now = datetime(2025, 1, 1, 12, 0, tzinfo=timezone.utc)
    first = assess([_sig("price_move:Price")], cooldown_seconds=3600, cooldowns={}, now=now)
    assert first.material and "price_move:Price" in first.cooldowns

    # 30 min later → still in cooldown → suppressed
    soon = now + timedelta(minutes=30)
    again = assess(
        [_sig("price_move:Price")], cooldown_seconds=3600, cooldowns=first.cooldowns, now=soon
    )
    assert not again.material and len(again.suppressed) == 1

    # 2h later → cooldown expired → fires again
    later = now + timedelta(hours=2)
    third = assess(
        [_sig("price_move:Price")], cooldown_seconds=3600, cooldowns=again.cooldowns, now=later
    )
    assert third.material


def test_cooldown_zero_never_suppresses():
    v = assess([_sig("k")], cooldown_seconds=0, cooldowns={"k": "2025-01-01T00:00:00+00:00"})
    assert v.material


def test_impersonal_guard_flags_advice_and_second_person():
    bad = "You should buy now — this is great for your portfolio. Strong buy; our recommendation stands."
    res = check_impersonal(bad)
    assert not res.passed
    reasons = " ".join(res.violations).lower()
    assert "second-person" in reasons and "advice" in reasons


def test_impersonal_guard_passes_clean_cited_memo_with_disclaimer():
    good = f"Revenue was $394.3 billion [C1]. Net margin held near 25.0% [C2].\n{DISCLAIMER}"
    assert check_impersonal(good).passed


def test_impersonal_guard_ignores_words_inside_other_words():
    # "buyback" must not trip the buy rule; the disclaimer's "recommendation" is stripped.
    text = f"The company announced a buyback program of $90.0 billion [C1].\n{DISCLAIMER}"
    assert check_impersonal(text).passed

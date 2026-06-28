"""Natural-language → spec parsing + the propose/dispose validation. Offline."""

from __future__ import annotations

import pytest

from jim.config import get_settings
from jim.monitors.create import create_monitor, parse_watch_spec
from jim.monitors.nl import (
    deterministic_triggers,
    detect_product,
    duration_seconds,
    parse_identifier,
    parse_interval,
    propose_triggers,
    validate_triggers,
)


def test_duration_parsing():
    assert duration_seconds("30m") == 1800
    assert duration_seconds("1h") == 3600
    assert duration_seconds("2d") == 172800
    assert duration_seconds("90") == 90
    with pytest.raises(ValueError):
        duration_seconds("soon")


def test_interval_words_and_every_phrase():
    assert parse_interval("check it hourly please") == 3600
    assert parse_interval("every 15 minutes") == 900
    assert parse_interval("every 2 days") == 172800
    assert parse_interval("no cadence here") is None


def test_product_and_identifier_detection():
    assert detect_product("watch the WETH token TVL on-chain") == "token"
    assert detect_product("watch AAPL earnings") == "fundamentals"
    assert parse_identifier("alert me about NVDA RSI") == "NVDA"  # RSI is a stop-word
    assert parse_identifier("no ticker") is None


def test_deterministic_triggers_from_keywords():
    kinds = {
        t.kind for t in deterministic_triggers("big price moves and overbought RSI", "fundamentals")
    }
    assert kinds == {"price_move", "threshold"}
    kinds = {t.kind for t in deterministic_triggers("notify on new 10-K filing", "fundamentals")}
    assert "new_filing" in kinds
    # token TVL/volume map to metric_change on token labels
    specs = deterministic_triggers("watch TVL and volume", "token")
    mc = next(t for t in specs if t.kind == "metric_change")
    assert "Liquidity / TVL (USD)" in mc.params["labels"]


def test_price_pct_is_extracted():
    specs = deterministic_triggers("alert if price moves 8%", "fundamentals")
    pm = next(t for t in specs if t.kind == "price_move")
    assert pm.params["pct"] == 8.0


def test_validate_triggers_drops_unknown_and_clamps():
    raw = [
        {"kind": "price_move", "params": {"pct": 99999}},  # clamped to 1000
        {"kind": "threshold", "params": {"label": "RSI (14-day)", "above": 70}},
        {"kind": "threshold", "params": {"label": "X"}},  # no bound → dropped
        {"kind": "bogus", "params": {}},  # unknown kind → dropped
    ]
    clean = validate_triggers(raw)
    kinds = [t.kind for t in clean]
    assert kinds == ["price_move", "threshold"]
    assert clean[0].params["pct"] == 1000.0


def test_parse_watch_spec_forms():
    assert parse_watch_spec("price:5", "fundamentals").params["pct"] == 5.0
    assert parse_watch_spec("price:8", "token").params["label"] == "Price (USD)"
    rsi = parse_watch_spec("rsi:75/25", "fundamentals")
    assert rsi.params["above"] == 75 and rsi.params["below"] == 25
    assert parse_watch_spec("ma", "fundamentals").kind == "ma_cross"
    assert parse_watch_spec("filing", "fundamentals").kind == "new_filing"
    metric = parse_watch_spec("metric:Revenue:12", "fundamentals")
    assert metric.params["labels"] == ["Revenue"] and metric.params["pct"] == 12.0
    assert parse_watch_spec("garbage", "fundamentals") is None


async def test_propose_triggers_offline_uses_deterministic(monkeypatch):
    monkeypatch.setattr(get_settings(), "anthropic_api_key", None)
    triggers, used_llm = await propose_triggers("price moves and overbought RSI", "fundamentals")
    assert used_llm is False
    assert {t.kind for t in triggers} == {"price_move", "threshold"}


async def test_create_monitor_from_natural_language(monkeypatch):
    monkeypatch.setattr(get_settings(), "anthropic_api_key", None)
    m = await create_monitor("NVDA", describe="alert on big price moves every 6 hours")
    assert m.product == "fundamentals" and m.identifier == "NVDA"
    assert m.interval_seconds == 6 * 3600
    assert any(t.kind == "price_move" for t in m.triggers)

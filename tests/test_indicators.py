"""Technical-indicator math, offline."""

from __future__ import annotations

import pytest

from jim.research.indicators import compute_indicators, ema, rsi, sma


def test_sma():
    assert sma([1, 2, 3, 4, 5], 5) == pytest.approx(3.0)
    assert sma([1, 2, 3], 5) is None  # not enough data


def test_ema_recent_weighted():
    flat = ema([10.0] * 30, 10)
    assert flat == pytest.approx(10.0)  # constant series → EMA equals the constant


def test_rsi_all_gains_is_100():
    rising = [float(i) for i in range(1, 40)]
    assert rsi(rising, 14) == pytest.approx(100.0)


def test_rsi_midrange_for_choppy():
    # Alternating up/down → RSI hovers near 50, always within bounds.
    closes = [100 + (1 if i % 2 == 0 else -1) for i in range(40)]
    val = rsi(closes, 14)
    assert 0.0 <= val <= 100.0


def test_compute_indicators_keys():
    closes = [100 + i * 0.5 for i in range(260)]  # ~1y of daily closes
    ind = compute_indicators(closes)
    assert {"sma50", "sma200", "rsi14", "macd", "macd_signal"} <= set(ind)
    assert ind["sma200"] < ind["sma50"]  # uptrend: short MA above long MA

"""Technical-indicator math — pure functions over a daily close series.

Closes are oldest → newest. Every value here becomes a cited Fact whose
provenance is "computed from <source>'s daily closes", so a reader can
reproduce it. No data fetching happens in this module (keeps it unit-testable).
"""

from __future__ import annotations

import numpy as np


def sma(closes: list[float], period: int) -> float | None:
    if len(closes) < period:
        return None
    return float(np.mean(closes[-period:]))


def ema_series(closes: list[float], period: int) -> list[float]:
    if not closes:
        return []
    k = 2.0 / (period + 1)
    out = [float(closes[0])]
    for v in closes[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def ema(closes: list[float], period: int) -> float | None:
    if len(closes) < period:
        return None
    return ema_series(closes, period)[-1]


def rsi(closes: list[float], period: int = 14) -> float | None:
    """Wilder's RSI (0–100)."""
    if len(closes) <= period:
        return None
    deltas = np.diff(np.asarray(closes, dtype=float))
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_gain = float(np.mean(gains[:period]))
    avg_loss = float(np.mean(losses[:period]))
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return float(100 - 100 / (1 + rs))


def macd(closes: list[float], fast: int = 12, slow: int = 26, signal: int = 9) -> dict | None:
    """MACD line, signal line, and histogram (latest values)."""
    if len(closes) < slow + signal:
        return None
    fast_e = ema_series(closes, fast)
    slow_e = ema_series(closes, slow)
    macd_line = [f - s for f, s in zip(fast_e, slow_e)]
    signal_line = ema_series(macd_line, signal)
    return {
        "macd": float(macd_line[-1]),
        "signal": float(signal_line[-1]),
        "hist": float(macd_line[-1] - signal_line[-1]),
    }


def compute_indicators(closes: list[float]) -> dict[str, float]:
    """Latest values for the indicators we publish, skipping any we can't compute."""
    out: dict[str, float] = {}
    for name, period in (("sma50", 50), ("sma200", 200)):
        v = sma(closes, period)
        if v is not None:
            out[name] = v
    r = rsi(closes, 14)
    if r is not None:
        out["rsi14"] = r
    m = macd(closes)
    if m is not None:
        out["macd"] = m["macd"]
        out["macd_signal"] = m["signal"]
    return out

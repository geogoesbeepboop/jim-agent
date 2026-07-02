"""Free equity price feed (Yahoo Finance chart API) — no key required.

Returns the latest price, 52-week range, latest volume, and a daily close
series for technicals. Best-effort: any failure returns ``None`` and the
fundamentals memo proceeds on EDGAR alone. The request runs under
jim.net.resilience (timeout + bounded retries + per-host breaker), so a flaky
feed gets a couple of chances before we degrade.

Note on provenance: market prices come with feed ToS (unlike public-domain
EDGAR). For a licensed deployment, swap this for a redistributable market-data
source; the Fact/citation model is unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

from jim.net.resilience import CircuitOpen, resilient_call

_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=1y&interval=1d"
_HOST = "query1.finance.yahoo.com"
_HEADERS = {"User-Agent": "Mozilla/5.0 (jim-agent research)"}


@dataclass
class PriceData:
    symbol: str
    price: float
    currency: str | None
    fifty_two_high: float | None
    fifty_two_low: float | None
    volume: float | None
    closes: list[float]  # oldest → newest
    as_of: str | None


async def fetch_prices(symbol: str) -> PriceData | None:
    async def _get() -> dict:
        async with httpx.AsyncClient(headers=_HEADERS, timeout=httpx.Timeout(20.0)) as c:
            resp = await c.get(_CHART_URL.format(symbol=symbol.upper()))
            resp.raise_for_status()
            return resp.json()

    try:
        res = (await resilient_call(_get, host=_HOST))["chart"]["result"][0]
    except (
        httpx.HTTPError,  # transport (after retries exhaust) + status errors
        TimeoutError,  # the wrapper's wall-clock backstop
        CircuitOpen,  # feed declared down — degrade instantly
        KeyError,
        IndexError,
        ValueError,
        TypeError,
    ):
        return None

    meta = res.get("meta", {})
    quote = (res.get("indicators", {}).get("quote") or [{}])[0]
    closes = [c for c in (quote.get("close") or []) if c is not None]
    volumes = [v for v in (quote.get("volume") or []) if v is not None]
    timestamps = res.get("timestamp") or []
    price = meta.get("regularMarketPrice")
    if price is None and closes:
        price = closes[-1]
    if price is None:
        return None

    as_of = None
    if timestamps:
        as_of = datetime.fromtimestamp(timestamps[-1], tz=timezone.utc).date().isoformat()

    return PriceData(
        symbol=symbol.upper(),
        price=float(price),
        currency=meta.get("currency"),
        fifty_two_high=meta.get("fiftyTwoWeekHigh"),
        fifty_two_low=meta.get("fiftyTwoWeekLow"),
        volume=float(volumes[-1]) if volumes else None,
        closes=[float(c) for c in closes],
        as_of=as_of,
    )

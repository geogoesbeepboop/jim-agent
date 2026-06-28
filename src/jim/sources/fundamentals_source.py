"""Fundamentals source = EDGAR (free, public domain) + market enrichment.

EDGAR gives the audited financials; Yahoo gives the live price, from which we
derive the market metrics the audited filings can't carry — Market Cap, P/E,
P/B, dividend yield — plus technicals (52W range, SMA50/200, RSI, MACD, volume).

Price enrichment is best-effort: if the feed is unavailable, the memo proceeds
on EDGAR facts alone. Market-derived facts cite BOTH inputs (price + filing).
"""

from __future__ import annotations

from jim.config import get_settings
from jim.research.budget import BudgetCap
from jim.research.edgar import fetch_snapshot
from jim.research.facts import (
    COUNT,
    INDEX,
    MULTIPLE,
    PERCENT,
    USD,
    Fact,
    Snapshot,
)
from jim.research.indicators import compute_indicators
from jim.sources.base import GatherResult
from jim.sources.yahoo import PriceData, fetch_prices
from jim.store import Store

_YH = "Yahoo Finance"
_YH_COMPUTED = "Yahoo Finance (computed from daily closes)"


def _id_factory(snapshot: Snapshot):
    start = max((int(f.id[1:]) for f in snapshot.facts if f.id.startswith("C")), default=0)
    counter = {"n": start}

    def nxt() -> str:
        counter["n"] += 1
        return f"C{counter['n']}"

    return nxt


def enrich_with_prices(snapshot: Snapshot, price: PriceData) -> None:
    """Append price, technical, and market-derived facts to ``snapshot``."""
    nxt = _id_factory(snapshot)
    by_label = {f.label: f for f in snapshot.facts}
    url = f"https://finance.yahoo.com/quote/{price.symbol}"

    def market(label, value, unit, concept) -> Fact:
        f = Fact(
            id=nxt(),
            label=label,
            value=value,
            unit=unit,
            source_label=_YH,
            concept=concept,
            accession=price.symbol,
            form="market data",
            filed=price.as_of,
            source_url=url,
        )
        snapshot.facts.append(f)
        return f

    def computed(label, value, unit) -> Fact:
        f = Fact(
            id=nxt(),
            label=label,
            value=value,
            unit=unit,
            source_label=_YH_COMPUTED,
            accession=price.symbol,
            form="indicator",
            filed=price.as_of,
            source_url=url,
        )
        snapshot.facts.append(f)
        return f

    def derived(label, value, unit, formula, inputs) -> Fact:
        f = Fact(
            id=nxt(),
            label=label,
            value=value,
            unit=unit,
            is_derived=True,
            derived_from=tuple(i.id for i in inputs),
            formula=formula,
        )
        snapshot.facts.append(f)
        return f

    price_fact = market("Price", price.price, USD, "regularMarketPrice")
    if price.fifty_two_high:
        market("52-week high", price.fifty_two_high, USD, "fiftyTwoWeekHigh")
    if price.fifty_two_low:
        market("52-week low", price.fifty_two_low, USD, "fiftyTwoWeekLow")
    if price.volume:
        market("Volume (latest session)", price.volume, COUNT, "volume")

    ind = compute_indicators(price.closes)
    if "sma50" in ind:
        computed("50-day moving average", ind["sma50"], USD)
    if "sma200" in ind:
        computed("200-day moving average", ind["sma200"], USD)
    if "rsi14" in ind:
        computed("RSI (14-day)", ind["rsi14"], INDEX)
    if "macd" in ind:
        computed("MACD", ind["macd"], INDEX)
        computed("MACD signal", ind["macd_signal"], INDEX)

    # Market-derived metrics (need an EDGAR input each).
    shares = by_label.get("Shares outstanding")
    eps = by_label.get("Diluted EPS")
    equity = by_label.get("Stockholders' equity")
    dps = by_label.get("Dividends per share")

    mcap = None
    if shares:
        mcap = derived(
            "Market cap",
            price.price * shares.value,
            USD,
            "Price × Shares outstanding",
            [price_fact, shares],
        )
    if eps and eps.value:
        derived(
            "P/E (TTM)", price.price / eps.value, MULTIPLE, "Price / Diluted EPS", [price_fact, eps]
        )
    if mcap and equity and equity.value:
        derived(
            "P/B",
            mcap.value / equity.value,
            MULTIPLE,
            "Market cap / Stockholders' equity",
            [mcap, equity],
        )
    if dps and price.price:
        derived(
            "Dividend yield",
            dps.value / price.price * 100,
            PERCENT,
            "Dividends per share / Price",
            [dps, price_fact],
        )

    if price.as_of and (snapshot.as_of is None or price.as_of > snapshot.as_of):
        snapshot.as_of = price.as_of


class FundamentalsSource:
    name = "fundamentals"
    is_paid = False

    async def gather(self, identifier: str, *, budget: BudgetCap, store: Store) -> GatherResult:
        snapshot = await fetch_snapshot(identifier)
        if get_settings().enable_prices:
            price = await fetch_prices(identifier)
            if price is not None:
                enrich_with_prices(snapshot, price)
        return GatherResult(snapshot=snapshot, cost_in_usd=0.0, cache_hit=False)

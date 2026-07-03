"""SEC EDGAR client — the public-domain primary source.

We pull XBRL "company facts" (one request per company) and extract the latest
annual value for a curated set of GAAP concepts. Each extracted value keeps the
``accession`` of the filing it came from, which is what makes every downstream
number citable to a real document.

SEC asks for a descriptive User-Agent with contact info and rate-limits to
~10 req/s; a fundamentals snapshot costs 1–2 requests so we stay well under.

Each outbound request runs under jim.net.resilience (timeout + bounded retries
+ per-host breaker) for transport-level failures; HTTP status handling — the 404
→ ``EdgarError`` path included — stays here, unretried.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date

import httpx

from jim.config import get_settings
from jim.net.resilience import resilient_call
from jim.research.facts import (
    PERCENT,
    SHARES,
    USD,
    USD_PER_SHARE,
    Fact,
    Snapshot,
    compute_derived,
)

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"


@dataclass(frozen=True)
class Concept:
    label: str
    unit: str
    kind: str  # "duration" (income/cash-flow) or "instant" (balance sheet)
    tags: tuple[str, ...]  # us-gaap tags to try, in priority order


# Curated, ordered. First matching tag with annual data wins.
PRIMARY_CONCEPTS: list[Concept] = [
    Concept(
        "Revenue",
        USD,
        "duration",
        ("RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "SalesRevenueNet"),
    ),
    Concept(
        "Cost of revenue",
        USD,
        "duration",
        ("CostOfGoodsAndServicesSold", "CostOfRevenue", "CostOfGoodsSold"),
    ),
    Concept("Gross profit", USD, "duration", ("GrossProfit",)),
    Concept("Operating income", USD, "duration", ("OperatingIncomeLoss",)),
    Concept("Net income", USD, "duration", ("NetIncomeLoss",)),
    Concept("Research & development", USD, "duration", ("ResearchAndDevelopmentExpense",)),
    Concept(
        "Operating cash flow",
        USD,
        "duration",
        (
            "NetCashProvidedByUsedInOperatingActivities",
            "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
        ),
    ),
    Concept("Total assets", USD, "instant", ("Assets",)),
    Concept("Total liabilities", USD, "instant", ("Liabilities",)),
    Concept(
        "Stockholders' equity",
        USD,
        "instant",
        (
            "StockholdersEquity",
            "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
        ),
    ),
    Concept("Cash & equivalents", USD, "instant", ("CashAndCashEquivalentsAtCarryingValue",)),
    Concept(
        "Depreciation & amortization",
        USD,
        "duration",
        (
            "DepreciationDepletionAndAmortization",
            "DepreciationAmortizationAndAccretionNet",
            "DepreciationAndAmortization",
        ),
    ),
    Concept(
        "Diluted EPS",
        USD_PER_SHARE,
        "duration",
        ("EarningsPerShareDiluted", "EarningsPerShareBasicAndDiluted"),
    ),
    Concept(
        "Dividends per share",
        USD_PER_SHARE,
        "duration",
        ("CommonStockDividendsPerShareDeclared", "CommonStockDividendsPerShareCashPaid"),
    ),
]

# Shares outstanding lives in the `dei` taxonomy (10-K cover), not us-gaap.
_DEI_SHARES_TAGS = ("EntityCommonStockSharesOutstanding",)

_UNIT_KEY = {USD: "USD", USD_PER_SHARE: "USD/shares", SHARES: "shares"}


class EdgarError(RuntimeError):
    """Raised when EDGAR has no usable data for a ticker."""


_ticker_cache: dict[str, str] = {}
_ticker_lock = asyncio.Lock()


def _filing_url(cik: str, accession: str) -> str:
    nodash = accession.replace("-", "")
    cik_int = int(cik)
    return f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{nodash}/{accession}-index.htm"


def _is_annual(entry: dict, kind: str) -> bool:
    form = entry.get("form", "")
    if not form.startswith("10-K"):
        return False
    if entry.get("fp") != "FY":
        return False
    if kind == "duration":
        start, end = entry.get("start"), entry.get("end")
        if not start or not end:
            return False
        # Guard against a stray non-annual duration sneaking in.
        span = (date.fromisoformat(end) - date.fromisoformat(start)).days
        return span >= 300
    return bool(entry.get("end"))


def _annual_entries(unit_entries: list[dict], kind: str) -> list[dict]:
    """Annual entries, newest first, de-duplicated to one per fiscal year."""
    annual = [e for e in unit_entries if _is_annual(e, kind)]
    annual.sort(key=lambda e: e["end"], reverse=True)
    seen_years: set = set()
    out: list[dict] = []
    for e in annual:
        fy = e.get("fy")
        if fy in seen_years:
            continue
        seen_years.add(fy)
        out.append(e)
    return out


async def _ticker_to_cik(client: httpx.AsyncClient, ticker: str) -> str:
    ticker = ticker.upper().strip()
    async with _ticker_lock:
        if not _ticker_cache:
            resp = await resilient_call(lambda: client.get(TICKERS_URL), host="www.sec.gov")
            resp.raise_for_status()
            for row in resp.json().values():
                _ticker_cache[row["ticker"].upper()] = f"{int(row['cik_str']):010d}"
    cik = _ticker_cache.get(ticker)
    if not cik:
        raise EdgarError(f"Unknown ticker {ticker!r} (not in SEC's ticker list).")
    return cik


def _extract(cik: str, gaap: dict, dei: dict) -> tuple[list[Fact], dict | None]:
    """Pull the latest annual value for each curated concept.

    Returns (primary_facts, prior_revenue_entry) — the prior-year revenue entry
    is handed back so the caller can build a fully-cited YoY growth figure.
    """
    counter = {"n": 0}

    def next_id() -> str:
        counter["n"] += 1
        return f"C{counter['n']}"

    facts: list[Fact] = []
    prior_revenue: dict | None = None
    revenue_series: list[dict] = []

    for concept in PRIMARY_CONCEPTS:
        unit_key = _UNIT_KEY[concept.unit]
        for tag in concept.tags:
            node = gaap.get(tag)
            if not node or unit_key not in node.get("units", {}):
                continue
            entries = _annual_entries(node["units"][unit_key], concept.kind)
            if not entries:
                continue
            latest = entries[0]
            facts.append(
                Fact(
                    id=next_id(),
                    label=concept.label,
                    value=float(latest["val"]),
                    unit=concept.unit,
                    source_label="SEC EDGAR",
                    concept=tag,
                    accession=latest["accn"],
                    form=latest.get("form"),
                    fiscal_year=latest.get("fy"),
                    fiscal_period=latest.get("fp"),
                    filed=latest.get("filed"),
                    period_end=latest.get("end"),
                    source_url=_filing_url(cik, latest["accn"]),
                )
            )
            if concept.label == "Revenue":
                revenue_series = entries
            break  # first matching tag wins

    # Shares outstanding from the dei taxonomy (10-K cover page).
    for tag in _DEI_SHARES_TAGS:
        node = dei.get(tag)
        if not node or "shares" not in node.get("units", {}):
            continue
        entries = [e for e in node["units"]["shares"] if str(e.get("form", "")).startswith("10-K")]
        if not entries:
            continue
        latest = max(entries, key=lambda e: e.get("end", ""))
        facts.append(
            Fact(
                id=next_id(),
                label="Shares outstanding",
                value=float(latest["val"]),
                unit=SHARES,
                source_label="SEC EDGAR",
                concept=tag,
                accession=latest["accn"],
                form=latest.get("form"),
                fiscal_year=latest.get("fy"),
                fiscal_period=latest.get("fp"),
                filed=latest.get("filed"),
                period_end=latest.get("end"),
                source_url=_filing_url(cik, latest["accn"]),
            )
        )
        break

    if len(revenue_series) >= 2:
        prior_revenue = revenue_series[1]

    return facts, prior_revenue


def _revenue_growth(facts: list[Fact], prior: dict, cik: str, next_id) -> Fact | None:
    cur = next((f for f in facts if f.label == "Revenue"), None)
    if not cur or not prior or not prior.get("val"):
        return None
    prior_val = float(prior["val"])
    growth = (cur.value - prior_val) / prior_val * 100
    prior_fact = Fact(
        id=next_id(),
        label="Revenue (prior FY)",
        value=prior_val,
        unit=USD,
        source_label="SEC EDGAR",
        concept=cur.concept,
        accession=prior["accn"],
        form=prior.get("form"),
        fiscal_year=prior.get("fy"),
        fiscal_period=prior.get("fp"),
        filed=prior.get("filed"),
        period_end=prior.get("end"),
        source_url=_filing_url(cik, prior["accn"]),
    )
    facts.append(prior_fact)
    return Fact(
        id=next_id(),
        label="Revenue growth (YoY)",
        value=growth,
        unit=PERCENT,
        is_derived=True,
        derived_from=(cur.id, prior_fact.id),
        formula="(Revenue − prior-FY Revenue) / prior-FY Revenue",
    )


async def fetch_snapshot(ticker: str) -> Snapshot:
    """Fetch and assemble a fully-cited fundamentals snapshot for ``ticker``."""
    settings = get_settings()
    headers = {"User-Agent": settings.sec_user_agent, "Accept-Encoding": "gzip, deflate"}
    timeout = httpx.Timeout(30.0)

    async with httpx.AsyncClient(headers=headers, timeout=timeout) as client:
        cik = await _ticker_to_cik(client, ticker)
        resp = await resilient_call(
            lambda: client.get(COMPANYFACTS_URL.format(cik=cik)), host="data.sec.gov"
        )
        if resp.status_code == 404:
            raise EdgarError(f"No XBRL company facts on file for {ticker.upper()} (CIK {cik}).")
        resp.raise_for_status()
        data = resp.json()

    gaap = data.get("facts", {}).get("us-gaap", {})
    dei = data.get("facts", {}).get("dei", {})
    if not gaap:
        raise EdgarError(f"{ticker.upper()} has no us-gaap XBRL facts (likely a non-filer).")

    facts, prior_revenue = _extract(cik, gaap, dei)
    if not facts:
        raise EdgarError(f"Could not extract any annual fundamentals for {ticker.upper()}.")

    # Continue the id sequence for derived facts.
    counter = {"n": len(facts)}

    def next_id() -> str:
        counter["n"] += 1
        return f"C{counter['n']}"

    if prior_revenue:
        growth = _revenue_growth(facts, prior_revenue, cik, next_id)
        if growth:
            facts.append(growth)

    facts.extend(compute_derived([f for f in facts if not f.is_derived], next_id))

    as_of = max((f.filed for f in facts if f.filed), default=None)
    return Snapshot(
        ticker=ticker.upper(),
        cik=cik,
        entity_name=data.get("entityName", ticker.upper()),
        facts=facts,
        as_of=as_of,
    )

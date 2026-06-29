"""Macro context as a FREE, public-domain Source.

Every figure cites a **US-government primary source** — which keeps macro inside
jim's core invariant (public-domain provenance, freely redistributable):

  - Fed funds (effective)  → Federal Reserve Bank of New York, EFFR
  - CPI (level + YoY)      → U.S. Bureau of Labor Statistics (CPI-U, CPIAUCSL)
  - 10y / 2y Treasury yld  → U.S. Department of the Treasury (daily par yields)
  - 2s10s spread           → derived from the two Treasury yields

Deliberately **not FRED**: FRED's API ToS forbids caching/redistribution, whereas
the underlying agency data is public domain (17 U.S.C. §105). We go straight to
the primary agencies. Equity index levels (S&P 500 etc.) are intentionally absent
— they are proprietary, with no public-domain path. See ADR-0007.

Best-effort, like the Yahoo enrichment: each upstream is fetched independently and
a failure simply drops that fact rather than failing the run. The fetcher is
injectable so the source is fully testable offline.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from jim.research.budget import BudgetCap
from jim.research.facts import INDEX, PERCENT, Fact, Snapshot
from jim.sources.base import GatherResult
from jim.store import Store


@dataclass
class MacroReading:
    """One cited macro figure: value, unit, the agency, and a resolving link."""

    label: str
    value: float
    unit: str
    source_label: str
    source_url: str
    as_of: str | None = None
    concept: str | None = None


@dataclass
class MacroData:
    readings: list[MacroReading] = field(default_factory=list)
    as_of: str | None = None


class MacroSource:
    name = "macro"
    is_paid = False  # public-domain US-gov data, free to fetch and redistribute

    def __init__(self, fetch_fn=None):
        # Injectable so tests need no network; defaults to the live gov fetchers.
        self._fetch = fetch_fn or fetch_macro

    async def gather(self, identifier: str, *, budget: BudgetCap, store: Store) -> GatherResult:
        data = await self._fetch()
        facts: list[Fact] = []
        for i, r in enumerate(data.readings, start=1):
            facts.append(
                Fact(
                    id=f"C{i}",
                    label=r.label,
                    value=r.value,
                    unit=r.unit,
                    source_label=r.source_label,
                    concept=r.concept,
                    accession=r.as_of,  # the release/observation date anchors the citation
                    form="gov release",
                    filed=r.as_of,
                    source_url=r.source_url,
                )
            )
        snapshot = Snapshot(
            ticker="US",
            cik="US-MACRO",
            entity_name="United States — macro context",
            facts=facts,
            as_of=data.as_of,
        )
        # Free source: no spend, never a cache hit (the engine's memo cache still
        # applies on top via the snapshot fingerprint).
        return GatherResult(snapshot=snapshot, cost_in_usd=0.0, cache_hit=False)


# --- live fetchers (best-effort; each failure drops its reading) -------------

_EFFR_URL = "https://markets.newyorkfed.org/api/rates/unsecured/effr/last/1.json"
_TREASURY_URL = (
    "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
    "pages/xml?data=daily_treasury_yield_curve&field_tdr_date_value={year}"
)
_BLS_V2 = "https://api.bls.gov/publicAPI/v2/timeseries/data/"
_BLS_V1 = "https://api.bls.gov/publicAPI/v1/timeseries/data/CPIAUCSL"


async def fetch_macro() -> MacroData:
    """Fetch the macro readings from US-government primary sources (best-effort)."""
    import asyncio

    readings: list[MacroReading] = []
    results = await asyncio.gather(
        _fetch_effr(), _fetch_cpi(), _fetch_treasury(), return_exceptions=True
    )
    for res in results:
        if isinstance(res, list):
            readings.extend(res)
    # Derived: 2s10s spread, if both legs are present.
    by_label = {r.label: r for r in readings}
    ten, two = by_label.get("10y Treasury yield"), by_label.get("2y Treasury yield")
    if ten and two:
        readings.append(
            MacroReading(
                label="2s10s spread",
                value=round(ten.value - two.value, 3),
                unit=PERCENT,
                source_label="U.S. Treasury (derived)",
                source_url=ten.source_url,
                as_of=ten.as_of,
                concept="10y − 2y par yield",
            )
        )
    as_of = next((r.as_of for r in readings if r.as_of), None)
    return MacroData(readings=readings, as_of=as_of)


async def _fetch_effr() -> list[MacroReading]:
    import httpx

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(20.0)) as c:
            resp = await c.get(_EFFR_URL)
            resp.raise_for_status()
            row = resp.json()["refRates"][0]
        return [
            MacroReading(
                label="Fed funds rate (effective)",
                value=float(row["percentRate"]),
                unit=PERCENT,
                source_label="Federal Reserve Bank of New York (EFFR)",
                source_url="https://www.newyorkfed.org/markets/reference-rates/effr",
                as_of=row.get("effectiveDate"),
                concept="EFFR",
            )
        ]
    except Exception:
        return []


async def _fetch_cpi() -> list[MacroReading]:
    import httpx

    from jim.config import get_settings

    settings = get_settings()
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(20.0)) as c:
            if settings.bls_api_key:
                resp = await c.post(
                    _BLS_V2,
                    json={
                        "seriesid": ["CPIAUCSL"],
                        "registrationkey": settings.bls_api_key,
                        "calculations": True,
                    },
                )
            else:
                resp = await c.get(_BLS_V1)
            resp.raise_for_status()
            series = resp.json()["Results"]["series"][0]["data"]
        latest = series[0]
        level = float(latest["value"])
        out = [
            MacroReading(
                label="CPI (index)",
                value=level,
                unit=INDEX,
                source_label="U.S. Bureau of Labor Statistics (CPI-U)",
                source_url="https://www.bls.gov/cpi/",
                as_of=f"{latest['year']}-{latest['period'].lstrip('M')}",
                concept="CPIAUCSL",
            )
        ]
        # YoY: same period twelve months back, if present in the window.
        year_ago = next(
            (
                d
                for d in series
                if d["period"] == latest["period"] and int(d["year"]) == int(latest["year"]) - 1
            ),
            None,
        )
        if year_ago:
            yoy = (level / float(year_ago["value"]) - 1.0) * 100.0
            out.append(
                MacroReading(
                    label="CPI inflation (YoY)",
                    value=round(yoy, 2),
                    unit=PERCENT,
                    source_label="U.S. Bureau of Labor Statistics (CPI-U)",
                    source_url="https://www.bls.gov/cpi/",
                    as_of=out[0].as_of,
                    concept="CPIAUCSL YoY",
                )
            )
        return out
    except Exception:
        return []


async def _fetch_treasury() -> list[MacroReading]:
    import re
    from datetime import datetime, timezone

    import httpx

    year = datetime.now(timezone.utc).year
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(20.0)) as c:
            resp = await c.get(_TREASURY_URL.format(year=year))
            resp.raise_for_status()
            text = resp.text
        # The feed is an Atom document; the last <entry> is the most recent day.
        entries = re.findall(r"<entry>.*?</entry>", text, re.DOTALL)
        if not entries:
            return []
        last = entries[-1]

        def grab(tag: str) -> str | None:
            m = re.search(rf"<d:{tag}[^>]*>([^<]+)</d:{tag}>", last)
            return m.group(1) if m else None

        date = grab("NEW_DATE")
        as_of = date.split("T")[0] if date else None
        out: list[MacroReading] = []
        for label, tag in (("10y Treasury yield", "BC_10YEAR"), ("2y Treasury yield", "BC_2YEAR")):
            raw = grab(tag)
            if raw:
                out.append(
                    MacroReading(
                        label=label,
                        value=float(raw),
                        unit=PERCENT,
                        source_label="U.S. Department of the Treasury",
                        source_url=(
                            "https://home.treasury.gov/resource-center/data-chart-center/"
                            "interest-rates"
                        ),
                        as_of=as_of,
                        concept=tag,
                    )
                )
        return out
    except Exception:
        return []

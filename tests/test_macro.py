"""The free, public-domain macro source — Fed funds / CPI / Treasury.

Offline: the gov fetchers are injected/mocked. Proves the source builds cited
facts at $0 cost, the live aggregator derives the 2s10s spread and degrades when
an upstream fails, the macro memo passes the deterministic gate, and macro is a
registered product.
"""

from __future__ import annotations

import pytest

from jim.research.budget import BudgetCap
from jim.research.facts import PERCENT
from jim.research.gate import check_sourcing
from jim.research.products import get_products
from jim.sources import macro as macro_mod
from jim.sources.macro import MacroData, MacroReading, MacroSource, fetch_macro
from jim.store import MemoryStore


def _readings() -> list[MacroReading]:
    return [
        MacroReading("Fed funds rate (effective)", 4.33, PERCENT, "NY Fed (EFFR)",
                     "https://x", "2026-06-27", "EFFR"),
        MacroReading("10y Treasury yield", 4.20, PERCENT, "U.S. Treasury", "https://x",
                     "2026-06-27", "BC_10YEAR"),
        MacroReading("2y Treasury yield", 3.90, PERCENT, "U.S. Treasury", "https://x",
                     "2026-06-27", "BC_2YEAR"),
    ]


async def test_macro_source_builds_cited_free_facts() -> None:
    async def fake_fetch():
        return MacroData(readings=_readings(), as_of="2026-06-27")

    src = MacroSource(fetch_fn=fake_fetch)
    res = await src.gather("US", budget=BudgetCap(0.10), store=MemoryStore())
    assert res.cost_in_usd == 0.0 and res.cache_hit is False  # free, public-domain
    assert res.snapshot.entity_name.startswith("United States")
    labels = {f.label for f in res.snapshot.facts}
    assert "Fed funds rate (effective)" in labels
    for f in res.snapshot.facts:
        assert f.source_url and f.accession  # every figure cites a primary source


async def test_fetch_macro_derives_2s10s_and_degrades(monkeypatch) -> None:
    async def effr():
        return [_readings()[0]]

    async def cpi():
        raise RuntimeError("BLS down")  # one upstream fails

    async def treasury():
        return _readings()[1:]  # 10y + 2y

    monkeypatch.setattr(macro_mod, "_fetch_effr", effr)
    monkeypatch.setattr(macro_mod, "_fetch_cpi", cpi)
    monkeypatch.setattr(macro_mod, "_fetch_treasury", treasury)

    data = await fetch_macro()
    labels = {r.label for r in data.readings}
    # CPI dropped (its fetch failed) but the run still produced the others...
    assert "CPI (index)" not in labels
    assert "Fed funds rate (effective)" in labels
    # ...and the derived 2s10s spread = 10y − 2y = 0.30.
    spread = next(r for r in data.readings if r.label == "2s10s spread")
    assert spread.value == pytest.approx(0.30)


async def test_macro_memo_passes_the_gate() -> None:
    async def fake_fetch():
        return MacroData(readings=_readings(), as_of="2026-06-27")

    res = await MacroSource(fetch_fn=fake_fetch).gather(
        "US", budget=BudgetCap(0.10), store=MemoryStore()
    )
    snap = res.snapshot
    # A faithful, cited macro memo must pass the deterministic sourcing gate.
    memo = (
        "Fed funds (effective) is 4.33% [C1]. The 10y Treasury yields 4.2% [C2] "
        "versus 3.9% [C3] at the 2y."
    )
    assert check_sourcing(memo, snap).passed


def test_macro_is_a_registered_product() -> None:
    products = get_products()
    assert "macro" in products
    assert products["macro"].source.is_paid is False
    assert products["macro"].price_out_usd > 0

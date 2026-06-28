"""Procurement: cache avoids re-buying, budget blocks overspend, parser works.

Offline — the buy function is injected, so no network or wallet is touched.
"""

from __future__ import annotations

import json

import pytest

from jim.buyer.client import PaidResponse
from jim.research.budget import BudgetCap
from jim.sources.base import BudgetExceeded
from jim.sources.thegraph import GraphSource
from jim.store import MemoryStore
from jim.vendor import build_mock_response


def _fake_buy(counter: dict):
    async def buy(
        url, *, method="GET", json_body=None, headers=None, private_key=None, timeout=180.0
    ):
        counter["n"] += 1
        payload = build_mock_response((json_body or {}).get("query", ""))
        return PaidResponse(
            status_code=200,
            text=json.dumps(payload),
            settlement={"transaction": "0xtx"},
            cost_in_usd=0.01,
            tx_hash="0xtx",
        )

    return buy


async def test_first_buy_then_cache_hit():
    counter = {"n": 0}
    store = MemoryStore()
    source = GraphSource(buy_fn=_fake_buy(counter))

    first = await source.gather("WETH", budget=BudgetCap(0.10), store=store)
    assert counter["n"] == 1
    assert first.cache_hit is False
    assert first.cost_in_usd == 0.01
    price = first.snapshot.by_id("C1")
    assert price.label == "Price (USD)" and price.value == pytest.approx(2500.0)
    mcap = next(f for f in first.snapshot.facts if f.label == "Market cap (USD)")
    assert mcap.value == pytest.approx(2500.0 * 3_000_000)

    # Second call reuses the cached datum: no new purchase, zero marginal cost.
    second = await source.gather("WETH", budget=BudgetCap(0.10), store=store)
    assert counter["n"] == 1  # buy NOT called again
    assert second.cache_hit is True
    assert second.cost_in_usd == 0.0


async def test_budget_blocks_purchase():
    counter = {"n": 0}
    source = GraphSource(buy_fn=_fake_buy(counter))
    # Ceiling below the source's price estimate → proposal denied, nothing bought.
    with pytest.raises(BudgetExceeded):
        await source.gather("WETH", budget=BudgetCap(0.001), store=MemoryStore())
    assert counter["n"] == 0


async def test_graph_facts_are_cited_to_the_subgraph():
    source = GraphSource(buy_fn=_fake_buy({"n": 0}))
    result = await source.gather("UNI", budget=BudgetCap(0.10), store=MemoryStore())
    for f in result.snapshot.facts:
        assert f.source_label == "The Graph · Uniswap v3"
        assert f.accession  # subgraph id is the citation anchor
        assert f.source_url

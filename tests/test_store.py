"""In-memory store: cache TTL, margin summary, semantic search. Offline."""

from __future__ import annotations

from datetime import timedelta

import pytest

from jim.store.embed import cosine, embed
from jim.store.repo import MemoryStore, _utcnow


async def test_purchase_cache_hit_and_expiry():
    store = MemoryStore()
    await store.record_purchase(
        source="thegraph",
        key="sub:0xabc",
        url="http://x",
        network="eip155:84532",
        cost_usd=0.01,
        tx_hash="0xtx",
        payload={"data": 1},
        ttl_seconds=3600,
    )
    hit = await store.get_cached_purchase("thegraph", "sub:0xabc")
    assert hit is not None and hit.payload == {"data": 1} and hit.cost_usd == 0.01

    # Force expiry.
    store.purchases[("thegraph", "sub:0xabc")]["expires_at"] = _utcnow() - timedelta(seconds=1)
    assert await store.get_cached_purchase("thegraph", "sub:0xabc") is None


async def test_margin_summary_counts_only_ok():
    store = MemoryStore()
    await store.record_query(
        product="token",
        identifier="WETH",
        mode="agent",
        status="ok",
        price_out_usd=0.50,
        cost_in_data_usd=0.01,
        cost_inference_usd=0.02,
        cache_hit=False,
        attempts=1,
    )
    await store.record_query(
        product="token",
        identifier="WBTC",
        mode="agent",
        status="rejected",
        price_out_usd=0.50,
        cost_in_data_usd=0.01,
        cost_inference_usd=0.02,
        cache_hit=True,
        attempts=2,
    )
    s = await store.margin_summary()
    assert s["billable_queries"] == 1  # only the ok run
    assert s["total_queries"] == 2
    assert s["revenue_usd"] == pytest.approx(0.50)
    assert s["total_margin_usd"] == pytest.approx(0.47)


async def test_semantic_search_ranks_similar_first():
    store = MemoryStore()
    await store.upsert_insight(
        key="a",
        text="Apple revenue grew strongly this year",
        embedding=embed("Apple revenue grew strongly this year"),
    )
    await store.upsert_insight(
        key="b",
        text="Ethereum on-chain transaction volume",
        embedding=embed("Ethereum on-chain transaction volume"),
    )
    results = await store.search_insights(embed("Apple revenue grew this year"), k=2)
    assert results[0]["cache_key"] == "a"
    assert results[0]["score"] > results[1]["score"]


def test_embeddings_are_deterministic():
    # Cross-process stability matters for cache reuse.
    assert embed("WETH token snapshot") == embed("WETH token snapshot")
    assert cosine(embed("hello world"), embed("hello world")) == pytest.approx(1.0, abs=1e-5)

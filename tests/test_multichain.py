"""Multi-chain token support — identifier parsing, chain registry, cache isolation.

Offline: the buy is injected, so no network. Proves a token resolves to the right
chain + subgraph, that cross-chain queries don't collide in the cache, and that
citations name the chain.
"""

from __future__ import annotations

import json

import pytest

from jim.buyer.client import PaidResponse
from jim.research.budget import BudgetCap
from jim.sources.base import ProcurementError
from jim.sources.thegraph import DEFAULT_CHAIN, GraphSource, chains, resolve, resolve_token
from jim.store import MemoryStore

# A chain-agnostic Uniswap-v3 payload (the real mock only knows ETH addresses; we
# care about cache isolation + citation labels, not the mock's per-address data).
_PAYLOAD = {
    "data": {
        "token": {
            "symbol": "WETH", "name": "Wrapped Ether", "decimals": "18",
            "totalSupply": "1000000000000000000000000", "volumeUSD": "1000000",
            "txCount": "5", "totalValueLockedUSD": "2000000", "derivedETH": "1",
        },
        "bundle": {"ethPriceUSD": "3000"},
    }
}


def _buy(counter: dict):
    async def buy(url, *, method="GET", json_body=None, headers=None, private_key=None,
                  timeout=180.0, max_price_usd=None):
        counter["n"] += 1
        counter["urls"].append(url)
        return PaidResponse(
            status_code=200,
            text=json.dumps(_PAYLOAD),
            settlement={"transaction": "0xtx"},
            cost_in_usd=0.0002,
            tx_hash="0xtx",
        )

    return buy


def test_chain_registry_has_the_evm_chains() -> None:
    reg = chains()
    assert set(reg) == {"ethereum", "base", "arbitrum", "polygon"}
    # each chain carries a distinct subgraph id
    assert len({c.subgraph_id for c in reg.values()}) == 4


def test_resolve_symbol_and_chain() -> None:
    addr, spec = resolve("WETH")
    assert spec.key == DEFAULT_CHAIN and addr.startswith("0xc02aaa39")
    addr_b, spec_b = resolve("WETH:base")
    assert spec_b.key == "base" and addr_b == "0x4200000000000000000000000000000000000006"
    # raw address + chain
    a, s = resolve("0x912ce59144191c1204e64559fe8253a0e49e6548:arbitrum")
    assert s.key == "arbitrum" and a == "0x912ce59144191c1204e64559fe8253a0e49e6548"


def test_resolve_rejects_unknown_chain_and_token() -> None:
    with pytest.raises(ProcurementError, match="Unsupported chain"):
        resolve("WETH:solana")
    with pytest.raises(ProcurementError, match="Unknown token"):
        resolve("NOTATOKEN:base")
    # back-compat helper still resolves an Ethereum symbol
    assert resolve_token("UNI").startswith("0x1f9840")


async def test_cross_chain_queries_do_not_collide_in_cache() -> None:
    counter = {"n": 0, "urls": []}
    store = MemoryStore()
    source = GraphSource(buy_fn=_buy(counter))

    await source.gather("WETH", budget=BudgetCap(0.10), store=store)  # ethereum
    await source.gather("WETH:base", budget=BudgetCap(0.10), store=store)  # base
    # Same symbol, different chains → two distinct buys (separate cache keys).
    assert counter["n"] == 2
    # A repeat of the ethereum query hits cache (no third buy).
    await source.gather("WETH", budget=BudgetCap(0.10), store=store)
    assert counter["n"] == 2


async def test_citation_names_the_chain() -> None:
    source = GraphSource(buy_fn=_buy({"n": 0, "urls": []}))
    res = await source.gather("WETH:base", budget=BudgetCap(0.10), store=MemoryStore())
    assert "base" in res.snapshot.entity_name
    for f in res.snapshot.facts:
        assert "Base" in f.source_label  # "The Graph · Uniswap v3 · Base"

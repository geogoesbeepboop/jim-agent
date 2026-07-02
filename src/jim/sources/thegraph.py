"""The Graph as a paid Source (x402) — now multi-chain.

Buys on-chain token data (a Uniswap-v3-schema subgraph) per query. Two wirings,
one code path, chosen by ``GRAPH_LIVE``:
  - live  → gateway.thegraph.com on Base mainnet (real USDC)
  - mock  → our local /mock-graph vendor on Base Sepolia (free testnet USDC)

The mock returns the exact same JSON shape, so :meth:`_parse` is identical for
both. Every parsed number cites one thing: the subgraph query that produced it.

**Multi-chain.** All Uniswap-v3 deployments (Ethereum, Base, Arbitrum, Polygon)
share the *same* GraphQL schema, so one query + one parser serves every chain —
only the subgraph id, the token address space, and the citation label change.
The identifier carries the chain: ``WETH`` (default Ethereum), ``WETH:base``,
``0xabc…:arbitrum``. Settlement is unchanged: every chain's data is still bought
over x402 on the configured ``graph_buy_network`` (Base). Aerodrome (a Solidly
fork with a *different* schema) is a registry entry left for a follow-up parser —
see ADR-0007.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from jim.buyer import pay
from jim.config import Settings, get_settings
from jim.research.budget import BudgetCap
from jim.research.facts import COUNT, USD, Fact, Snapshot
from jim.sources.base import GatherResult, ProcurementError, procure
from jim.store import Store


@dataclass(frozen=True)
class ChainSpec:
    """One EVM chain's Uniswap-v3 subgraph + token address space."""

    key: str  # "ethereum", "base", ...
    label: str  # human label for citations, e.g. "Uniswap v3 · Base"
    subgraph_id: str  # The Graph decentralized-network subgraph id
    tokens: dict[str, str] = field(default_factory=dict)  # SYMBOL → 0x address (lowercase)


# Community Uniswap-v3 subgraphs on The Graph's decentralized network (verified via
# Graph Explorer). They share Uniswap's schema, so the parser is identical. The
# Ethereum subgraph id is read from config so an operator can override it; the
# others are pinned here. Aerodrome-on-Base needs a different (Solidly) schema and
# is intentionally absent until its own parser lands. See ADR-0007.
_UNIV3_SUBGRAPHS: dict[str, str] = {
    "base": "FUbEPQw1oMghy39fwWBFY5fE6MXPXZQtjncQy2cXdrNS",
    "arbitrum": "3V7ZY6muhxaQL5qvntX1CFXJ32W7BxXZTGTwmpH5J4t3",
    "polygon": "3hCPRGf4z88VC5rsBKU5AA9FBBq5nF3jbKJG7VZCbhjm",
}

# A small, high-confidence per-chain symbol map (canonical wrapped-native + USDC +
# the chain's notable token). Anything else: pass a raw 0x address.
_TOKENS: dict[str, dict[str, str]] = {
    "ethereum": {
        "WETH": "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",
        "WBTC": "0x2260fac5e5542a773aa44fbcfedf7c193bc2c599",
        "USDC": "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",
        "UNI": "0x1f9840a85d5af5bf1d1762f925bdaddc4201f984",
        "DAI": "0x6b175474e89094c44da98b954eedeac495271d0f",
        "LINK": "0x514910771af9ca656af840dff83e8264ecf986ca",
    },
    "base": {
        "WETH": "0x4200000000000000000000000000000000000006",
        "USDC": "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
        "CBBTC": "0xcbb7c0000ab88b473b1f5afd9ef808440eed33bf",
        "AERO": "0x940181a94a35a4569e4529a3cdfb74e38fd98631",
    },
    "arbitrum": {
        "WETH": "0x82af49447d8a07e3bd95bd0d56f35241523fbab1",
        "USDC": "0xaf88d065e77c8cc2239327c5edb3a432268e5831",
        "ARB": "0x912ce59144191c1204e64559fe8253a0e49e6548",
        "WBTC": "0x2f2a2543b76a4166549f7aab2e75bef0aefc5b0f",
    },
    "polygon": {
        "WETH": "0x7ceb23fd6bc0add59e62ac25578270cff1b9f619",
        "USDC": "0x3c499c542cef5e3811e1192ce70d8cc03d5c3359",
        "WMATIC": "0x0d500b1d8e8ef31e21c99d1db9a6444d3adf1270",
        "WBTC": "0x1bfd67037b42cf73acf2047067bd4f2c47d9bfd6",
    },
}

DEFAULT_CHAIN = "ethereum"

# Back-compat: the Ethereum-mainnet token map, unqualified.
TOKENS = _TOKENS["ethereum"]

_QUERY = (
    '{ token(id: "%s") { symbol name decimals totalSupply volumeUSD txCount '
    'totalValueLockedUSD derivedETH } bundle(id: "1") { ethPriceUSD } }'
)


def chains(settings: Settings | None = None) -> dict[str, ChainSpec]:
    """Build the chain registry, with the Ethereum subgraph id taken from config."""
    s = settings or get_settings()
    specs = {
        DEFAULT_CHAIN: ChainSpec(
            key=DEFAULT_CHAIN,
            label="Uniswap v3 · Ethereum",
            subgraph_id=s.graph_subgraph_id,
            tokens=_TOKENS["ethereum"],
        )
    }
    for key, sub in _UNIV3_SUBGRAPHS.items():
        specs[key] = ChainSpec(
            key=key,
            label=f"Uniswap v3 · {key.capitalize()}",
            subgraph_id=sub,
            tokens=_TOKENS.get(key, {}),
        )
    return specs


def resolve(identifier: str, settings: Settings | None = None) -> tuple[str, ChainSpec]:
    """Parse ``SYMBOL[:chain]`` or ``0x…[:chain]`` → (lowercase address, ChainSpec)."""
    raw = identifier.strip()
    token_part, _, chain_part = raw.partition(":")
    chain_key = (chain_part or DEFAULT_CHAIN).strip().lower()
    registry = chains(settings)
    spec = registry.get(chain_key)
    if spec is None:
        raise ProcurementError(
            f"Unsupported chain {chain_key!r}. Supported: {', '.join(sorted(registry))}."
        )

    token_part = token_part.strip()
    if token_part.lower().startswith("0x") and len(token_part) == 42:
        return token_part.lower(), spec
    addr = spec.tokens.get(token_part.upper())
    if not addr:
        known = ", ".join(sorted(spec.tokens)) or "(none mapped)"
        raise ProcurementError(
            f"Unknown token {token_part!r} on {chain_key}. Known: {known}, "
            f"or pass a 0x address (e.g. 0x…:{chain_key})."
        )
    return addr, spec


def resolve_token(identifier: str) -> str:
    """Back-compat: resolve to a lowercase address (chain-aware; default Ethereum)."""
    return resolve(identifier)[0]


class GraphSource:
    name = "thegraph"
    is_paid = True
    price_estimate_usd = 0.05  # well under the per-query budget; actual ≈ $0.0002 live

    def __init__(self, buy_fn=pay):
        # buy_fn is injectable so tests can avoid the network.
        self._buy = buy_fn

    def _url(self, settings: Settings, spec: ChainSpec) -> str:
        if settings.graph_live:
            return f"{settings.graph_gateway_url}/subgraphs/id/{spec.subgraph_id}"
        host = "localhost" if settings.seller_host in ("0.0.0.0", "") else settings.seller_host
        return f"http://{host}:{settings.seller_port}/mock-graph/subgraphs/id/{spec.subgraph_id}"

    async def gather(self, identifier: str, *, budget: BudgetCap, store: Store) -> GatherResult:
        settings = get_settings()
        addr, spec = resolve(identifier, settings)
        url = self._url(settings, spec)
        result = await procure(
            source_name=self.name,
            # chain-qualified so cross-chain data never collides in the cache
            cache_key=f"{spec.key}:{spec.subgraph_id}:{addr}",
            url=url,
            method="POST",
            json_body={"query": _QUERY % addr},
            network=settings.graph_buy_network,
            price_estimate_usd=self.price_estimate_usd,
            private_key=settings.graph_buy_key,
            budget=budget,
            store=store,
            ttl_seconds=settings.purchase_cache_ttl_seconds,
            buy_fn=self._buy,
        )
        snapshot = self._parse(result.payload, identifier, addr, spec)
        return GatherResult(
            snapshot=snapshot, cost_in_usd=result.cost_in_usd, cache_hit=result.cache_hit
        )

    def _parse(self, payload: dict, identifier: str, addr: str, spec: ChainSpec) -> Snapshot:
        data = (payload or {}).get("data") or {}
        token = data.get("token")
        bundle = data.get("bundle") or {}
        if not token:
            raise ProcurementError(
                f"The Graph returned no token entity for {identifier!r} on {spec.key}."
            )

        eth_price = float(bundle.get("ethPriceUSD", 0) or 0)
        derived_eth = float(token.get("derivedETH", 0) or 0)
        price = derived_eth * eth_price
        tvl = float(token.get("totalValueLockedUSD", 0) or 0)
        volume = float(token.get("volumeUSD", 0) or 0)
        txcount = float(token.get("txCount", 0) or 0)
        decimals = int(token.get("decimals", 18) or 18)
        supply = float(token.get("totalSupply", 0) or 0) / (10**decimals)
        mcap = price * supply

        url = f"https://thegraph.com/explorer/subgraphs/{spec.subgraph_id}"
        label = f"The Graph · {spec.label}"
        ref = spec.subgraph_id
        n = {"i": 0}

        def fact(lbl: str, value: float, unit: str, concept: str) -> Fact:
            n["i"] += 1
            return Fact(
                id=f"C{n['i']}",
                label=lbl,
                value=value,
                unit=unit,
                source_label=label,
                concept=concept,
                accession=ref,
                form="subgraph query",
                source_url=url,
            )

        facts = [
            fact("Price (USD)", price, USD, "derivedETH*ethPriceUSD"),
            fact("ETH price (USD)", eth_price, USD, "bundle.ethPriceUSD"),
            fact("Market cap (USD)", mcap, USD, "price*totalSupply"),
            fact("Liquidity / TVL (USD)", tvl, USD, "totalValueLockedUSD"),
            fact("Cumulative volume (USD)", volume, USD, "volumeUSD"),
            fact("Circulating supply", supply, COUNT, "totalSupply"),
            fact("On-chain transactions", txcount, COUNT, "txCount"),
        ]
        symbol = token.get("symbol", identifier.upper())
        name = token.get("name", symbol)
        return Snapshot(
            ticker=symbol,
            cik=addr,
            entity_name=f"{name} ({symbol}) · {spec.key}",
            facts=facts,
            as_of=None,
        )

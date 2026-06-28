"""The Graph as a paid Source (x402).

Buys on-chain token data (Uniswap v3 subgraph) per query. Two wirings, one code
path, chosen by ``GRAPH_LIVE``:
  - live  → gateway.thegraph.com on Base mainnet (real USDC)
  - mock  → our local /mock-graph vendor on Base Sepolia (free testnet USDC)

The mock returns the exact same JSON shape, so :meth:`_parse` is identical for
both. Every parsed number cites one thing: the subgraph query that produced it.
"""

from __future__ import annotations

from jim.buyer import pay
from jim.config import Settings, get_settings
from jim.research.budget import BudgetCap
from jim.research.facts import COUNT, USD, Fact, Snapshot
from jim.sources.base import GatherResult, ProcurementError, procure
from jim.store import Store

# Symbol → Uniswap v3 (Ethereum mainnet) token address.
TOKENS: dict[str, str] = {
    "WETH": "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",
    "WBTC": "0x2260fac5e5542a773aa44fbcfedf7c193bc2c599",
    "USDC": "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",
    "UNI": "0x1f9840a85d5af5bf1d1762f925bdaddc4201f984",
    "DAI": "0x6b175474e89094c44da98b954eedeac495271d0f",
    "LINK": "0x514910771af9ca656af840dff83e8264ecf986ca",
}

_QUERY = (
    '{ token(id: "%s") { symbol name decimals totalSupply volumeUSD txCount '
    'totalValueLockedUSD derivedETH } bundle(id: "1") { ethPriceUSD } }'
)


def resolve_token(identifier: str) -> str:
    """Accept a symbol (WETH) or a raw 0x address; return a lowercase address."""
    ident = identifier.strip()
    if ident.lower().startswith("0x") and len(ident) == 42:
        return ident.lower()
    addr = TOKENS.get(ident.upper())
    if not addr:
        raise ProcurementError(
            f"Unknown token {identifier!r}. Known: {', '.join(sorted(TOKENS))}, or pass a 0x address."
        )
    return addr


class GraphSource:
    name = "thegraph"
    is_paid = True
    price_estimate_usd = 0.05  # well under the per-query budget; actual ≈ $0.01

    def __init__(self, buy_fn=pay):
        # buy_fn is injectable so tests can avoid the network.
        self._buy = buy_fn

    def _url(self, settings: Settings) -> str:
        sub = settings.graph_subgraph_id
        if settings.graph_live:
            return f"{settings.graph_gateway_url}/subgraphs/id/{sub}"
        host = "localhost" if settings.seller_host in ("0.0.0.0", "") else settings.seller_host
        return f"http://{host}:{settings.seller_port}/mock-graph/subgraphs/id/{sub}"

    async def gather(self, identifier: str, *, budget: BudgetCap, store: Store) -> GatherResult:
        settings = get_settings()
        addr = resolve_token(identifier)
        url = self._url(settings)
        result = await procure(
            source_name=self.name,
            cache_key=f"{settings.graph_subgraph_id}:{addr}",
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
        snapshot = self._parse(result.payload, identifier, addr, settings)
        return GatherResult(
            snapshot=snapshot, cost_in_usd=result.cost_in_usd, cache_hit=result.cache_hit
        )

    def _parse(self, payload: dict, identifier: str, addr: str, settings: Settings) -> Snapshot:
        data = (payload or {}).get("data") or {}
        token = data.get("token")
        bundle = data.get("bundle") or {}
        if not token:
            raise ProcurementError(f"The Graph returned no token entity for {identifier!r}.")

        eth_price = float(bundle.get("ethPriceUSD", 0) or 0)
        derived_eth = float(token.get("derivedETH", 0) or 0)
        price = derived_eth * eth_price
        tvl = float(token.get("totalValueLockedUSD", 0) or 0)
        volume = float(token.get("volumeUSD", 0) or 0)
        txcount = float(token.get("txCount", 0) or 0)
        decimals = int(token.get("decimals", 18) or 18)
        supply = float(token.get("totalSupply", 0) or 0) / (10**decimals)
        mcap = price * supply

        url = f"https://thegraph.com/explorer/subgraphs/{settings.graph_subgraph_id}"
        label = "The Graph · Uniswap v3"
        ref = settings.graph_subgraph_id
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
            entity_name=f"{name} ({symbol})",
            facts=facts,
            as_of=None,
        )

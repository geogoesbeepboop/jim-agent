"""Mock 'The Graph' vendor — a testnet stand-in for gateway.thegraph.com.

Returns the EXACT Uniswap-v3 subgraph JSON shape the real gateway returns, so
``GraphSource._parse`` is identical for mock and live. Numbers here are static
illustrative placeholders (clearly not live market data) — their only job is to
exercise the buy → cache → budget → margin loop on Base Sepolia with free USDC.

Flip GRAPH_LIVE=true to buy the real thing on Base mainnet instead.
"""

from __future__ import annotations

import re

_ETH_PRICE_USD = 2500.0

# address -> (symbol, name, decimals, price_usd, supply, tvl_usd, volume_usd, tx_count)
_TOKENS: dict[str, tuple] = {
    "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2": (
        "WETH",
        "Wrapped Ether",
        18,
        2500.0,
        3_000_000,
        500_000_000,
        900_000_000_000,
        12_500_000,
    ),
    "0x2260fac5e5542a773aa44fbcfedf7c193bc2c599": (
        "WBTC",
        "Wrapped BTC",
        8,
        64_000.0,
        150_000,
        200_000_000,
        50_000_000_000,
        1_200_000,
    ),
    "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48": (
        "USDC",
        "USD Coin",
        6,
        1.0,
        30_000_000_000,
        800_000_000,
        2_000_000_000_000,
        25_000_000,
    ),
    "0x1f9840a85d5af5bf1d1762f925bdaddc4201f984": (
        "UNI",
        "Uniswap",
        18,
        8.0,
        600_000_000,
        60_000_000,
        30_000_000_000,
        3_000_000,
    ),
    "0x6b175474e89094c44da98b954eedeac495271d0f": (
        "DAI",
        "Dai Stablecoin",
        18,
        1.0,
        4_000_000_000,
        120_000_000,
        200_000_000_000,
        5_000_000,
    ),
    "0x514910771af9ca656af840dff83e8264ecf986ca": (
        "LINK",
        "ChainLink Token",
        18,
        14.0,
        1_000_000_000,
        40_000_000,
        20_000_000_000,
        2_000_000,
    ),
}

_ID_RE = re.compile(r'token\s*\(\s*id:\s*"(0x[0-9a-fA-F]{40})"', re.IGNORECASE)


def _address_from_query(query: str) -> str | None:
    m = _ID_RE.search(query or "")
    return m.group(1).lower() if m else None


def build_mock_response(query: str) -> dict:
    """Return a Uniswap-v3-shaped GraphQL response for the token in ``query``."""
    addr = _address_from_query(query)
    row = _TOKENS.get(addr) if addr else None
    if not row:
        return {"data": {"token": None, "bundle": {"ethPriceUSD": str(_ETH_PRICE_USD)}}}

    symbol, name, decimals, price_usd, supply, tvl, volume, txcount = row
    derived_eth = price_usd / _ETH_PRICE_USD
    return {
        "data": {
            "token": {
                "symbol": symbol,
                "name": name,
                "decimals": str(decimals),
                "totalSupply": str(int(supply * (10**decimals))),
                "volumeUSD": str(float(volume)),
                "txCount": str(txcount),
                "totalValueLockedUSD": str(float(tvl)),
                "derivedETH": str(derived_eth),
            },
            "bundle": {"ethPriceUSD": str(_ETH_PRICE_USD)},
        }
    }

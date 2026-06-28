"""Pre-compute popular tokens: warm the cache so later sales are pure margin.

    uv run jim-seller                          # serve the mock-Graph vendor
    uv run python scripts/precompute.py        # buy + cache WETH, WBTC, UNI, ...

The first run buys each token's data over x402 (cost_in > 0). Re-running, or any
later /research/token sale within the TTL, hits the cache (cost_in = 0) — that's
"buy a datum once, resell derived insight many times".
"""

from __future__ import annotations

import argparse
import asyncio

from jim.research.engine import run_research
from jim.sources.thegraph import TOKENS

DEFAULT = ["WETH", "WBTC", "UNI"]


async def _run(tokens: list[str]) -> int:
    for sym in tokens:
        result = await run_research(sym, product="token", mode="agent")
        c = result.cost
        tag = "cache hit" if c.get("cache_hit") else f"bought ${c.get('data_cost_usd', 0):.4f}"
        print(
            f"{sym:<6} {result.status:<9} {tag:<18} "
            f"margin ${c.get('margin_usd', 0):.4f}"
            + (f"   ({result.error})" if result.error else "")
        )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="precompute")
    parser.add_argument(
        "tokens",
        nargs="*",
        default=DEFAULT,
        help=f"Tokens to warm (default: {', '.join(DEFAULT)}; known: {', '.join(TOKENS)})",
    )
    args = parser.parse_args()
    return asyncio.run(_run(args.tokens or DEFAULT))


if __name__ == "__main__":
    raise SystemExit(main())

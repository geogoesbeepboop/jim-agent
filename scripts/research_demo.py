"""Pay for a fundamentals memo over x402, end-to-end.

Run the seller (needs EVM_ADDRESS + ANTHROPIC_API_KEY in .env):
    uv run jim-seller

Then buy a report (needs a funded EVM_PRIVATE_KEY):
    uv run python scripts/research_demo.py AAPL
    uv run python scripts/research_demo.py MSFT --mode agent

This exercises the same x402 buy client jim will later use to purchase upstream
data — here it's buying our own product.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from jim.buyer import fetch_paid
from jim.config import get_settings


async def _run(ticker: str, mode: str) -> int:
    settings = get_settings()
    host = "localhost" if settings.seller_host in ("0.0.0.0", "") else settings.seller_host
    url = f"http://{host}:{settings.seller_port}/research/fundamentals?ticker={ticker}&mode={mode}"

    print(
        f"Paying {settings.research_price} for a {mode} fundamentals memo on {ticker.upper()} ...\n"
    )
    result = await fetch_paid(url)
    print(f"HTTP {result.status_code}   paid: {result.paid}\n")

    try:
        data = json.loads(result.text)
    except json.JSONDecodeError:
        print(result.text, file=sys.stderr)
        return 1

    if result.status_code != 200:
        print(f"Error: {data}", file=sys.stderr)
        return 1

    print("=" * 70)
    print(f"{data.get('company')} ({data['ticker']}) — status {data['status'].upper()}")
    print("=" * 70)
    print(f"\n{data.get('memo')}\n")
    src = data.get("sourcing") or {}
    print(
        f"Sourcing: {'PASS' if src.get('passed') else 'FAIL'} "
        f"({src.get('figures_covered')}/{src.get('figures_checked')} figures, "
        f"coverage {src.get('coverage', 0):.0%})"
    )
    ff = data.get("faithfulness") or {}
    if ff.get("evaluated"):
        print(f"Faithfulness: {ff.get('score')}")
    print(f"Inference cost: ${data.get('cost', {}).get('inference_cost_usd', 0):.5f}")
    print(f"\n{len(data.get('citations', []))} citations attached.")
    return 0 if data["status"] == "ok" else 1


def main() -> int:
    parser = argparse.ArgumentParser(prog="research_demo")
    parser.add_argument("ticker")
    parser.add_argument("--mode", choices=["human", "agent"], default="human")
    args = parser.parse_args()
    return asyncio.run(_run(args.ticker, args.mode))


if __name__ == "__main__":
    raise SystemExit(main())

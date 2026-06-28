"""End-to-end Phase 0 proof: pay our own paywalled /ping over x402.

Run the seller in one terminal:
    uv run jim-seller

Then in another:
    uv run python scripts/ping_demo.py

Exit 0 means a USDC payment settled on Base Sepolia for an API call we control,
acting as BOTH seller and buyer — the Phase 0 exit criterion.
"""

from __future__ import annotations

import asyncio
import json
import sys

from jim.buyer import fetch_paid
from jim.config import get_settings


async def _run() -> int:
    settings = get_settings()
    base = f"http://{'localhost' if settings.seller_host in ('0.0.0.0', '') else settings.seller_host}:{settings.seller_port}"
    url = f"{base}/ping"

    print(f"Paying {settings.ping_price} for {url} on {settings.network} ...\n")
    result = await fetch_paid(url)

    print(f"HTTP {result.status_code}")
    print(f"Body : {result.text}")

    if result.paid:
        print("\nSettlement receipt (X-PAYMENT-RESPONSE):")
        print(json.dumps(result.settlement, indent=2, default=str))
        print("\n✅ Phase 0 proven: payment settled for an endpoint we control.")
        return 0

    print(
        "\n❌ No settlement receipt. Either the endpoint was free, the payment "
        "failed, or the wallet is unfunded (need testnet ETH + USDC).",
        file=sys.stderr,
    )
    return 1


def main() -> int:
    return asyncio.run(_run())


if __name__ == "__main__":
    raise SystemExit(main())

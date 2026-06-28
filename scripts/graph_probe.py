"""Probe the configured Graph endpoint (live or mock) — decode the 402 price.

    uv run python scripts/graph_probe.py WETH

Unpaid: prints the advertised price + network from the payment-required header,
so you can confirm whether you're pointed at the real mainnet gateway or the
local Sepolia mock before spending anything.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import sys

import httpx

from jim.config import get_settings
from jim.sources.thegraph import GraphSource, resolve_token, _QUERY


async def _run(token: str) -> int:
    settings = get_settings()
    addr = resolve_token(token)
    url = GraphSource()._url(settings)
    mode = "LIVE (real USDC, mainnet)" if settings.graph_live else "MOCK (testnet)"
    print(f"Endpoint: {url}\nMode: {mode}\n")

    async with httpx.AsyncClient(timeout=30.0) as c:
        r = await c.post(url, json={"query": _QUERY % addr})
    print(f"HTTP {r.status_code}")
    raw = r.headers.get("payment-required")
    if not raw:
        print("No payment-required header (endpoint free or unreachable).", file=sys.stderr)
        return 1
    ch = json.loads(base64.b64decode(raw))
    a = ch["accepts"][0]
    print(f"Price: {int(a['amount']) / 1e6:.4f} USDC  on  {a['network']}  → payTo {a['payTo']}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(prog="graph_probe")
    p.add_argument("token", nargs="?", default="WETH")
    args = p.parse_args()
    return asyncio.run(_run(args.token))


if __name__ == "__main__":
    raise SystemExit(main())

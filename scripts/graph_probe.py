"""Audit the configured Graph endpoint (live or mock) — decode the real 402 price.

    uv run python scripts/graph_probe.py WETH
    uv run python scripts/graph_probe.py AERO:base        # multi-chain

The on-chain x402 price is dynamic and unpublished — it lives only in the 402
header. This is the pre-mainnet audit: it makes an UNPAID request, decodes the
advertised price, and checks it against the per-query data budget (the same cap
the live buy path enforces). A FAIL means going live would either overpay or be
refused by the price guard. Reads only; never spends. See ADR-0007.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import sys

import httpx

from jim.config import get_settings
from jim.sources.thegraph import GraphSource, _QUERY, resolve


async def _run(token: str) -> int:
    settings = get_settings()
    addr, spec = resolve(token, settings)
    url = GraphSource()._url(settings, spec)
    mode = "LIVE (real USDC, mainnet)" if settings.graph_live else "MOCK (testnet)"
    print(f"Token:    {token}  →  {addr}")
    print(f"Chain:    {spec.key}   subgraph {spec.subgraph_id}")
    print(f"Endpoint: {url}\nMode:     {mode}\n")

    async with httpx.AsyncClient(timeout=30.0) as c:
        r = await c.post(url, json={"query": _QUERY % addr})
    print(f"HTTP {r.status_code}")
    raw = r.headers.get("payment-required")
    if not raw:
        print("No payment-required header (endpoint free or unreachable).", file=sys.stderr)
        return 1
    a = json.loads(base64.b64decode(raw))["accepts"][0]
    price = int(a["amount"]) / 1e6
    cap = settings.per_query_budget_usd
    print(f"Price:    {price:.6f} USDC  on  {a['network']}  → payTo {a['payTo']}")
    print(f"Budget:   per-query data ceiling ${cap:.4f}")

    ok = price <= cap
    verdict = "PASS — within budget; the live buy path would settle this" if ok else (
        "FAIL — exceeds the per-query budget; the price guard would REFUSE this buy"
    )
    print(f"\nAudit:    {'✅' if ok else '⛔'} {verdict}")
    return 0 if ok else 2


def main() -> int:
    p = argparse.ArgumentParser(prog="graph_probe")
    p.add_argument("token", nargs="?", default="WETH", help="SYMBOL or SYMBOL:chain or 0x…:chain")
    args = p.parse_args()
    return asyncio.run(_run(args.token))


if __name__ == "__main__":
    raise SystemExit(main())

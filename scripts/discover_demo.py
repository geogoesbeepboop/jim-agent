"""Discover jim the way another agent would (Phase 5).

Run the seller (no key needed for discovery):
    uv run jim-seller

Then, from another terminal:
    uv run python scripts/discover_demo.py

This fetches the free discovery surface — the /.well-known/x402 manifest and the
/catalog — and prints what an agent learns: our network + settlement asset, every
product's call shape + price, and our MCP endpoint. No payment happens; discovery
is free. (To then *pay* for a report, see scripts/research_demo.py.)
"""

from __future__ import annotations

import asyncio
import sys

import httpx

from jim.config import get_settings


async def _run() -> int:
    settings = get_settings()
    host = "localhost" if settings.seller_host in ("0.0.0.0", "") else settings.seller_host
    base = f"http://{host}:{settings.seller_port}"

    async with httpx.AsyncClient(timeout=httpx.Timeout(15.0)) as c:
        try:
            manifest = (await c.get(f"{base}/.well-known/x402")).json()
        except httpx.HTTPError as e:
            print(f"Could not reach {base} — is the seller running? ({e})", file=sys.stderr)
            return 1

    svc = manifest["service"]
    print("=" * 72)
    print(f"  Discovered: {svc['name']} — {svc['description']}")
    print("=" * 72)
    print(f"  Network : {manifest['network']}  (mainnet={manifest['is_mainnet']})")
    print(f"  Asset   : {manifest['asset']['symbol']} @ {manifest['asset']['address']}")
    print(f"  Pay to  : {manifest['pay_to']}")
    print(f"  MCP     : {manifest['mcp']['endpoint']}  tools={manifest['mcp']['tools']}")
    print("-" * 72)
    for r in manifest["resources"]:
        params = ", ".join(r["input_schema"].get("properties", {}))
        print(f"  {r['method']:<4} {r['resource']}")
        print(f"       ${r['price_usd']:.2f}  ·  params: {params}  ·  tags: {', '.join(r['tags'])}")
    print("-" * 72)
    print("  Pricing tiers:")
    for product, tiers in manifest["pricing"].items():
        schedule = "  ".join(f"{t['name']}=${t['price_usd']:.2f}" for t in tiers)
        print(f"    {product:<13} {schedule}")
    print("=" * 72)
    print("  Discovery is free. To pay for a report: scripts/research_demo.py")
    return 0


def main() -> int:
    return asyncio.run(_run())


if __name__ == "__main__":
    raise SystemExit(main())

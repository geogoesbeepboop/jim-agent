"""``jim-market`` — inspect the marketplace surface (Phase 5).

    uv run jim-market catalog       # what jim sells (routes, prices, tiers, sources)
    uv run jim-market pricing        # the published pricing schedule
    uv run jim-market manifest       # the /.well-known/x402 discovery manifest (JSON)
    uv run jim-market agent-card     # the A2A agent card (JSON)
    uv run jim-market mainnet        # the mainnet-cutover readiness preflight

The companion ``jim-map`` renders the live system diagram; ``jim-mcp`` runs the
MCP server.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from jim.a2a.card import agent_card
from jim.config import get_settings
from jim.marketplace.catalog import build_catalog
from jim.marketplace.discovery import discovery_manifest
from jim.marketplace.mainnet import check_mainnet_readiness
from jim.marketplace.pricing import tiers_for

_STATUS_MARK = {"ok": "✓", "info": "·", "warn": "!", "fail": "✗"}


def _catalog() -> int:
    listings = build_catalog()
    s = get_settings()
    print(f"{s.service_name} — {len(listings)} product(s) on {s.network}\n")
    for listing in listings:
        paid = " · buys upstream over x402" if listing.paid_upstream else ""
        print(f"  {listing.title}  [{listing.product}]")
        print(f"    {listing.route_key}  →  ${listing.price_usd:.2f}")
        print(f"    {listing.identifier_param}=<{listing.identifier_example}>  ·  source: {listing.source_name}{paid}")
        print(f"    upstream: {listing.upstream}")
        print(f"    tags: {', '.join(listing.tags)}")
        tiers = "  ".join(f"{t.name}=${t.price_usd:.2f}/{t.unit}" for t in listing.tiers)
        print(f"    tiers: {tiers}\n")
    return 0


def _pricing() -> int:
    for listing in build_catalog():
        print(f"{listing.title} [{listing.product}]")
        for t in tiers_for(listing.product):
            print(f"  {t.name:<9} ${t.price_usd:<8.4f} {t.unit:<22} {t.description}")
        print()
    return 0


def _manifest() -> int:
    print(json.dumps(discovery_manifest(), indent=2))
    return 0


def _agent_card() -> int:
    # The 1.0 card reflects a base URL; use the configured public URL for the CLI.
    print(json.dumps(agent_card(get_settings().public_url), indent=2))
    return 0


async def _mainnet() -> int:
    readiness = await check_mainnet_readiness()
    print(f"Mainnet readiness — network {readiness.network}  "
          f"({'MAINNET' if readiness.is_mainnet else 'testnet'})\n")
    for c in readiness.checks:
        print(f"  {_STATUS_MARK.get(c.status, '?')} [{c.status:<4}] {c.name:<16} {c.detail}")
    n_warn = len(readiness.warnings)
    if not readiness.ready:
        verdict = "NOT ready (resolve the ✗ items)"
    elif n_warn:
        verdict = f"clear of blockers, {n_warn} warning(s) to review before cutover"
    else:
        verdict = "READY to cut over"
    print(f"\n  → {verdict}.")
    return 0 if readiness.ready else 1


def main() -> int:
    p = argparse.ArgumentParser(prog="jim-market", description="Inspect jim's marketplace surface.")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("catalog", help="List products (routes, prices, tiers, sources)")
    sub.add_parser("pricing", help="Published pricing schedule")
    sub.add_parser("manifest", help="Discovery manifest JSON (/.well-known/x402)")
    sub.add_parser("agent-card", help="A2A agent card JSON (/.well-known/agent-card.json)")
    sub.add_parser("mainnet", help="Mainnet-cutover readiness preflight")
    args = p.parse_args()

    if args.cmd == "catalog":
        return _catalog()
    if args.cmd == "pricing":
        return _pricing()
    if args.cmd == "manifest":
        return _manifest()
    if args.cmd == "agent-card":
        return _agent_card()
    if args.cmd == "mainnet":
        return asyncio.run(_mainnet())
    return 1


if __name__ == "__main__":
    sys.exit(main())

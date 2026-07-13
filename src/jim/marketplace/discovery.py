"""The discovery manifest (Phase 5) — "how an agent finds and pays jim".

Two complementary discovery paths:

  1. **Pull**: ``GET /.well-known/x402`` returns this manifest — a single document
     an agent can fetch to learn our identity, network, settlement asset, pay-to
     address, every product's call shape + price, and our MCP endpoint.
  2. **Index**: each paid route carries a Bazaar discovery extension
     (:mod:`jim.marketplace.catalog`), so the first successful settlement lets a
     Bazaar-speaking facilitator auto-catalog us with no manual submission.

The manifest is **deterministic** — built from config + the catalog, with no
timestamps or run-specific state — so two calls return byte-identical bytes and
it can be cached/signed freely.
"""

from __future__ import annotations

from jim.config import get_settings
from jim.marketplace.catalog import Listing, build_catalog
from jim.marketplace.pricing import pricing_schedule


def _resource_entry(listing: Listing, base_url: str) -> dict:
    return {
        "product": listing.product,
        "resource": listing.resource_url(base_url),
        "method": listing.verb,
        "price_usd": listing.price_usd,
        "mime_type": "application/json",
        "tags": listing.tags,
        "input_schema": listing.input_schema,
        "output_schema": listing.output_schema,
    }


def discovery_manifest(base_url: str | None = None) -> dict:
    """The full machine-readable service manifest."""
    s = get_settings()
    base = (base_url or s.public_url).rstrip("/")
    listings = build_catalog()
    return {
        "x402Version": 2,
        "service": {
            "name": s.service_name,
            "description": s.service_description,
            "url": base,
            "icon_url": s.service_icon_url,
            "tags": s.service_tags,
        },
        "network": s.network,
        "is_mainnet": s.is_mainnet,
        "asset": {"address": s.usdc_address, "symbol": "USDC", "decimals": 6},
        "pay_to": s.evm_address,
        "facilitator": s.facilitator_url,
        "resources": [_resource_entry(listing, base) for listing in listings],
        "pricing": pricing_schedule(),
        "mcp": {
            "transport": "stdio | streamable-http",
            "endpoint": f"{base}/mcp",
            "tools": [f"research_{listing.product}" for listing in listings],
            "note": "Tools are x402-gated: the MCP call triggers the 402 → pay → settle cycle.",
        },
        # The A2A agent card — a link, not an embed: task delegation is its own
        # document (see jim.a2a.card, served at /.well-known/agent-card.json).
        "agent_card": f"{base}/.well-known/agent-card.json",
        "trust": {
            "sourcing_gate": "deterministic; every published figure must match a cited fact",
            "impersonal": "general analysis only — no personalized advice (publisher's-exclusion lane)",
        },
    }

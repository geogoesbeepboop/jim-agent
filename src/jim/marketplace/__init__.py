"""Phase 5: marketplace, discovery, mainnet.

The pieces that let other agents *find* jim and pay it, publish a pricing
schedule, expose jim as an MCP server, render the system as a whole, and guard
the cutover to Base mainnet.

  - :mod:`jim.marketplace.catalog`    — the machine-discoverable product list + Bazaar extensions
  - :mod:`jim.marketplace.pricing`    — published pricing tiers (deterministic)
  - :mod:`jim.marketplace.discovery`  — the /.well-known/x402 manifest
  - :mod:`jim.a2a.card`               — the A2A 1.0 agent card (task delegation; re-exported here)
  - :mod:`jim.marketplace.mcp_server` — jim as an x402-gated MCP server
  - :mod:`jim.marketplace.sysmap`     — the live Mermaid system map
  - :mod:`jim.marketplace.mainnet`    — the mainnet-cutover readiness preflight
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from jim.marketplace.catalog import Listing, build_catalog, listing_for, product_names
from jim.marketplace.discovery import discovery_manifest
from jim.marketplace.pricing import PricingTier, price_for, pricing_schedule, tiers_for

if TYPE_CHECKING:
    from jim.a2a.card import agent_card

__all__ = [
    "Listing",
    "build_catalog",
    "listing_for",
    "product_names",
    "agent_card",
    "discovery_manifest",
    "PricingTier",
    "tiers_for",
    "price_for",
    "pricing_schedule",
]


def __getattr__(name: str):
    # Lazy re-export: jim.a2a.card imports jim.marketplace.catalog, so pulling
    # agent_card at module-load time would cycle back through this package while
    # it is still initializing. Resolve it on first access instead (PEP 562).
    if name == "agent_card":
        from jim.a2a.card import agent_card

        return agent_card
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

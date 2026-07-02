"""The A2A agent card (Phase 7) — "how a peer agent *delegates* to jim".

The interop doc (docs/AGENT_INTEROP.md §1) draws the line that matters here:
**MCP exposes *tools*; A2A delegates *tasks*.** Calling ``research_fundamentals``
over MCP is a tool call — synchronous, known shape. "Produce cited research on
this ticker" is a *task*: jim's engine is already task-shaped (gather → debate →
synthesize → gate → judge), and the agent card, published at
``/.well-known/agent-card.json`` next to the x402 manifest, is what lets a peer
orchestrator *delegate* to jim rather than merely *call* it.

The card hand-builds Google's A2A ``AgentCard`` shape (no new dependency; the
protocol is just a JSON document) and extends it with two jim-specific blocks:

  - ``"x402"`` — jim's payment binding: network, settlement asset, pay-to
    address, per-skill pricing, and a link to the full x402 discovery manifest.
    This is jim's convention, not official A2A — A2A defines the task lifecycle,
    x402 defines how the task gets paid for.
  - ``"trust"`` — what makes jim safe to compose with: the deterministic
    sourcing gate, gate-derived per-source reputation (:mod:`jim.interop.trust`),
    and the propagated call-chain header that bounds payment loops and depth
    (:mod:`jim.interop.callchain`).

Skill ids reuse :func:`jim.marketplace.mcp_server.mcp_tool_name`, so a skill
maps 1:1 onto both the MCP tool and the paid HTTP route — one catalog, three
faces. Like the discovery manifest, the card is **deterministic** — built from
config + the catalog, with no timestamps or run-specific state — so two calls
return byte-identical output and it can be cached/signed freely.
"""

from __future__ import annotations

from jim.config import get_settings
from jim.interop.callchain import CALL_CHAIN_HEADER
from jim.marketplace.catalog import Listing, build_catalog
from jim.marketplace.mcp_server import mcp_tool_name

_JSON = "application/json"


def _skill(listing: Listing) -> dict:
    """One catalog listing as an A2A skill (id matches the MCP tool name)."""
    return {
        "id": mcp_tool_name(listing.product),
        "name": listing.title,
        "description": listing.description,
        "tags": listing.tags,
        "examples": [
            f"GET {listing.path}?{listing.identifier_param}={listing.identifier_example}"
        ],
        "inputModes": [_JSON],
        "outputModes": [_JSON],
    }


def agent_card(base_url: str | None = None) -> dict:
    """The A2A-style agent card a peer fetches to delegate tasks to jim."""
    s = get_settings()
    base = (base_url or s.public_url).rstrip("/")
    listings = build_catalog()
    return {
        "protocolVersion": "0.2",
        "name": s.service_name,
        "description": s.service_description,
        "url": base,
        "preferredTransport": "HTTP+JSON",
        "version": "0.1.0",
        "provider": {"organization": s.service_name, "url": base},
        "capabilities": {
            # Research is request/response today; monitors (Phase 4) deliver
            # push via HMAC-signed webhooks — A2A's push-notification lane.
            "streaming": False,
            "pushNotifications": True,
            "stateTransitionHistory": False,
        },
        "defaultInputModes": [_JSON],
        "defaultOutputModes": [_JSON],
        "skills": [_skill(listing) for listing in listings],
        # jim's payment binding (not official A2A): every skill is x402-gated.
        "x402": {
            "network": s.network,
            "asset": {"address": s.usdc_address, "symbol": "USDC", "decimals": 6},
            "pay_to": s.evm_address,
            "discovery": f"{base}/.well-known/x402",
            "pricing": {listing.product: listing.price_usd for listing in listings},
        },
        # Why jim is safe to compose with (the Phase 7 primitives).
        "trust": {
            "sourcing_gate": "deterministic; every published figure must match a cited fact",
            "reputation": "per-source gate pass-rate (Laplace-smoothed)",
            "peer_trust_floor": s.peer_trust_floor,
            "call_chain": {"header": CALL_CHAIN_HEADER, "max_depth": s.call_chain_max_depth},
        },
    }

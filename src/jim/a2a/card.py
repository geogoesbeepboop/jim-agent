"""The A2A 1.0 agent card — the SDK-validated task-delegation face of jim.

MCP exposes *tools*; A2A delegates *tasks*. "Produce cited research on this
ticker" is a task, and this card is what a peer orchestrator fetches to learn
that jim can run it, on which transports, for what price, under what payment
extension. A later stage swaps :func:`jim.marketplace.agentcard.agent_card` for
this one at ``/.well-known/agent-card.json``; the shapes differ because that
card is hand-built A2A-0.2 JSON while this one is built as the pinned SDK's
**protobuf** ``AgentCard`` and serialized with ``MessageToDict`` — so the SDK
itself validates the structure and camelCases the wire. If it round-trips
through ``ParseDict``, it is a well-formed 1.0 card.

Two deliberate departures from the old card (ADR-0010):

- The ad-hoc top-level ``"x402"`` and ``"trust"`` blocks are gone. Payment
  binding is not jim-private convention anymore — it rides the **x402 extension**
  declared under ``capabilities.extensions`` (network, pay-to, asset, per-skill
  pricing, discovery link), which is how an A2A client actually negotiates
  payment. The trust story moves into the description prose plus the discovery
  manifest the extension links to.
- ``stateTransitionHistory`` is **not** set: a2a-sdk 1.1.0's proto
  ``AgentCapabilities`` has no such field (only ``streaming`` /
  ``pushNotifications`` / ``extensions`` / ``extendedAgentCard``), so an
  SDK-validated card cannot carry it. jim serves streaming + push instead.

Like the old card, this is **deterministic** — config + catalog, no timestamps
or run state — so two calls are byte-identical and it can be cached or signed.
"""

from __future__ import annotations

import json

from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentExtension,
    AgentInterface,
    AgentProvider,
    AgentSkill,
)
from google.protobuf import json_format

from jim import __version__
from jim.a2a.extension import X402_EXT_URI
from jim.config import get_settings
from jim.marketplace.catalog import Listing, build_catalog
from jim.marketplace.mcp_server import mcp_tool_name
from jim.marketplace.pricing import price_for
from jim.research.products import usd

_JSON = "application/json"
_TEXT = "text/plain"
# Every skill accepts both the terse text grammar and a structured JSON DataPart;
# jim answers in JSON. No file modes anywhere (A2A allows them; jim ships none).
_INPUT_MODES = [_JSON, _TEXT]
_OUTPUT_MODES = [_JSON]

# One appended sentence carrying jim's composition-safety contract — the story
# the dropped "trust" block used to tell, now prose (the extension links the
# machine-readable discovery manifest).
_TRUST_SENTENCE = (
    " Sources are scored by their sourcing-gate pass-rate, call chains that would "
    "loop or run too deep are refused before any payment moves, and research that "
    "fails the deterministic sourcing gate is refused and never billed."
)

_MONITOR_DESC = (
    "Create a continuous monitor over a fundamentals or token target. You pay an "
    "activation price to start it, then a per-update price only when a material, "
    "cited change is pushed — quiet polls cost nothing. The update artifact is "
    "withheld until that update's payment settles, and an unpaid alert pauses the "
    "monitor until it is cleared. Poll interval and the per-context cap on active "
    "monitors are configurable (defaults: 30-minute minimum interval, 5 active)."
)


def _price(value) -> float:
    """Coerce a price to a float. Monitor prices are ``"$0.10"`` strings today;
    tolerate a float in case a parallel setting lands as one (see ``agent_card``)."""
    return usd(value) if isinstance(value, str) else float(value)


def _compact(payload: dict) -> str:
    """A stable, compact JSON string for a DataPart example (deterministic keys)."""
    return json.dumps(payload, separators=(",", ":"))


def _research_skill(listing: Listing) -> AgentSkill:
    """One catalog listing as an A2A skill (id == the MCP tool name)."""
    param = listing.identifier_param
    example = listing.identifier_example
    text_form = f"research {listing.product} {example} mode=agent"
    data_form = _compact(
        {
            "kind": "data",
            "data": {"skill": mcp_tool_name(listing.product), param: example, "mode": "agent"},
        }
    )
    return AgentSkill(
        id=mcp_tool_name(listing.product),
        name=listing.title,
        description=listing.description,
        tags=list(listing.tags),
        examples=[text_form, data_form],
        input_modes=_INPUT_MODES,
        output_modes=_OUTPUT_MODES,
    )


def _monitor_skill() -> AgentSkill:
    """The A2A-only ``monitor_create`` skill (no MCP/HTTP twin — see ADR-0010)."""
    data_form = _compact(
        {
            "kind": "data",
            "data": {
                "skill": "monitor_create",
                "product": "fundamentals",
                "ticker": "AAPL",
                "every": "1d",
                "watch": ["price:5", "filing"],
            },
        }
    )
    return AgentSkill(
        id="monitor_create",
        name="Create monitor",
        description=_MONITOR_DESC,
        tags=["monitor", "fundamentals", "token", "push", "cited"],
        examples=["monitor fundamentals AAPL every=1d watch=price:5,filing", data_form],
        input_modes=_INPUT_MODES,
        output_modes=_OUTPUT_MODES,
    )


def _x402_extension(base: str, settings, listings: list[Listing]) -> AgentExtension:
    """The x402 payment binding as an ``AgentExtension`` (params is a proto Struct)."""
    pricing: dict = {
        listing.product: {
            "oneshot": price_for(listing.product, "oneshot"),
            "agent": price_for(listing.product, "agent"),
        }
        for listing in listings
    }
    pricing["monitor"] = {
        "activation": _price(settings.monitor_activation_price),
        "update": _price(settings.monitor_update_price),
    }
    params = {
        "network": settings.network,
        "payTo": settings.evm_address,
        "asset": {"address": settings.usdc_address, "symbol": "USDC", "decimals": 6},
        "discovery": f"{base}/.well-known/x402",
        "pricing": pricing,
    }
    ext = AgentExtension(
        uri=X402_EXT_URI,
        required=True,
        description="x402 settlement binding: a task is paid over x402 before any figure ships.",
    )
    # AgentExtension.params is a google.protobuf.Struct; ParseDict populates it
    # from a plain dict (ints widen to doubles, None -> null, map keys sorted).
    json_format.ParseDict(params, ext.params)
    return ext


def agent_card(base: str) -> dict:
    """The A2A 1.0 agent card as a JSON-ready camelCase dict (SDK-serialized)."""
    settings = get_settings()
    base = base.rstrip("/")
    listings = build_catalog()

    card = AgentCard(
        name=settings.service_name,
        description=settings.service_description + _TRUST_SENTENCE,
        version=__version__,
        provider=AgentProvider(organization=settings.service_name, url=base),
        # JSON-RPC first: it is the preferred / fully-v0.3 x402 binding (ADR-0010);
        # REST is best-effort second by position.
        supported_interfaces=[
            AgentInterface(
                url=f"{base}/a2a/jsonrpc", protocol_binding="JSONRPC", protocol_version="1.0"
            ),
            AgentInterface(
                url=f"{base}/a2a/rest", protocol_binding="HTTP+JSON", protocol_version="1.0"
            ),
        ],
        capabilities=AgentCapabilities(
            streaming=True,
            push_notifications=True,
            extensions=[_x402_extension(base, settings, listings)],
        ),
        default_input_modes=_INPUT_MODES,
        default_output_modes=_OUTPUT_MODES,
        skills=[_research_skill(listing) for listing in listings] + [_monitor_skill()],
    )
    if settings.service_icon_url:
        card.icon_url = settings.service_icon_url
    return json_format.MessageToDict(card)

"""Phase 7 A2A agent card — the task-delegation face of the marketplace.

All offline: the card is pure (config + catalog), so most tests need no wallet
or network; the route test uses a fresh LocalWallet + the in-memory store, and
no settlement happens (the card endpoint is free).
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from jim.config import Settings, get_settings
from jim.interop.callchain import CALL_CHAIN_HEADER
from jim.marketplace.agentcard import agent_card
from jim.marketplace.catalog import build_catalog
from jim.marketplace.discovery import discovery_manifest
from jim.marketplace.mcp_server import mcp_tool_name
from jim.seller.app import build_app
from jim.wallet import LocalWallet

BASE = "https://jim.example"


def _client() -> TestClient:
    wallet = LocalWallet.create()
    settings = Settings(evm_address=wallet.address, evm_private_key=wallet.private_key)
    return TestClient(build_app(settings))


def test_every_product_is_a_skill_with_the_mcp_tool_id() -> None:
    card = agent_card(BASE)
    skills = {s["id"]: s for s in card["skills"]}
    listings = build_catalog()
    assert set(skills) == {mcp_tool_name(listing.product) for listing in listings}
    for listing in listings:
        skill = skills[mcp_tool_name(listing.product)]
        assert skill["name"] == listing.title
        assert skill["description"] == listing.description
        assert skill["tags"] == listing.tags
        # The example is the literal HTTP call shape, straight from the catalog.
        assert skill["examples"] == [
            f"GET {listing.path}?{listing.identifier_param}={listing.identifier_example}"
        ]
        assert skill["inputModes"] == skill["outputModes"] == ["application/json"]
        # Per-skill price rides in the x402 binding, keyed by product.
        assert card["x402"]["pricing"][listing.product] == listing.price_usd


def test_card_declares_the_a2a_identity_fields() -> None:
    card = agent_card(BASE)
    s = get_settings()
    assert card["protocolVersion"] == "0.2"
    assert card["name"] == s.service_name
    assert card["description"] == s.service_description
    assert card["url"] == BASE
    assert card["preferredTransport"] == "HTTP+JSON"
    assert card["provider"] == {"organization": s.service_name, "url": BASE}
    # No streaming/task-history yet; monitors push via signed webhooks.
    assert card["capabilities"] == {
        "streaming": False,
        "pushNotifications": True,
        "stateTransitionHistory": False,
    }


def test_x402_block_binds_payment_to_the_card() -> None:
    x = agent_card(BASE)["x402"]
    s = get_settings()
    assert x["network"] == s.network
    assert x["pay_to"] == s.evm_address
    assert x["asset"] == {"address": s.usdc_address, "symbol": "USDC", "decimals": 6}
    assert x["discovery"] == f"{BASE}/.well-known/x402"


def test_trust_block_carries_the_composition_safety_contract() -> None:
    trust = agent_card(BASE)["trust"]
    s = get_settings()
    assert "deterministic" in trust["sourcing_gate"]
    assert "Laplace" in trust["reputation"]
    assert trust["peer_trust_floor"] == s.peer_trust_floor
    assert trust["call_chain"] == {
        "header": CALL_CHAIN_HEADER,
        "max_depth": s.call_chain_max_depth,
    }


def test_agent_card_is_deterministic() -> None:
    assert agent_card(BASE) == agent_card(BASE)


def test_manifest_links_the_card_without_embedding_it() -> None:
    m = discovery_manifest(BASE)
    assert m["agent_card"] == f"{BASE}/.well-known/agent-card.json"


def test_well_known_route_serves_the_card() -> None:
    resp = _client().get("/.well-known/agent-card.json")
    assert resp.status_code == 200
    card = resp.json()
    assert {s["id"] for s in card["skills"]} == {
        "research_fundamentals",
        "research_token",
        "research_macro",
    }
    # The card's URLs reflect the request host when no public_base_url is set.
    assert card["url"].startswith("http")
    assert card["x402"]["discovery"].endswith("/.well-known/x402")
    # pay_to mirrors the configured identity (None on a wallet-less machine —
    # the card still serves; a peer just can't pay until EVM_ADDRESS is set).
    assert card["x402"]["pay_to"] == get_settings().evm_address

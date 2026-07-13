"""S1b — the SDK-validated A2A 1.0 card + the x402 extension middleware.

All offline: the card is pure (config + catalog, no wallet/network), and the
middleware is exercised through a tiny in-process Starlette/FastAPI app over the
FastAPI ``TestClient`` — no server, no settlement. The old-card suite
(``tests/test_agentcard.py``) still guards the 0.2 card until S3 swaps routes;
this file guards the 1.0 replacement independently.
"""

from __future__ import annotations

import json

from a2a.types import AgentCard
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from google.protobuf import json_format

from jim import __version__
from jim.a2a.card import agent_card
from jim.a2a.extension import (
    EXT_HEADER,
    LEGACY_EXT_HEADER,
    X402_EXT_URI,
    X402ExtensionEchoMiddleware,
    is_activated,
    parse_extension_header,
)
from jim.config import get_settings
from jim.marketplace.catalog import build_catalog
from jim.marketplace.mcp_server import mcp_tool_name
from jim.marketplace.pricing import price_for
from jim.research.products import usd

BASE = "https://jim.example"
_MODES = {"application/json", "text/plain"}


# --- the card ---------------------------------------------------------------


def test_card_roundtrips_through_the_sdk_proto() -> None:
    """SDK-validated: the dict parses back into a proto AgentCard losslessly.

    ``ParseDict`` (unknown fields NOT ignored) raises on any stray/misspelled
    key, so a clean round-trip proves the structure is a well-formed 1.0 card.
    """
    d = agent_card(BASE)
    back = json_format.ParseDict(d, AgentCard())
    assert json_format.MessageToDict(back) == d


def test_card_identity_fields() -> None:
    card = agent_card(BASE)
    s = get_settings()
    assert card["name"] == s.service_name
    assert card["version"] == __version__
    # description = service_description + the appended trust-contract sentence.
    assert card["description"].startswith(s.service_description)
    assert len(card["description"]) > len(s.service_description)
    assert card["provider"] == {"organization": s.service_name, "url": BASE}


def test_supported_interfaces_are_two_entries_jsonrpc_first() -> None:
    ifaces = agent_card(BASE)["supportedInterfaces"]
    assert ifaces == [
        {"url": f"{BASE}/a2a/jsonrpc", "protocolBinding": "JSONRPC", "protocolVersion": "1.0"},
        {"url": f"{BASE}/a2a/rest", "protocolBinding": "HTTP+JSON", "protocolVersion": "1.0"},
    ]


def test_capabilities_flags_and_single_x402_extension() -> None:
    cap = agent_card(BASE)["capabilities"]
    assert cap["streaming"] is True
    assert cap["pushNotifications"] is True
    # No extended card; stateTransitionHistory is not a 1.1.0 proto field.
    assert "extendedAgentCard" not in cap
    assert "stateTransitionHistory" not in cap

    exts = cap["extensions"]
    assert len(exts) == 1
    ext = exts[0]
    assert ext["uri"] == X402_EXT_URI
    assert ext["required"] is True

    params = ext["params"]
    s = get_settings()
    assert params["network"] == s.network
    assert params["payTo"] == s.evm_address  # None on a wallet-less machine, still serves
    assert params["asset"] == {"address": s.usdc_address, "symbol": "USDC", "decimals": 6}
    assert params["discovery"] == f"{BASE}/.well-known/x402"
    assert "pricing" in params


def test_pricing_covers_research_tiers_and_monitor() -> None:
    pricing = agent_card(BASE)["capabilities"]["extensions"][0]["params"]["pricing"]
    for listing in build_catalog():
        assert pricing[listing.product]["oneshot"] == price_for(listing.product, "oneshot")
        assert pricing[listing.product]["agent"] == price_for(listing.product, "agent")
    assert pricing["monitor"]["update"] == usd(get_settings().monitor_update_price)
    assert "activation" in pricing["monitor"]


def test_four_skills_with_expected_ids_and_both_example_forms() -> None:
    card = agent_card(BASE)
    skills = card["skills"]
    listings = build_catalog()
    expected_ids = [mcp_tool_name(listing.product) for listing in listings] + ["monitor_create"]
    assert [s["id"] for s in skills] == expected_ids
    assert len(skills) == 4

    for skill in skills:
        text_form, data_form = skill["examples"]  # exactly two, both forms
        # Text grammar: verb-prefixed, human-typable.
        assert text_form.split()[0] in {"research", "monitor"}
        # JSON DataPart: parses, is a data part, and names its own skill.
        payload = json.loads(data_form)
        assert payload["kind"] == "data"
        assert payload["data"]["skill"] == skill["id"]
        assert skill["inputModes"] == ["application/json", "text/plain"]
        assert skill["outputModes"] == ["application/json"]


def test_no_file_modes_anywhere() -> None:
    card = agent_card(BASE)
    assert set(card["defaultInputModes"]) | set(card["defaultOutputModes"]) <= _MODES
    for skill in card["skills"]:
        assert set(skill["inputModes"]) | set(skill["outputModes"]) <= _MODES


def test_no_legacy_top_level_blocks() -> None:
    card = agent_card(BASE)
    # The 0.2 ad-hoc blocks and single-interface fields are gone.
    for dead in ("x402", "trust", "url", "preferredTransport", "protocolVersion"):
        assert dead not in card


def test_agent_card_is_deterministic() -> None:
    assert agent_card(BASE) == agent_card(BASE)


# --- the extension: parsing + activation ------------------------------------


def test_parse_extension_header() -> None:
    assert parse_extension_header("") == set()
    assert parse_extension_header(X402_EXT_URI) == {X402_EXT_URI}
    assert parse_extension_header(" a , b ,c ") == {"a", "b", "c"}
    assert parse_extension_header("a,,b,") == {"a", "b"}  # empty segments drop out


def test_is_activated() -> None:
    assert is_activated({X402_EXT_URI}) is True
    assert is_activated({X402_EXT_URI, "https://other/ext"}) is True
    assert is_activated({"https://other/ext"}) is False
    assert is_activated(set()) is False
    assert is_activated(None) is False


# --- the extension: middleware ----------------------------------------------


def _echo_client() -> TestClient:
    """A minimal app under /a2a (+ a control route outside it) behind the middleware."""
    app = FastAPI()

    @app.get("/a2a/thing")
    async def thing(request: Request):  # noqa: ANN202 - test fixture
        # Report the canonical header the app *saw*, to prove ingress normalization.
        return {"seen": request.headers.get(EXT_HEADER)}

    @app.get("/other")
    async def other(request: Request):  # noqa: ANN202 - test fixture
        return {"seen": request.headers.get(EXT_HEADER)}

    app.add_middleware(X402ExtensionEchoMiddleware)
    return TestClient(app)


def test_middleware_canonical_header_activates_and_echoes() -> None:
    r = _echo_client().get("/a2a/thing", headers={EXT_HEADER: X402_EXT_URI})
    assert r.json()["seen"] == X402_EXT_URI
    assert r.headers.get("a2a-extensions") == X402_EXT_URI


def test_middleware_legacy_header_is_normalized_and_echoed() -> None:
    r = _echo_client().get("/a2a/thing", headers={LEGACY_EXT_HEADER: X402_EXT_URI})
    # Ingress: the app sees the canonical header even though only the legacy one was sent.
    assert r.json()["seen"] == X402_EXT_URI
    # Egress: activation echoed back.
    assert r.headers.get("a2a-extensions") == X402_EXT_URI


def test_middleware_no_header_no_echo() -> None:
    r = _echo_client().get("/a2a/thing")
    assert r.json()["seen"] is None
    assert "a2a-extensions" not in r.headers


def test_middleware_non_a2a_path_is_untouched() -> None:
    # The header is present but the path is outside the mount → no echo.
    r = _echo_client().get("/other", headers={EXT_HEADER: X402_EXT_URI})
    assert r.json()["seen"] == X402_EXT_URI  # ordinary request header, not stripped
    assert "a2a-extensions" not in r.headers


def test_middleware_other_extension_is_not_echoed() -> None:
    other = "https://example/other-ext"
    r = _echo_client().get("/a2a/thing", headers={EXT_HEADER: other})
    assert r.json()["seen"] == other
    assert "a2a-extensions" not in r.headers  # only x402 is echoed

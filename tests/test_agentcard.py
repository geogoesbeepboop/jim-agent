"""The A2A 1.0 agent card as *served* by the seller (S3 route-level lock).

The card's unit-level shape — proto round-trip, skills, pricing, the dropped 0.2
blocks — is covered by ``tests/test_a2a_card.py``. This file guards the *served
surface*: that ``GET /.well-known/agent-card.json`` returns the new
SDK-validated card, reflects the request host, advertises both A2A bindings and
the required x402 extension, and stays linked from the ``/.well-known/x402``
manifest. All offline: a fresh wallet + the in-memory store, the card route is
free (no settlement).
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from jim.a2a.extension import X402_EXT_URI
from jim.config import Settings
from jim.seller.app import build_app
from jim.wallet import LocalWallet

CARD_PATH = "/.well-known/agent-card.json"


def _client(base_url: str = "http://testserver") -> TestClient:
    wallet = LocalWallet.create()
    settings = Settings(evm_address=wallet.address, evm_private_key=wallet.private_key)
    return TestClient(build_app(settings), base_url=base_url)


def test_well_known_route_serves_the_new_card() -> None:
    resp = _client().get(CARD_PATH)
    assert resp.status_code == 200
    card = resp.json()
    # 1.0 shape (not the 0.2 card): supportedInterfaces + capabilities.extensions.
    assert "supportedInterfaces" in card
    assert "extensions" in card["capabilities"]


def test_served_card_reflects_the_request_host() -> None:
    # The url fields are built from the request base, not a baked-in constant:
    # two hosts yield two cards, each self-consistent with its own host.
    card_a = _client(base_url="http://alpha.test").get(CARD_PATH).json()
    card_b = _client(base_url="http://beta.test").get(CARD_PATH).json()
    assert card_a["provider"]["url"] == "http://alpha.test"
    assert card_b["provider"]["url"] == "http://beta.test"
    assert card_a["supportedInterfaces"][0]["url"] == "http://alpha.test/a2a/jsonrpc"
    assert card_b["supportedInterfaces"][0]["url"] == "http://beta.test/a2a/jsonrpc"


def test_supported_interfaces_point_at_both_bindings_jsonrpc_first() -> None:
    ifaces = _client().get(CARD_PATH).json()["supportedInterfaces"]
    assert ifaces == [
        {
            "url": "http://testserver/a2a/jsonrpc",
            "protocolBinding": "JSONRPC",
            "protocolVersion": "1.0",
        },
        {
            "url": "http://testserver/a2a/rest",
            "protocolBinding": "HTTP+JSON",
            "protocolVersion": "1.0",
        },
    ]


def test_x402_extension_is_present_and_required() -> None:
    exts = _client().get(CARD_PATH).json()["capabilities"]["extensions"]
    assert len(exts) == 1
    assert exts[0]["uri"] == X402_EXT_URI
    assert exts[0]["required"] is True


def test_served_card_has_four_skills() -> None:
    skills = _client().get(CARD_PATH).json()["skills"]
    ids = {s["id"] for s in skills}
    assert ids == {"research_fundamentals", "research_token", "research_macro", "monitor_create"}
    assert len(skills) == 4


def test_served_card_drops_the_legacy_top_level_blocks() -> None:
    card = _client().get(CARD_PATH).json()
    # The 0.2 ad-hoc payment/trust blocks and single-interface fields are gone;
    # payment now rides the x402 extension, trust moved into the description.
    for dead in ("x402", "trust", "url", "preferredTransport", "protocolVersion"):
        assert dead not in card


def test_served_card_is_deterministic_across_requests() -> None:
    client = _client()
    assert client.get(CARD_PATH).json() == client.get(CARD_PATH).json()


def test_card_stays_linked_from_the_x402_manifest() -> None:
    client = _client()
    manifest = client.get("/.well-known/x402").json()
    assert manifest["agent_card"] == "http://testserver" + CARD_PATH
    # And the link resolves to the served card on the same host.
    assert client.get(manifest["agent_card"].replace("http://testserver", "")).status_code == 200

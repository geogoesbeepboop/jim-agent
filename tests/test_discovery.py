"""Phase 5 discovery + storefront endpoints, exercised through the seller app.

Offline: a fresh wallet + the in-memory store; no settlement actually happens
(we only inspect the 402 challenge and the free discovery/UI surface).
"""

from __future__ import annotations

import base64
import json

from fastapi.testclient import TestClient

from jim.config import Settings
from jim.seller.app import build_app
from jim.wallet import LocalWallet


def _client() -> TestClient:
    wallet = LocalWallet.create()
    settings = Settings(evm_address=wallet.address, evm_private_key=wallet.private_key)
    return TestClient(build_app(settings))


def _challenge(resp) -> dict:
    return json.loads(base64.b64decode(resp.headers["payment-required"]))


def test_paid_route_402_advertises_bazaar_discovery() -> None:
    resp = _client().get("/research/fundamentals?ticker=AAPL")
    assert resp.status_code == 402
    challenge = _challenge(resp)
    # The Bazaar extension rides at the PaymentRequired top level (v2). The HTTP
    # method is enriched in by the server extension at request time.
    bazaar = challenge["extensions"]["bazaar"]
    assert bazaar["info"]["input"]["method"] == "GET"
    assert "ticker" in bazaar["info"]["input"]["queryParams"]
    # It declares the output contract so an indexer knows what a buyer gets.
    example = bazaar["info"]["output"]["example"]
    assert example["product"] == "fundamentals"
    assert "citations" in example


def test_catalog_endpoint_lists_products() -> None:
    resp = _client().get("/catalog")
    assert resp.status_code == 200
    body = resp.json()
    products = {p["product"] for p in body["products"]}
    assert products == {"fundamentals", "token", "macro"}
    assert all("resource" in p and "price_usd" in p for p in body["products"])


def test_pricing_endpoint_publishes_tiers() -> None:
    body = _client().get("/pricing").json()
    assert set(body["pricing"]) == {"fundamentals", "token", "macro"}
    names = {t["name"] for t in body["pricing"]["fundamentals"]}
    assert names == {"oneshot", "agent", "bundle", "monitor"}


def test_well_known_manifest() -> None:
    body = _client().get("/.well-known/x402").json()
    assert body["x402Version"] == 2
    assert body["asset"]["symbol"] == "USDC"
    assert {r["product"] for r in body["resources"]} == {"fundamentals", "token", "macro"}
    # Resource URLs reflect the request host when no public_base_url is set.
    assert all(r["resource"].startswith("http") for r in body["resources"])


def test_mainnet_readiness_endpoint() -> None:
    body = _client().get("/mainnet/readiness").json()
    assert "ready" in body and isinstance(body["checks"], list)


def test_system_map_endpoints() -> None:
    c = _client()
    mmd = c.get("/map.mmd")
    assert mmd.status_code == 200 and mmd.text.startswith("flowchart LR")
    graph = c.get("/map.json").json()
    assert {"groups", "nodes", "edges"} <= set(graph)
    html = c.get("/map")
    assert html.status_code == 200 and "mermaid" in html.text


def test_storefront_renders_products() -> None:
    html = _client().get("/").text
    assert "Company Fundamentals" in html
    assert "On-chain Token Snapshot" in html
    assert "/ui/checkout" in html  # the page wires the checkout call


def test_health_and_ping_unchanged() -> None:
    c = _client()
    assert c.get("/health").status_code == 200
    assert c.get("/ping").status_code == 402  # still paid

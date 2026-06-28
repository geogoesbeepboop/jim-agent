"""Seller monitor endpoints — free management API + a real run cycle.

Mirrors test_paywall.py's offline style: the monitor routes are NOT paywalled
(management is free; the *push* is the product), and a run exercises the engine
with a mocked source + no key (deterministic update fallback)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from jim.config import Settings, get_settings
from jim.monitors import engine
from jim.research.facts import INDEX, USD, Fact, Snapshot
from jim.research.products import Product
from jim.seller.app import build_app
from jim.sources.base import GatherResult
from jim.store import reset_store
from jim.wallet import LocalWallet


@pytest.fixture(autouse=True)
def _reset():
    reset_store()
    yield
    reset_store()


def _client() -> TestClient:
    wallet = LocalWallet.create()
    return TestClient(
        build_app(Settings(evm_address=wallet.address, evm_private_key=wallet.private_key))
    )


def test_monitor_management_is_free_not_paywalled():
    c = _client()
    r = c.post("/monitors", json={"identifier": "AAPL", "watch": ["price:5", "rsi:70/30"]})
    assert r.status_code == 200  # not 402
    body = r.json()
    assert body["identifier"] == "AAPL"
    assert {t["kind"] for t in body["triggers"]} == {"price_move", "threshold"}


def test_monitor_crud_and_feed():
    c = _client()
    mid = c.post("/monitors", json={"identifier": "MSFT", "watch": ["price:5"]}).json()["id"]

    assert len(c.get("/monitors").json()["monitors"]) == 1
    assert c.get(f"/monitors/{mid}").status_code == 200
    assert c.get("/monitors/nope").status_code == 404

    feed = c.get("/monitors/feed").json()
    assert feed["feed"] == [] and "materiality_rate" in feed["stats"]

    assert c.delete(f"/monitors/{mid}").json()["deleted"] == mid
    assert c.delete(f"/monitors/{mid}").status_code == 404


def test_natural_language_create_detects_token_product():
    c = _client()
    body = c.post(
        "/monitors", json={"identifier": "WETH", "describe": "watch on-chain TVL and price moves"}
    ).json()
    assert body["product"] == "token"
    assert any(t["kind"] == "price_move" for t in body["triggers"])


def test_run_endpoint_runs_a_cycle(monkeypatch):
    monkeypatch.setattr(get_settings(), "anthropic_api_key", None)  # deterministic fallback

    snap = Snapshot(
        ticker="AAPL",
        cik="0000320193",
        entity_name="Apple Inc.",
        as_of="2025-01-01",
        facts=[
            Fact(id="C1", label="Price", value=100.0, unit=USD, accession="a"),
            Fact(id="C2", label="RSI (14-day)", value=55.0, unit=INDEX),
        ],
    )

    class _Src:
        name, is_paid = "fake", False

        async def gather(self, identifier, *, budget, store):
            return GatherResult(snapshot=snap, cost_in_usd=0.0, cache_hit=False)

    monkeypatch.setattr(
        engine,
        "get_product",
        lambda name: Product(
            name="fundamentals", source=_Src(), price_out_usd=0.25, identifier_label="x"
        ),
    )

    c = _client()
    mid = c.post("/monitors", json={"identifier": "AAPL", "watch": ["price:5"]}).json()["id"]
    run = c.post(f"/monitors/{mid}/run").json()
    assert run["status"] == "baseline"  # first run just sets the baseline
    assert c.post("/monitors/nope/run").status_code == 404

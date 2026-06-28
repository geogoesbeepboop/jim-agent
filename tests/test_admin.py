"""The admin dashboard (settlements / revenue / on-chain audit) + the wallet
paywall surface + the cleaner storefront's pay-with-wallet action.

Offline: no facilitator settlement happens, but we assert the *surface* — the
admin endpoints are free, the audit view aggregates recorded receipts with
explorer links, the paywall is served to browsers but not agents, and the
storefront wires the wallet checkout.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

import pytest

from jim.admin import address_url, admin_dashboard, admin_html, render_text, tx_url
from jim.config import BASE_MAINNET, Settings
from jim.marketplace.ui import storefront_html
from jim.seller.app import build_app
from jim.store import get_store, reset_store
from jim.wallet import LocalWallet

BROWSER = {
    "Accept": "text/html",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
}
AGENT = {"Accept": "application/json"}


@pytest.fixture(autouse=True)
def _reset():
    reset_store()
    yield
    reset_store()


def _client() -> TestClient:
    w = LocalWallet.create()
    return TestClient(build_app(Settings(evm_address=w.address, evm_private_key=w.private_key)))


async def _seed(store) -> None:
    await store.record_receipt(
        tx_hash="0xabc",
        payer="0xBuyer1",
        pay_to="0xSeller",
        amount_usdc=0.25,
        network="eip155:84532",
        path="/research/fundamentals",
        product="fundamentals",
        identifier="AAPL",
        mode="human",
        status_code=200,
        success=True,
        receipt={},
    )


# --- explorer link helpers ---------------------------------------------------


def test_explorer_urls_per_network() -> None:
    assert tx_url("eip155:84532", "0xT") == "https://sepolia.basescan.org/tx/0xT"
    assert tx_url(BASE_MAINNET, "0xT") == "https://basescan.org/tx/0xT"
    assert address_url(BASE_MAINNET, "0xA") == "https://basescan.org/address/0xA"
    assert tx_url("eip155:99999", "0xT") is None  # unknown network → no link
    assert tx_url("eip155:84532", None) is None


# --- admin dashboard data ----------------------------------------------------


async def test_admin_dashboard_decorates_audit_trail() -> None:
    store = get_store()
    await _seed(store)
    data = await admin_dashboard()
    assert data["summary"]["revenue_usdc"] == pytest.approx(0.25)
    assert data["summary"]["unique_buyers"] == 1
    row = data["recent"][0]
    assert row["tx_explorer_url"].endswith("/tx/0xabc")
    assert row["payer_explorer_url"].endswith("/address/0xBuyer1")
    # both renderers tolerate real data without throwing
    assert "audit" in admin_html(data).lower()
    assert "Settled revenue" in render_text(data)


async def test_admin_renderers_handle_empty() -> None:
    data = await admin_dashboard()
    assert "No settlements" in admin_html(data)
    assert "no settlements" in render_text(data).lower()


# --- seller endpoints: admin views are free ---------------------------------


def test_admin_endpoints_are_free() -> None:
    client = _client()
    assert client.get("/admin/audit").status_code == 200
    html = client.get("/admin")
    assert html.status_code == 200
    assert "audit" in html.text.lower()


# --- wallet paywall surface --------------------------------------------------


def test_paywall_served_to_browser_not_to_agent() -> None:
    client = _client()
    browser = client.get("/research/fundamentals?ticker=AAPL", headers=BROWSER)
    assert browser.status_code == 402
    assert "text/html" in browser.headers["content-type"]
    assert "window.x402" in browser.text  # the bundled wallet paywall config

    agent = client.get("/research/fundamentals?ticker=AAPL", headers=AGENT)
    assert agent.status_code == 402
    assert "payment-required" in agent.headers  # machine-readable challenge
    assert "window.x402" not in agent.text


# --- cleaner storefront wires the wallet checkout ---------------------------


def test_storefront_has_wallet_action_and_admin_link() -> None:
    html = storefront_html()
    assert "Pay with wallet" in html
    assert "Preview" in html
    assert "/admin" in html
    # the wallet button needs each product's paid path to build the checkout URL
    assert 'data-path="/research/fundamentals"' in html
    assert "window.open(" in html

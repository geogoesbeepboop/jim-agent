"""The public proof page (Horizon 1) — offline proofs of the transparency rollup.

The page's claim is that every number is a deterministic rollup of the same
store the seller writes. These tests seed that store and pin the math: pass
rate from the query ledger, refused-not-billed from rejected runs at current
prices, settlements decorated with explorer links, trust sorted by score —
and both surfaces (/proof, /proof.json) free to read.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from jim.config import Settings
from jim.marketplace.proof import proof_html, proof_stats
from jim.research.products import get_products
from jim.seller.app import build_app
from jim.store import get_store, reset_store
from jim.wallet import LocalWallet


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
        tx_hash="0xfeedbeef",
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
    # Three gated runs: two shipped verified, one refused (rejected, $0 booked).
    for status in ("ok", "ok", "rejected"):
        await store.record_query(
            product="fundamentals",
            identifier="AAPL",
            mode="human",
            status=status,
            price_out_usd=0.25 if status == "ok" else 0.0,
            cost_in_data_usd=0.0,
            cost_inference_usd=0.01,
            cache_hit=False,
            attempts=1,
        )
    await store.record_trust_event(source="fundamentals", ok=True, context="fundamentals:AAPL")
    await store.record_trust_event(source="peer:sent", ok=False, context="fundamentals:AAPL")


async def test_proof_stats_math() -> None:
    await _seed(get_store())
    data = await proof_stats()

    v = data["verification"]
    assert v["runs_gated"] == 3
    assert v["shipped_verified"] == 2
    assert v["refused_runs"] == 1
    assert v["gate_pass_rate"] == pytest.approx(2 / 3, abs=1e-4)
    # Refused money is priced at the CURRENT catalog price for the product.
    fundamentals_price = get_products()["fundamentals"].price_out_usd
    assert v["refused_not_billed_usd"] == pytest.approx(fundamentals_price)

    assert data["settlements"]["revenue_usdc"] == pytest.approx(0.25)
    assert data["recent_settlements"][0]["tx_explorer_url"].endswith("/tx/0xfeedbeef")
    assert data["recent_refusals"][0]["identifier"] == "AAPL"
    assert data["recent_refusals"][0]["declined_usd"] == pytest.approx(fundamentals_price)

    # Trust sorted by score, best first; outcome counts intact.
    assert [t["source"] for t in data["trust"]] == ["fundamentals", "peer:sent"]
    assert data["trust"][1]["fail"] == 1


async def test_proof_stats_empty_store() -> None:
    data = await proof_stats()
    v = data["verification"]
    assert v["runs_gated"] == 0
    assert v["gate_pass_rate"] is None
    assert v["refused_not_billed_usd"] == 0.0
    assert data["recent_settlements"] == []
    assert data["trust"] == []
    # The renderer tolerates the empty state without throwing.
    html = proof_html(data)
    assert "No settlements yet" in html
    assert "No gated runs yet" in html


async def test_proof_html_renders_the_claim_and_the_ledger() -> None:
    await _seed(get_store())
    html = proof_html(await proof_stats())
    assert "never billed" in html
    assert "refused — not billed" in html
    assert "0xfeedbeef"[:10] in html  # settlement row (shortened hash)
    assert "peer:sent" in html  # trust table
    assert "/proof.json" in html  # machine view linked


def test_proof_endpoints_are_free() -> None:
    client = _client()
    page = client.get("/proof")
    assert page.status_code == 200
    assert "proof" in page.text.lower()
    data = client.get("/proof.json")
    assert data.status_code == 200
    assert data.json()["verification"]["runs_gated"] == 0


def test_storefront_links_the_proof_page() -> None:
    from jim.marketplace.ui import storefront_html

    assert 'href="/proof"' in storefront_html()

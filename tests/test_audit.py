"""The settlement audit log (Payment & UI): the pure decode/classify helpers,
the MemoryStore receipt ledger, and the audit middleware end-to-end.

All offline — no wallet, network, or facilitator. The middleware test fakes the
``PAYMENT-RESPONSE`` header the x402 layer would write after a real settlement,
then asserts a receipt was persisted with the right buyer/tx/amount/query.
"""

from __future__ import annotations

import base64
import json

import pytest
from fastapi import FastAPI, Response
from fastapi.testclient import TestClient

from jim.seller.audit import (
    PaymentAuditMiddleware,
    _amount_to_usdc,
    classify_request,
    decode_settlement,
    settlement_header,
)
from jim.store import get_store, reset_store


@pytest.fixture(autouse=True)
def _reset():
    reset_store()
    yield
    reset_store()


def _settle_header(**fields) -> str:
    body = {
        "success": True,
        "payer": "0xBuyerAddressAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        "transaction": "0xtxhashBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB",
        "network": "eip155:84532",
        "amount": "250000",
        "payTo": "0xSellerCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC",
        **fields,
    }
    return base64.b64encode(json.dumps(body).encode()).decode()


# --- pure helpers ------------------------------------------------------------


def test_amount_base_units_become_usdc() -> None:
    assert _amount_to_usdc("250000") == 0.25  # base units / 1e6
    assert _amount_to_usdc("10000") == 0.01
    assert _amount_to_usdc("0.50") == 0.50  # already decimal → as-is
    assert _amount_to_usdc(None) == 0.0
    assert _amount_to_usdc("garbage") == 0.0


def test_decode_settlement_roundtrip() -> None:
    dec = decode_settlement(_settle_header())
    assert dec is not None
    assert dec["payer"].startswith("0xBuyer")
    assert dec["transaction"].startswith("0xtxhash")
    assert dec["amount_usdc"] == 0.25
    assert dec["success"] is True


def test_decode_settlement_rejects_junk() -> None:
    assert decode_settlement(None) is None
    assert decode_settlement("") is None
    assert decode_settlement("not-valid-base64-$$$") is None
    # valid base64 but not JSON
    assert decode_settlement(base64.b64encode(b"hello").decode()) is None


def test_settlement_header_prefers_v2_then_v1() -> None:
    assert settlement_header({"PAYMENT-RESPONSE": "v2"}) == "v2"
    assert settlement_header({"X-PAYMENT-RESPONSE": "v1"}) == "v1"
    assert settlement_header({"content-type": "x"}) is None


def test_classify_request_maps_path_and_identifier() -> None:
    assert classify_request("/research/fundamentals", {"ticker": "aapl"})[:2] == (
        "fundamentals",
        "aapl",
    )
    assert classify_request("/research/token", {"token": "WETH"})[:2] == ("token", "WETH")
    assert classify_request("/ping", {})[0] == "ping"
    assert classify_request("/mock-graph/subgraphs/id/x", {})[0] == "mock-graph"
    assert classify_request("/health", {})[0] is None


# --- MemoryStore receipt ledger ---------------------------------------------


async def _record(store, **over):
    base = dict(
        tx_hash="0xtx1",
        payer="0xAAA",
        pay_to="0xSeller",
        amount_usdc=0.25,
        network="eip155:84532",
        path="/research/fundamentals",
        product="fundamentals",
        identifier="AAPL",
        mode="human",
        status_code=200,
        success=True,
        receipt={"raw": True},
    )
    base.update(over)
    await store.record_receipt(**base)


async def test_receipts_summary_aggregates_revenue_and_buyers() -> None:
    store = get_store()
    await _record(store, tx_hash="0x1", payer="0xAAA", amount_usdc=0.25)
    await _record(store, tx_hash="0x2", payer="0xAAA", amount_usdc=0.50, product="token")
    await _record(store, tx_hash="0x3", payer="0xBBB", amount_usdc=0.25)
    # A failed settlement must not count toward revenue.
    await _record(store, tx_hash="0x4", payer="0xCCC", amount_usdc=9.99, success=False)

    summary = await store.receipts_summary()
    assert summary["settlements"] == 3
    assert summary["total_receipts"] == 4
    assert summary["revenue_usdc"] == pytest.approx(1.00)
    assert summary["unique_buyers"] == 2
    # 0xAAA is the top buyer at $0.75 across 2 payments.
    top = summary["top_buyers"][0]
    assert top["address"] == "0xaaa" and top["payments"] == 2
    assert top["spent_usdc"] == pytest.approx(0.75)
    by_product = {p["product"]: p for p in summary["by_product"]}
    assert by_product["fundamentals"]["payments"] == 2
    assert by_product["token"]["revenue_usdc"] == pytest.approx(0.50)


async def test_recent_receipts_newest_first() -> None:
    store = get_store()
    await _record(store, tx_hash="0xold")
    await _record(store, tx_hash="0xnew")
    rows = await store.recent_receipts()
    assert [r["tx_hash"] for r in rows] == ["0xnew", "0xold"]


# --- audit middleware end-to-end --------------------------------------------


def _audited_app(header_value: str | None) -> FastAPI:
    """Minimal app: a route that emits a settlement header, wrapped by the audit
    middleware — the same shape as the real seller, without a facilitator."""
    app = FastAPI()

    @app.get("/research/fundamentals")
    async def fundamentals(ticker: str, mode: str = "human") -> Response:
        r = Response(content='{"ok":true}', media_type="application/json")
        if header_value is not None:
            r.headers["PAYMENT-RESPONSE"] = header_value
        return r

    app.add_middleware(PaymentAuditMiddleware)
    return app


def test_middleware_records_a_receipt_on_settlement() -> None:
    client = TestClient(_audited_app(_settle_header()))
    resp = client.get("/research/fundamentals?ticker=aapl&mode=agent")
    assert resp.status_code == 200

    store = get_store()
    rows = store.receipts  # MemoryStore exposes the raw list
    assert len(rows) == 1
    r = rows[0]
    assert r["payer"].startswith("0xBuyer")
    assert r["tx_hash"].startswith("0xtxhash")
    assert r["amount_usdc"] == 0.25
    assert r["product"] == "fundamentals"
    assert r["identifier"] == "AAPL"  # uppercased
    assert r["mode"] == "agent"
    assert r["success"] is True


def test_middleware_no_receipt_when_unsettled() -> None:
    client = TestClient(_audited_app(None))  # no PAYMENT-RESPONSE header → free/unsettled
    resp = client.get("/research/fundamentals?ticker=AAPL")
    assert resp.status_code == 200
    assert get_store().receipts == []


def test_audit_layer_sees_header_added_by_inner_middleware() -> None:
    """The real shape: x402 adds PAYMENT-RESPONSE *after* the handler, from an
    inner middleware. Proves our audit layer (added last → outermost) wraps it
    and still sees that header — the ordering the seller app relies on."""
    from starlette.middleware.base import BaseHTTPMiddleware

    class FakeSettleMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            resp = await call_next(request)
            resp.headers["PAYMENT-RESPONSE"] = _settle_header(payer="0xInner")
            return resp

    app = FastAPI()

    @app.get("/research/token")
    async def token(token: str, mode: str = "human"):
        return {"ok": True}

    # Same order as build_app: payment layer first (inner), audit last (outer).
    app.add_middleware(FakeSettleMiddleware)
    app.add_middleware(PaymentAuditMiddleware)

    TestClient(app).get("/research/token?token=weth")
    rows = get_store().receipts
    assert len(rows) == 1
    assert rows[0]["payer"] == "0xInner"
    assert rows[0]["product"] == "token"
    assert rows[0]["identifier"] == "WETH"

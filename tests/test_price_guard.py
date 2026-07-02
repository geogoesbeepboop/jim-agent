"""The dynamic-price cap guard — refuse to overpay for x402 data.

The on-chain x402 price is set by the seller in the 402 header (it can name any
amount). The guard reads that advertised price in the unpaid pre-flight and
refuses — before any settlement — if it exceeds the cap. Critical for mainnet,
where the price is real USDC and not pre-published. Offline: the pre-flight HTTP
is mocked, so no wallet/network/settlement.
"""

from __future__ import annotations

import base64
import json

import pytest

from jim.buyer import client as buyer
from jim.buyer.client import PriceCapExceeded
from jim.research.budget import BudgetCap
from jim.sources.base import BudgetExceeded, ProcurementError, procure
from jim.store import MemoryStore
from jim.wallet import LocalWallet


def _payment_required_header(price_usd: float) -> str:
    amount = str(int(round(price_usd * 1_000_000)))  # USDC base units
    body = {"x402Version": 2, "accepts": [{"amount": amount, "network": "eip155:8453"}]}
    return base64.b64encode(json.dumps(body).encode()).decode()


class _FakeResp:
    def __init__(self, status: int, headers: dict):
        self.status_code = status
        self.headers = headers
        self.text = ""


class _FakeProbe:
    """Stands in for the unpaid pre-flight httpx client."""

    def __init__(self, resp: _FakeResp):
        self._resp = resp

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def request(self, method, url, **kw):
        return self._resp


def _patch_preflight(monkeypatch, price_usd: float):
    resp = _FakeResp(402, {"payment-required": _payment_required_header(price_usd)})
    monkeypatch.setattr(buyer.httpx, "AsyncClient", lambda *a, **k: _FakeProbe(resp))


async def test_pay_refuses_over_cap_before_settling(monkeypatch) -> None:
    _patch_preflight(monkeypatch, price_usd=0.50)  # seller advertises $0.50
    key = LocalWallet.create().private_key
    with pytest.raises(PriceCapExceeded) as ei:
        await buyer.pay("https://seller/data", method="POST", private_key=key, max_price_usd=0.10)
    assert ei.value.advertised_usd == pytest.approx(0.50)
    assert ei.value.cap_usd == pytest.approx(0.10)


async def test_procure_translates_price_cap_to_budget_exceeded() -> None:
    async def buy_fn(url, *, method="GET", json_body=None, headers=None, private_key=None,
                     timeout=180.0, max_price_usd=None):
        raise PriceCapExceeded(advertised_usd=0.5, cap_usd=max_price_usd or 0.1, url=url)

    # The engine catches BudgetExceeded, so an over-cap price must surface as one.
    with pytest.raises(BudgetExceeded):
        await procure(
            source_name="thegraph",
            cache_key="k",
            url="https://seller/data",
            method="POST",
            json_body={"query": "{}"},
            network="eip155:8453",
            price_estimate_usd=0.05,
            private_key="0xkey",
            budget=BudgetCap(0.10),
            store=MemoryStore(),
            ttl_seconds=60,
            buy_fn=buy_fn,
        )


async def test_procure_passes_remaining_budget_as_cap() -> None:
    seen = {}

    async def buy_fn(url, *, method="GET", json_body=None, headers=None, private_key=None,
                     timeout=180.0, max_price_usd=None):
        seen["cap"] = max_price_usd
        from jim.buyer.client import PaidResponse

        return PaidResponse(status_code=200, text="{}", settlement=None, cost_in_usd=0.0)

    try:
        await procure(
            source_name="thegraph", cache_key="k", url="u", method="POST",
            json_body={"query": "{}"}, network="eip155:8453", price_estimate_usd=0.02,
            private_key="0xkey", budget=BudgetCap(0.10), store=MemoryStore(),
            ttl_seconds=60, buy_fn=buy_fn,
        )
    except ProcurementError:
        pass  # empty payload parse is fine; we only care about the cap that was passed
    assert seen["cap"] == pytest.approx(0.10)  # full remaining ceiling

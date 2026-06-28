"""Phase 5 human UI — the storefront page + the checkout backend.

Offline: the "ok" path runs the real engine over the token product with only the
x402 buy + the LLM mocked (same seam as test_token), so the sourcing gate and the
checkout wrapping are exercised for real. No network, key, or wallet needed.
"""

from __future__ import annotations

import json

import pytest

from jim.buyer.client import PaidResponse
from jim.config import Settings
from jim.marketplace import ui
from jim.marketplace.ui import checkout, storefront_html
from jim.research import engine
from jim.research.cost import Usage
from jim.research.debate import DebateResult
from jim.research.facts import _fmt_value
from jim.research.judge import JudgeResult
from jim.research.products import Product
from jim.research.synthesize import SynthResult
from jim.sources.thegraph import GraphSource
from jim.store import reset_store
from jim.vendor import build_mock_response


@pytest.fixture(autouse=True)
def _reset():
    reset_store()
    yield
    reset_store()


async def _fake_buy(url, *, method="GET", json_body=None, headers=None, private_key=None, timeout=180.0):
    payload = build_mock_response((json_body or {}).get("query", ""))
    return PaidResponse(
        status_code=200, text=json.dumps(payload), settlement={"transaction": "0xtx"},
        cost_in_usd=0.01, tx_hash="0xtx",
    )


async def _fake_synth(snapshot, *, mode="human", feedback=None, debate=None) -> SynthResult:
    memo = " ".join(f"{f.label} was {_fmt_value(f.value, f.unit)} [{f.id}]." for f in snapshot.facts)
    return SynthResult(memo=memo, usage=Usage(model="test", input_tokens=10, output_tokens=20))


def _wire_offline_token(monkeypatch):
    monkeypatch.setattr(engine, "synthesize", _fake_synth)

    async def fake_judge(memo, snapshot):
        return JudgeResult.skip()

    async def fake_debate(snapshot):
        return DebateResult(bull="", bear="", verdict="", usages=[])

    monkeypatch.setattr(engine, "judge_faithfulness", fake_judge)
    monkeypatch.setattr(engine, "run_debate", fake_debate)
    monkeypatch.setattr(
        engine,
        "get_product",
        lambda name: Product("token", GraphSource(buy_fn=_fake_buy), 0.50, "token"),
    )


def test_storefront_html_is_renderable_and_lists_products() -> None:
    html = storefront_html()
    assert "Company Fundamentals" in html and "On-chain Token Snapshot" in html
    for link in ("/catalog", "/pricing", "/map", "/.well-known/x402"):
        assert link in html


async def test_checkout_unknown_product_is_an_error() -> None:
    out = await checkout(product="bogus", identifier="X", mode="human")
    assert out["ok"] is False


async def test_checkout_direct_returns_a_cited_preview(monkeypatch) -> None:
    _wire_offline_token(monkeypatch)
    out = await checkout(product="token", identifier="WETH", mode="agent")
    assert out["ok"] is True
    assert out["paid"] is False  # default: preview, no settlement
    assert out["settled_via"] == "direct"
    result = out["result"]
    assert result["status"] == "ok"
    assert result["sourcing"]["passed"] is True
    assert result["memo"]


async def test_checkout_falls_back_to_direct_without_a_buyer_key(monkeypatch) -> None:
    """settle=True is requested, but with no wallet the checkout must not attempt
    a real x402 self-pay — it degrades to a direct preview."""
    _wire_offline_token(monkeypatch)
    # No buyer key, yet UI settlement is on → can_settle must resolve False.
    monkeypatch.setattr(
        ui, "get_settings", lambda: Settings(evm_private_key=None, ui_settle_via_x402=True)
    )
    out = await checkout(product="token", identifier="WETH", mode="agent", settle=True)
    assert out["settled_via"] == "direct"
    assert out["ok"] is True

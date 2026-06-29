"""Token product end-to-end, offline: real GraphSource + parser + gate + margin.

Only the x402 buy and the LLM are mocked; the on-chain parsing, the sourcing
gate, and the margin accounting are the real code paths.
"""

from __future__ import annotations

import json

import pytest

from jim.buyer.client import PaidResponse
from jim.research import engine
from jim.research.cost import Usage
from jim.research.debate import DebateResult
from jim.research.facts import _fmt_value
from jim.research.judge import JudgeResult
from jim.research.products import Product
from jim.research.synthesize import SynthResult
from jim.sources.thegraph import GraphSource
from jim.store import get_store, reset_store
from jim.vendor import build_mock_response


@pytest.fixture(autouse=True)
def _reset():
    reset_store()
    yield
    reset_store()


async def _fake_buy(
    url, *, method="GET", json_body=None, headers=None, private_key=None, timeout=180.0
):
    payload = build_mock_response((json_body or {}).get("query", ""))
    return PaidResponse(
        status_code=200,
        text=json.dumps(payload),
        settlement={"transaction": "0xtx"},
        cost_in_usd=0.01,
        tx_hash="0xtx",
    )


async def _fake_synth(snapshot, *, mode="human", feedback=None, debate=None) -> SynthResult:
    # Build a correctly-cited memo straight from the facts (passes the gate).
    memo = " ".join(
        f"{f.label} was {_fmt_value(f.value, f.unit)} [{f.id}]." for f in snapshot.facts
    )
    return SynthResult(memo=memo, usage=Usage(model="test", input_tokens=10, output_tokens=20))


async def test_token_product_end_to_end(monkeypatch):
    monkeypatch.setattr(engine, "synthesize", _fake_synth)

    async def fake_judge(memo, snapshot, **_):
        return JudgeResult.skip()

    async def fake_debate(snapshot):
        return DebateResult(bull="", bear="", verdict="", usages=[])

    monkeypatch.setattr(engine, "judge_faithfulness", fake_judge)
    monkeypatch.setattr(engine, "run_debate", fake_debate)

    def fake_get_product(name):
        return Product(
            name="token",
            source=GraphSource(buy_fn=_fake_buy),
            price_out_usd=0.50,
            identifier_label="token",
        )

    monkeypatch.setattr(engine, "get_product", fake_get_product)

    result = await engine.run_research("WETH", product="token", mode="agent")

    assert result.status == "ok"
    assert result.product == "token"
    assert result.gate.passed and result.gate.coverage == 1.0
    # Economics: $0.50 in, $0.01 data, ~$0 inference → ~$0.49 margin.
    assert result.cost["data_cost_usd"] == 0.01
    assert result.cost["margin_usd"] == pytest.approx(0.49)
    # Every figure traces to The Graph.
    assert any("The Graph" in c for c in result.citations())

    summary = await get_store().margin_summary()
    assert summary["billable_queries"] == 1
    assert summary["total_margin_usd"] == pytest.approx(0.49)


async def test_token_second_call_hits_cache_and_improves_margin(monkeypatch):
    monkeypatch.setattr(engine, "synthesize", _fake_synth)

    async def fake_judge(memo, snapshot, **_):
        return JudgeResult.skip()

    async def fake_debate(snapshot):
        return DebateResult(bull="", bear="", verdict="", usages=[])

    monkeypatch.setattr(engine, "judge_faithfulness", fake_judge)
    monkeypatch.setattr(engine, "run_debate", fake_debate)

    # One shared GraphSource so its purchases land in one store across both calls.
    source = GraphSource(buy_fn=_fake_buy)

    def fake_get_product(name):
        return Product(name="token", source=source, price_out_usd=0.50, identifier_label="token")

    monkeypatch.setattr(engine, "get_product", fake_get_product)

    first = await engine.run_research("WETH", product="token")
    second = await engine.run_research("WETH", product="token")

    assert first.cost["cache_hit"] is False and first.cost["data_cost_usd"] == 0.01
    assert second.cost["cache_hit"] is True and second.cost["data_cost_usd"] == 0.0
    # The repackaged (cached) sale has higher margin than the first.
    assert second.cost["margin_usd"] > first.cost["margin_usd"]

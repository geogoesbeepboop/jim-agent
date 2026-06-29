"""Engine graph + economics, proven offline by mocking source + LLM.

The gate is real and deterministic, so we feed scripted memos and assert the
graph retries on a gate failure, gives up after max attempts, surfaces gather
errors, and records correct per-query margin.
"""

from __future__ import annotations

import pytest

from jim.research import engine
from jim.research.cost import Usage
from jim.research.debate import DebateResult
from jim.research.facts import USD, Fact, Snapshot
from jim.research.judge import JudgeResult
from jim.research.products import Product
from jim.research.synthesize import SynthResult
from jim.sources.base import GatherResult, ProcurementError
from jim.store import get_store, reset_store


def _snapshot() -> Snapshot:
    return Snapshot(
        ticker="ACME",
        cik="0000000001",
        entity_name="Acme Corp",
        facts=[
            Fact(
                id="C1",
                label="Revenue",
                value=100.0,
                unit=USD,
                source_label="SEC EDGAR",
                accession="x",
                form="10-K",
                fiscal_year=2024,
                fiscal_period="FY",
            )
        ],
        as_of="2025-01-01",
    )


class FakeSource:
    name = "fake"
    is_paid = False

    def __init__(self, cost_usd: float = 0.0, raise_exc: Exception | None = None):
        self.cost_usd = cost_usd
        self.raise_exc = raise_exc

    async def gather(self, identifier, *, budget, store) -> GatherResult:
        if self.raise_exc:
            raise self.raise_exc
        return GatherResult(snapshot=_snapshot(), cost_in_usd=self.cost_usd, cache_hit=False)


@pytest.fixture(autouse=True)
def _mock_io(monkeypatch):
    reset_store()  # fresh in-memory store per test

    async def fake_judge(memo, snapshot, **_) -> JudgeResult:
        return JudgeResult.skip()

    async def fake_debate(snapshot) -> DebateResult:
        return DebateResult(bull="", bear="", verdict="", usages=[])

    monkeypatch.setattr(engine, "judge_faithfulness", fake_judge)
    monkeypatch.setattr(engine, "run_debate", fake_debate)
    yield
    reset_store()


def _use_source(monkeypatch, source: FakeSource, price_out: float = 0.25):
    def fake_get_product(name):
        return Product(
            name="fundamentals", source=source, price_out_usd=price_out, identifier_label="x"
        )

    monkeypatch.setattr(engine, "get_product", fake_get_product)


def _script(memos: list[str]):
    calls = {"n": 0}

    async def fake_synth(snapshot, *, mode="human", feedback=None, debate=None) -> SynthResult:
        memo = memos[min(calls["n"], len(memos) - 1)]
        calls["n"] += 1
        return SynthResult(memo=memo, usage=Usage(model="test", input_tokens=10, output_tokens=20))

    return fake_synth, calls


async def test_retries_then_passes(monkeypatch):
    _use_source(monkeypatch, FakeSource())
    synth, calls = _script(["Revenue was $999 [C1].", "Revenue was $100 [C1]."])
    monkeypatch.setattr(engine, "synthesize", synth)

    result = await engine.run_research("ACME")

    assert result.status == "ok"
    assert result.attempts == 2
    assert calls["n"] == 2
    assert result.gate.passed


async def test_rejects_after_exhausting_attempts(monkeypatch):
    _use_source(monkeypatch, FakeSource())
    synth, _ = _script(["Revenue was $999 [C1]."])  # always wrong
    monkeypatch.setattr(engine, "synthesize", synth)

    result = await engine.run_research("ACME")

    assert result.status == "rejected"
    assert not result.gate.passed
    assert result.attempts == 2


async def test_gather_error_surfaces(monkeypatch):
    _use_source(monkeypatch, FakeSource(raise_exc=ProcurementError("upstream down")))
    synth, _ = _script(["unused"])
    monkeypatch.setattr(engine, "synthesize", synth)

    result = await engine.run_research("ACME")

    assert result.status == "error"
    assert "upstream down" in (result.error or "")
    assert result.memo is None


async def test_margin_is_recorded(monkeypatch):
    # Paid source costing $0.03; price_out $0.25; test model has no cost → margin 0.22.
    _use_source(monkeypatch, FakeSource(cost_usd=0.03), price_out=0.25)
    synth, _ = _script(["Revenue was $100 [C1]."])
    monkeypatch.setattr(engine, "synthesize", synth)

    result = await engine.run_research("ACME")

    assert result.status == "ok"
    assert result.cost["data_cost_usd"] == 0.03
    assert result.cost["price_out_usd"] == 0.25
    assert result.cost["margin_usd"] == pytest.approx(0.22)

    summary = await get_store().margin_summary()
    assert summary["billable_queries"] == 1
    assert summary["revenue_usd"] == pytest.approx(0.25)
    assert summary["data_cost_usd"] == pytest.approx(0.03)
    assert summary["total_margin_usd"] == pytest.approx(0.22)

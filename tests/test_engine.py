"""Engine rejection paths, proven offline by mocking source + LLM.

This file pins the two ways a synthesized memo dies: the deterministic gate
exhausts its retries, and the faithfulness judge fails a gate-clean memo —
each rejected and never billed. The happy paths (first-try ship, gate-feedback
retry, gather-error fail-closed, margin accounting) are pinned once, as eval
scenarios in ``src/jim/eval/scenarios.py``, executed on every commit by
``tests/test_eval_harness.py`` — one copy of each case, not two.
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
from jim.sources.base import GatherResult
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


async def test_rejects_after_exhausting_attempts(monkeypatch):
    _use_source(monkeypatch, FakeSource())
    synth, _ = _script(["Revenue was $999 [C1]."])  # always wrong
    monkeypatch.setattr(engine, "synthesize", synth)

    result = await engine.run_research("ACME")

    assert result.status == "rejected"
    assert not result.gate.passed
    assert result.attempts == 2


async def test_failing_judge_rejects_run_and_never_bills(monkeypatch):
    """status = ok only if gate.passed AND judge.passed. A memo the gate clears
    but the judge fails must be rejected on the judge's verdict alone, with no
    retry, and must book $0 — never-bill-rejected (ADR-0008) is not a
    gate-only privilege."""
    _use_source(monkeypatch, FakeSource())
    synth, calls = _script(["Revenue was $100 [C1]."])  # numerically clean
    monkeypatch.setattr(engine, "synthesize", synth)

    async def failing_judge(memo, snapshot, **_) -> JudgeResult:
        return JudgeResult(
            skipped=False,
            passed=False,
            score=0.2,
            issues=["unsupported: scripted unfaithful claim"],
        )

    monkeypatch.setattr(engine, "judge_faithfulness", failing_judge)

    result = await engine.run_research("ACME")

    assert result.status == "rejected"
    assert result.gate is not None and result.gate.passed
    assert result.judge is not None and not result.judge.passed
    assert result.attempts == 1 and calls["n"] == 1  # judge failure never retries
    assert result.cost["price_out_usd"] == 0.0
    assert result.cost["margin_usd"] <= 0.0

    summary = await get_store().margin_summary()
    assert summary["billable_queries"] == 0
    assert summary["revenue_usd"] == 0.0
    assert summary["total_queries"] == 1

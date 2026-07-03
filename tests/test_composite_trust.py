"""Composition + trust (Phase 7): the general contractor, gated and attributed.

Proves the AGENT_INTEROP claims offline:

  - CompositeSource merges peer facts into the primary snapshot with renumbered
    citations and per-fact origins; a failing peer degrades to a note, never a
    failure;
  - the sourcing gate verifies peer facts exactly like EDGAR facts (the
    composition firewall) — a misquoted peer figure fails the run;
  - gate outcomes attribute to the right source, and the trust ledger turns
    them into scores (reputation by verification).
"""

from __future__ import annotations

import pytest

from jim.interop.trust import attribute_gate_outcome, laplace_score
from jim.research.budget import BudgetCap
from jim.research.facts import INDEX, USD, Fact, Snapshot
from jim.research.gate import check_sourcing
from jim.sources.base import GatherResult, ProcurementError
from jim.store.repo import MemoryStore


class FakePrimary:
    name = "fundamentals"
    is_paid = False

    async def gather(self, identifier, *, budget, store):
        snap = Snapshot(
            ticker=identifier.upper(),
            cik="0000000001",
            entity_name="Test Corp",
            facts=[
                Fact(id="C1", label="Revenue", value=1_000_000_000.0, unit=USD),
                Fact(id="C2", label="Net income", value=100_000_000.0, unit=USD),
            ],
        )
        return GatherResult(snapshot=snap, cost_in_usd=0.0, cache_hit=False)


class FakePeer:
    is_paid = True

    def __init__(self, name: str, facts: list[Fact], cost: float = 0.01, fail: bool = False):
        self.name = f"peer:{name}"
        self._facts = facts
        self._cost = cost
        self._fail = fail

    async def gather(self, identifier, *, budget, store):
        if self._fail:
            raise ProcurementError(f"{self.name} is down")
        snap = Snapshot(
            ticker=identifier.upper(),
            cik=f"PEER:{self.name}",
            entity_name=f"{identifier} signals",
            facts=list(self._facts),
            origins={f.id: self.name for f in self._facts},
        )
        return GatherResult(snapshot=snap, cost_in_usd=self._cost, cache_hit=False)


def _sentiment_fact() -> Fact:
    return Fact(id="C1", label="News sentiment index", value=62.5, unit=INDEX)


async def _compose(*peers):
    from jim.sources.peer import CompositeSource

    source = CompositeSource(FakePrimary(), list(peers))
    return await source.gather("AAPL", budget=BudgetCap(ceiling_usd=0.10), store=MemoryStore())


async def test_peer_facts_merge_with_renumbered_ids_and_origins() -> None:
    result = await _compose(FakePeer("sent", [_sentiment_fact()]))
    snap = result.snapshot
    assert [f.id for f in snap.facts] == ["C1", "C2", "C3"]
    assert snap.facts[2].label == "News sentiment index"
    assert snap.origins["C1"] == "fundamentals"  # primary origins backfilled
    assert snap.origins["C3"] == "peer:sent"
    assert result.cost_in_usd == pytest.approx(0.01)
    assert any("peer:sent: +1 facts" in n for n in result.notes)


async def test_a_failing_peer_degrades_to_a_note() -> None:
    result = await _compose(
        FakePeer("down", [], fail=True), FakePeer("sent", [_sentiment_fact()])
    )
    snap = result.snapshot
    assert len(snap.facts) == 3  # primary 2 + surviving peer 1
    assert any("peer:down: skipped" in n for n in result.notes)


async def test_the_gate_verifies_peer_facts_like_any_other() -> None:
    result = await _compose(FakePeer("sent", [_sentiment_fact()]))
    snap = result.snapshot

    good = "Revenue was $1.00 billion [C1]. News sentiment is 62.5 [C3]."
    assert check_sourcing(good, snap).passed

    fabricated = "Revenue was $1.00 billion [C1]. News sentiment is 91.5 [C3]."
    gate = check_sourcing(fabricated, snap)
    assert not gate.passed  # the composition firewall: a misquoted peer figure fails


async def test_gate_outcomes_attribute_to_the_right_source() -> None:
    result = await _compose(FakePeer("sent", [_sentiment_fact()]))
    snap = result.snapshot

    passed = check_sourcing("Revenue was $1.00 billion [C1]. Sentiment is 62.5 [C3].", snap)
    verdicts = attribute_gate_outcome(snap, passed, default_source="fundamentals")
    assert verdicts == {"fundamentals": True, "peer:sent": True}

    failed = check_sourcing("Sentiment is 91.5 [C3].", snap)
    verdicts = attribute_gate_outcome(snap, failed, default_source="fundamentals")
    assert verdicts == {"peer:sent": False}  # only the implicated source is debited


def test_uncited_hallucinations_blame_no_source() -> None:
    snap = Snapshot(
        ticker="T", cik="1", entity_name="T",
        facts=[Fact(id="C1", label="Revenue", value=1e9, unit=USD)],
    )
    gate = check_sourcing("Revenue grew to 5 billion dollars.", snap)  # uncited fabrication
    assert not gate.passed
    assert attribute_gate_outcome(snap, gate, default_source="fundamentals") == {}


# --- the trust ledger ---------------------------------------------------------


def test_laplace_score_math() -> None:
    assert laplace_score(0, 0) == 0.5  # a new source starts neutral
    assert laplace_score(3, 0) == 0.8
    assert laplace_score(0, 3) == pytest.approx(0.2)
    assert laplace_score(98, 0) == pytest.approx(0.99)


async def test_memory_store_trust_roundtrip() -> None:
    store = MemoryStore()
    await store.record_trust_event(source="peer:sent", ok=True, context="fundamentals:AAPL")
    await store.record_trust_event(source="peer:sent", ok=False, context="fundamentals:MSFT")
    await store.record_trust_event(source="fundamentals", ok=True, context="fundamentals:AAPL")

    scores = await store.trust_scores()
    assert scores["peer:sent"]["ok"] == 1
    assert scores["peer:sent"]["fail"] == 1
    assert scores["peer:sent"]["score"] == 0.5
    assert scores["fundamentals"]["score"] == pytest.approx(2 / 3, abs=1e-4)
    assert scores["peer:sent"]["last_event_at"] is not None


async def test_engine_records_trust_events(monkeypatch) -> None:
    """A gated run attributes its outcome through the engine seam."""
    from jim.research import engine as eng
    from jim.research.gate import GateResult

    store = MemoryStore()
    snap = Snapshot(
        ticker="AAPL", cik="1", entity_name="Apple",
        facts=[Fact(id="C1", label="Revenue", value=1e9, unit=USD)],
        origins={"C1": "fundamentals"},
    )

    class FakeGraph:
        async def ainvoke(self, state):
            return {
                **state,
                "status": "ok",
                "memo": "Revenue was $1.00 billion [C1].",
                "snapshot": snap,
                "gate": GateResult(passed=True, n_figures=1, n_covered=1),
                "attempts": 1,
            }

    monkeypatch.setattr(eng, "get_store", lambda: store)
    monkeypatch.setattr(eng, "_GRAPH", FakeGraph())
    result = await eng.run_research("AAPL", product="fundamentals", mode="human")
    assert result.status == "ok"

    scores = await store.trust_scores()
    assert scores["fundamentals"]["ok"] == 1

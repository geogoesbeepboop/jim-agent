"""The billing invariant: a run jim's gates rejected is never billed.

The x402 payment middleware only settles 2xx responses (a verified payment on
an error response is cancelled). These tests pin the two halves of that
contract offline:

  - the seller refuses rejected runs with a non-2xx + structured diagnostics
    (so the middleware cancels instead of settling), and
  - the engine records $0 revenue for rejected runs, so the margin ledger
    shows the true loss instead of phantom income.

This is the fix for the first mainnet settlement, whose memo footer read
"paid … status rejected": the research failed the gate but the payment settled
anyway. Now the refusal happens before money moves.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from jim.research.engine import ResearchResult
from jim.research.gate import GateResult
from jim.research.schemas import FundamentalsResponse
from jim.seller.app import _deliver_or_refuse


def _rejected_result() -> ResearchResult:
    return ResearchResult(
        ticker="COIN",
        mode="human",
        status="rejected",
        product="fundamentals",
        memo="an unverified memo that must not ship",
        gate=GateResult(passed=False, violations=[], n_figures=10, n_covered=7),
        attempts=2,
    )


def test_rejected_run_is_refused_with_diagnostics_and_not_billed() -> None:
    with pytest.raises(HTTPException) as exc:
        _deliver_or_refuse(_rejected_result(), FundamentalsResponse)
    assert exc.value.status_code == 502  # >= 400 → payment middleware cancels
    detail = exc.value.detail
    assert detail["status"] == "rejected"
    assert detail["billed"] is False
    assert detail["sourcing"] == {
        "passed": False,
        "figures_checked": 10,
        "figures_covered": 7,
    }
    # The unverified memo must not leak through the refusal.
    assert "unverified memo" not in str(detail)


def test_error_run_stays_a_422_input_error() -> None:
    result = ResearchResult(
        ticker="NOPE", mode="human", status="error", error="Unknown ticker 'NOPE'"
    )
    with pytest.raises(HTTPException) as exc:
        _deliver_or_refuse(result, FundamentalsResponse)
    assert exc.value.status_code == 422


def test_ok_run_ships() -> None:
    from jim.research.facts import USD, Fact, Snapshot

    snap = Snapshot(
        ticker="AAPL",
        cik="0000320193",
        entity_name="Apple Inc.",
        facts=[Fact(id="C1", label="Revenue", value=1e9, unit=USD)],
    )
    result = ResearchResult(
        ticker="AAPL",
        mode="human",
        status="ok",
        product="fundamentals",
        memo="Revenue was $1.00 billion [C1].",
        snapshot=snap,
        gate=GateResult(passed=True, n_figures=1, n_covered=1),
    )
    resp = _deliver_or_refuse(result, FundamentalsResponse)
    assert resp.status == "ok"
    assert resp.memo == "Revenue was $1.00 billion [C1]."


async def test_ui_preview_refuses_rejected_runs(monkeypatch) -> None:
    """Even the free preview refuses to render unverified output."""
    from jim.marketplace import ui

    async def fake_run_research(identifier, *, product, mode):
        return _rejected_result()

    import jim.research.engine as engine

    monkeypatch.setattr(engine, "run_research", fake_run_research)
    out = await ui._checkout_direct("fundamentals", "COIN", "human", 0.25)
    assert out["ok"] is False
    assert out["rejected"] is True
    assert out["billed"] is False
    assert "memo" not in str(out.get("result", ""))


async def test_rejected_engine_run_records_zero_revenue(monkeypatch) -> None:
    """The margin ledger sees $0 price_out for a rejected run."""
    from jim.research import engine as eng

    recorded: dict = {}

    class FakeStore:
        async def record_query(self, **kw):
            recorded.update(kw)

        async def get_cached_memo(self, **kw):
            return None

        async def put_cached_memo(self, **kw):  # pragma: no cover - ok runs only
            pass

        async def upsert_insight(self, **kw):  # pragma: no cover - ok runs only
            pass

    class FakeGraph:
        async def ainvoke(self, state):
            return {
                **state,
                "status": "rejected",
                "memo": "unverified",
                "gate": GateResult(passed=False, n_figures=3, n_covered=1),
                "attempts": 2,
            }

    monkeypatch.setattr(eng, "get_store", lambda: FakeStore())
    monkeypatch.setattr(eng, "_GRAPH", FakeGraph())
    result = await eng.run_research("COIN", product="fundamentals", mode="human")
    assert result.status == "rejected"
    assert result.cost["price_out_usd"] == 0.0
    assert result.cost["margin_usd"] <= 0.0
    assert recorded["price_out_usd"] == 0.0

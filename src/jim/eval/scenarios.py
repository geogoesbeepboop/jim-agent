"""Scripted full-engine scenarios — the pipeline's behavior, proven offline.

The gate regression checks one function; these check the *system*: the real
LangGraph engine (retry loop, memo cache, margin ledger, refusal paths) runs
end-to-end with only its I/O seams scripted — a fake source instead of EDGAR, a
scripted synthesizer instead of Anthropic, judge/debate skipped, and a fresh
in-memory store per scenario. Same seams the engine tests use, so the eval can
run in any environment (a developer's live ``.env`` included) without touching
the real database, spending tokens, or hitting the network.

Each scenario pins one behavioral promise:

  - the gate's feedback loop actually repairs a hallucinating synthesis pass;
  - a persistently-hallucinating run is REJECTED and books $0 revenue — the
    never-bill-rejected invariant (ADR-0008), asserted at the ledger;
  - the memo cache short-circuits inference on identical data;
  - hostile identifiers are refused before any side effect;
  - upstream failures fail closed, never shipping an unverified memo;
  - economics (price − data − inference = margin) land in the store correctly.
"""

from __future__ import annotations

import inspect
import math
from dataclasses import dataclass, field
from typing import Callable

from jim.config import get_settings
from jim.research.cost import Usage
from jim.research.facts import USD, Fact, Snapshot
from jim.sources import BudgetExceeded
from jim.sources.base import ProcurementError
from jim.store.repo import MemoryStore

# A validator inspects (results, store, extras) and returns (passed, details).
# It may be async (e.g. to read the store's ledger).
Validator = Callable[[list, MemoryStore, dict], tuple[bool, dict]]


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


class _ScriptedSource:
    """Stand-in for EDGAR/The Graph: fixed snapshot, optional cost or failure."""

    name = "scripted"
    is_paid = False

    def __init__(self, cost_usd: float = 0.0, error: Exception | None = None):
        self.cost_usd = cost_usd
        self.error = error

    async def gather(self, identifier, *, budget, store):
        from jim.sources.base import GatherResult

        if self.error is not None:
            raise self.error
        return GatherResult(snapshot=_snapshot(), cost_in_usd=self.cost_usd, cache_hit=False)


@dataclass
class Scenario:
    name: str
    description: str
    validate: Validator
    memos: list[str] = field(default_factory=list)  # scripted synth outputs (last one repeats)
    identifier: str = "ACME"
    runs: int = 1
    use_memo_cache: bool = False
    source_cost_usd: float = 0.0
    source_error_factory: Callable[[], Exception] | None = None
    price_out_usd: float = 0.25


async def run_scenario(scenario: Scenario) -> tuple[bool, dict]:
    """Execute one scenario against the real engine with scripted seams.

    Returns ``(passed, details)``. Token/cost figures inside ``details`` are
    *scripted* (the fake source/synth invented them) — they validate the
    engine's accounting and are deliberately NOT counted as real eval spend.
    """
    from unittest import mock

    from jim.research import engine
    from jim.research.debate import DebateResult
    from jim.research.judge import JudgeResult
    from jim.research.products import Product
    from jim.research.synthesize import SynthResult

    store = MemoryStore()
    calls = {"synth_calls": 0}

    async def scripted_synth(snapshot, *, mode="human", feedback=None, debate=None):
        memo = scenario.memos[min(calls["synth_calls"], len(scenario.memos) - 1)]
        calls["synth_calls"] += 1
        return SynthResult(
            memo=memo, usage=Usage(model="scripted", input_tokens=250, output_tokens=180)
        )

    async def skip_judge(memo, snapshot, **_):
        return JudgeResult.skip()

    async def no_debate(snapshot):
        return DebateResult(bull="", bear="", verdict="", usages=[])

    source = _ScriptedSource(
        cost_usd=scenario.source_cost_usd,
        error=scenario.source_error_factory() if scenario.source_error_factory else None,
    )

    def scripted_product(name):
        return Product(
            name="fundamentals",
            source=source,
            price_out_usd=scenario.price_out_usd,
            identifier_label="ticker",
        )

    with (
        mock.patch.object(engine, "get_product", scripted_product),
        mock.patch.object(engine, "synthesize", scripted_synth),
        mock.patch.object(engine, "judge_faithfulness", skip_judge),
        mock.patch.object(engine, "run_debate", no_debate),
        mock.patch.object(engine, "get_store", lambda: store),
    ):
        results = []
        for _ in range(scenario.runs):
            results.append(
                await engine.run_research(
                    scenario.identifier,
                    enable_debate=False,
                    use_memo_cache=scenario.use_memo_cache,
                )
            )

    verdict = scenario.validate(results, store, dict(calls))
    if inspect.isawaitable(verdict):
        verdict = await verdict
    return verdict


# --- validators ---------------------------------------------------------------


def _v_clean_first_try(results, store, extras):
    r = results[0]
    ok = (
        r.status == "ok"
        and r.attempts == 1
        and bool(r.gate and r.gate.passed)
        and math.isclose(r.cost["margin_usd"], 0.25, abs_tol=1e-9)
    )
    return ok, {"status": r.status, "attempts": r.attempts, "cost": r.cost}


def _v_retry_recovers(results, store, extras):
    r = results[0]
    ok = r.status == "ok" and r.attempts == 2 and extras["synth_calls"] == 2
    return ok, {"status": r.status, "attempts": r.attempts, **extras}


async def _v_rejected_never_billed(results, store, extras):
    r = results[0]
    summary = await store.margin_summary()
    settings = get_settings()
    ok = (
        r.status == "rejected"
        and r.attempts == settings.research_max_attempts
        and r.cost["price_out_usd"] == 0.0
        and r.cost["margin_usd"] <= 0.0
        and summary["billable_queries"] == 0
        and summary["revenue_usd"] == 0.0
        and summary["total_queries"] == 1
    )
    return ok, {
        "status": r.status,
        "attempts": r.attempts,
        "cost": r.cost,
        "ledger": {k: summary[k] for k in ("total_queries", "billable_queries", "revenue_usd")},
    }


def _v_gather_error(results, store, extras):
    r = results[0]
    ok = r.status == "error" and "upstream down" in (r.error or "") and r.memo is None
    return ok, {"status": r.status, "error": r.error}


def _v_budget_exceeded(results, store, extras):
    r = results[0]
    ok = r.status == "error" and r.memo is None
    return ok, {"status": r.status, "error": r.error}


def _v_memo_cache(results, store, extras):
    first, second = results[0], results[1]
    ok = (
        first.status == "ok"
        and not first.served_from_cache
        and second.status == "ok"
        and second.served_from_cache
        and second.cost.get("input_tokens", 0) == 0
        and extras["synth_calls"] == 1
    )
    return ok, {
        "first_cached": first.served_from_cache,
        "second_cached": second.served_from_cache,
        "second_run_tokens": second.cost.get("input_tokens", 0),
        **extras,
    }


def _v_hostile_identifier(results, store, extras):
    r = results[0]
    ok = r.status == "error" and len(store.queries) == 0 and extras["synth_calls"] == 0
    return ok, {"status": r.status, "error": r.error, "recorded_queries": len(store.queries)}


def _v_margin_accounting(results, store, extras):
    r = results[0]
    ok = (
        r.status == "ok"
        and math.isclose(r.cost["data_cost_usd"], 0.03, abs_tol=1e-9)
        and math.isclose(r.cost["price_out_usd"], 0.25, abs_tol=1e-9)
        and math.isclose(r.cost["margin_usd"], 0.22, abs_tol=1e-6)
    )
    return ok, {"cost": r.cost}


def _v_advice_tone_dings_rubric(results, store, extras):
    from jim.eval.rubric import score_memo

    r = results[0]
    if r.status != "ok" or not r.memo or r.snapshot is None:
        return False, {"status": r.status, "error": r.error}
    rubric = score_memo(r.memo, r.snapshot, gate=r.gate, completeness=r.completeness)
    ok = rubric.dimensions.get("impersonal") == 0.0 and rubric.composite < 0.9
    return ok, {"rubric": rubric.to_dict(), "status": r.status}


SCENARIOS: list[Scenario] = [
    Scenario(
        name="clean_synthesis_ships_first_try",
        description="A fully-cited memo passes the gate on attempt 1 and books full margin.",
        memos=["Revenue was $100 [C1]."],
        validate=_v_clean_first_try,
    ),
    Scenario(
        name="gate_feedback_repairs_hallucination",
        description=(
            "Attempt 1 plants a wrong number; the gate's feedback drives a retry "
            "that ships clean — the self-correction loop works."
        ),
        memos=["Revenue was $999 [C1].", "Revenue was $100 [C1]."],
        validate=_v_retry_recovers,
    ),
    Scenario(
        name="persistent_hallucination_rejected_never_billed",
        description=(
            "Every attempt hallucinates; the run is REJECTED, revenue books $0, and "
            "the store ledger shows the loss — the never-bill-rejected invariant."
        ),
        memos=["Revenue was $999 [C1]."],
        validate=_v_rejected_never_billed,
    ),
    Scenario(
        name="gather_error_fails_closed",
        description="An upstream failure surfaces as status=error; nothing unverified ships.",
        memos=["unused"],
        source_error_factory=lambda: ProcurementError("upstream down"),
        validate=_v_gather_error,
    ),
    Scenario(
        name="budget_exceeded_fails_closed",
        description="The per-query budget cap denying a purchase kills the run, not the wallet.",
        memos=["unused"],
        source_error_factory=lambda: BudgetExceeded("cap reached"),
        validate=_v_budget_exceeded,
    ),
    Scenario(
        name="memo_cache_short_circuits_inference",
        description=(
            "Two identical queries: the second is served from the memo cache with "
            "zero inference tokens and only one synthesis call total."
        ),
        memos=["Revenue was $100 [C1]."],
        runs=2,
        use_memo_cache=True,
        validate=_v_memo_cache,
    ),
    Scenario(
        name="hostile_identifier_refused_before_side_effects",
        description=(
            "A path-traversal identifier is refused at the front door: no synthesis, "
            "no store record, no source fetch."
        ),
        memos=["unused"],
        identifier="../etc/passwd",
        validate=_v_hostile_identifier,
    ),
    Scenario(
        name="paid_data_margin_accounted",
        description="price_out − data − inference = margin lands in the result and the ledger.",
        memos=["Revenue was $100 [C1]."],
        source_cost_usd=0.03,
        validate=_v_margin_accounting,
    ),
    Scenario(
        name="advice_tone_dings_rubric",
        description=(
            "A numerically-clean memo with buy advice still ships offline (the judge "
            "needs a key) — but the rubric's impersonal dimension goes to 0, so the "
            "quality score records the leak."
        ),
        memos=["Revenue was $100 [C1]. Investors should buy before earnings."],
        validate=_v_advice_tone_dings_rubric,
    ),
]

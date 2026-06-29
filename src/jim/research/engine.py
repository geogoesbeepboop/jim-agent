"""The research engine — a LangGraph pipeline, now product- and margin-aware.

    gather → synthesize → gate ──pass──→ judge → finalize
                            │
                            └─fail & attempts left─→ synthesize (with feedback)
                            │
                            └─fail & exhausted─────→ finalize (status=rejected)

`gather` pulls from the product's source — free EDGAR or paid x402 (The Graph),
the latter through the budget cap + cache. Every run records its economics
(price_out − data_cost − inference_cost = margin) to the store for the dashboard.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from jim.config import get_settings
from jim.obs import research_trace
from jim.research.budget import BudgetCap
from jim.research.completeness import CompletenessResult, check_completeness
from jim.research.cost import CostLedger
from jim.research.debate import run_debate
from jim.research.facts import Snapshot
from jim.research.gate import GateResult, check_sourcing
from jim.research.judge import JudgeResult, judge_faithfulness
from jim.research.products import get_product
from jim.research.edgar import EdgarError
from jim.research.synthesize import synthesize
from jim.sources import BudgetExceeded, ProcurementError, Source
from jim.store import Store, get_store
from jim.store.embed import embed


class EngineState(TypedDict, total=False):
    identifier: str
    product: str
    mode: str
    source: Source
    budget: BudgetCap
    store: Store
    snapshot: Snapshot
    memo: str
    feedback: str | None
    enable_debate: bool
    debate: str | None
    debated: bool
    gate: GateResult
    judge: JudgeResult
    attempts: int
    max_attempts: int
    ledger: CostLedger
    cost_in_data: float
    cache_hit: bool
    status: str  # "ok" | "rejected" | "error"
    error: str | None
    # research-quality: memo cache + high-stakes judge
    memo_cache_enabled: bool
    memo_cache_ttl: int
    high_stakes: bool
    served_from_cache: bool


@dataclass
class ResearchResult:
    ticker: str
    mode: str
    status: str  # "ok" | "rejected" | "error"
    product: str = "fundamentals"
    entity_name: str | None = None
    cik: str | None = None
    as_of: str | None = None
    memo: str | None = None
    snapshot: Snapshot | None = None
    gate: GateResult | None = None
    judge: JudgeResult | None = None
    debate: str | None = None
    completeness: CompletenessResult | None = None
    served_from_cache: bool = False
    attempts: int = 0
    cost: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def citations(self) -> list[str]:
        return [f.citation() for f in self.snapshot.facts] if self.snapshot else []


# --- Nodes ------------------------------------------------------------------


async def _gather(state: EngineState) -> dict:
    try:
        result = await state["source"].gather(
            state["identifier"], budget=state["budget"], store=state["store"]
        )
    except (EdgarError, BudgetExceeded, ProcurementError) as e:
        return {"status": "error", "error": str(e)}
    return {
        "snapshot": result.snapshot,
        "cost_in_data": result.cost_in_usd,
        "cache_hit": result.cache_hit,
    }


def _memo_cache_key(state: EngineState) -> str:
    return f"{state['product']}:{state['identifier'].upper()}:{state['mode']}"


async def _memo_cache(state: EngineState) -> dict:
    """Serve a recent identical memo, skipping all inference, when the freshly
    gathered data is unchanged and the cached memo still passes the gate.

    The gate re-check is the safety net: the cached memo only ships if it's still
    fully sourced against *this* snapshot, so a never-valid memo can't leak through
    even on a fingerprint collision. Cheap (deterministic, no key)."""
    if state.get("status") == "error" or not state.get("memo_cache_enabled"):
        return {}
    snapshot = state["snapshot"]
    cached = await state["store"].get_cached_memo(
        key=_memo_cache_key(state),
        fingerprint=snapshot.fingerprint(),
        ttl_seconds=state["memo_cache_ttl"],
    )
    if cached is None:
        return {}
    gate = check_sourcing(cached["memo"], snapshot)
    if not gate.passed:
        return {}  # stale/invalid against current data → fall through to synthesis
    return {
        "memo": cached["memo"],
        "gate": gate,
        "debate": cached.get("debate"),
        "served_from_cache": True,
        "status": "ok",
    }


async def _debate(state: EngineState) -> dict:
    """Bull vs bear vs judge over the facts, before any memo is written."""
    result = await run_debate(state["snapshot"])
    for usage in result.usages:
        state["ledger"].add(usage)
    return {"debate": result.context(), "debated": True}


async def _synthesize(state: EngineState) -> dict:
    result = await synthesize(
        state["snapshot"],
        mode=state["mode"],
        feedback=state.get("feedback"),
        debate=state.get("debate"),
    )
    state["ledger"].add(result.usage)
    return {"memo": result.memo, "attempts": state.get("attempts", 0) + 1}


async def _gate(state: EngineState) -> dict:
    gate = check_sourcing(state["memo"], state["snapshot"])
    return {"gate": gate, "feedback": gate.feedback() or None}


async def _judge(state: EngineState) -> dict:
    judge = await judge_faithfulness(
        state["memo"], state["snapshot"], high_stakes=state.get("high_stakes", False)
    )
    if judge.usage:
        state["ledger"].add(judge.usage)
    status = "ok" if (state["gate"].passed and judge.passed) else "rejected"
    return {"judge": judge, "status": status}


def _finalize(state: EngineState) -> dict:
    return {"status": state.get("status", "rejected")}


def _route_after_gate(state: EngineState) -> str:
    if state["gate"].passed:
        return "judge"
    if state.get("attempts", 0) < state.get("max_attempts", 2):
        return "synthesize"
    return "finalize"


def _route_after_cache(state: EngineState) -> str:
    if state.get("status") == "error":
        return "finalize"
    if state.get("served_from_cache"):
        return "finalize"  # cached memo reused — no synthesis/judge needed
    # Debate once, before the first synthesis (skip on synthesize retries).
    if state.get("enable_debate") and not state.get("debated"):
        return "debate"
    return "synthesize"


def _build_graph():
    g = StateGraph(EngineState)
    g.add_node("gather", _gather)
    g.add_node("memo_cache", _memo_cache)
    g.add_node("debate", _debate)
    g.add_node("synthesize", _synthesize)
    g.add_node("gate", _gate)
    g.add_node("judge", _judge)
    g.add_node("finalize", _finalize)

    g.add_edge(START, "gather")
    g.add_edge("gather", "memo_cache")
    g.add_conditional_edges(
        "memo_cache",
        _route_after_cache,
        {"finalize": "finalize", "debate": "debate", "synthesize": "synthesize"},
    )
    g.add_edge("debate", "synthesize")
    g.add_edge("synthesize", "gate")
    g.add_conditional_edges(
        "gate",
        _route_after_gate,
        {"synthesize": "synthesize", "judge": "judge", "finalize": "finalize"},
    )
    g.add_edge("judge", END)
    g.add_edge("finalize", END)
    return g.compile()


_GRAPH = _build_graph()


async def run_research(
    identifier: str,
    *,
    product: str = "fundamentals",
    mode: str = "human",
    enable_debate: bool | None = None,
    high_stakes: bool = False,
    use_memo_cache: bool | None = None,
) -> ResearchResult:
    """Run the full pipeline for an identifier and return a structured result.

    Args:
        identifier: ticker (fundamentals) or token symbol/address (token).
        product: which research product to run.
        mode: "human" (narrative) or "agent" (terse, metric-dense).
        enable_debate: override the bull/bear/judge debate (eval A/B). None = config.
        high_stakes: upgrade the faithfulness judge to the stronger model.
        use_memo_cache: override the memo cache (eval A/B disables it). None = config.
    """
    settings = get_settings()
    debate_on = settings.enable_debate if enable_debate is None else enable_debate
    memo_cache_on = settings.memo_cache_enabled if use_memo_cache is None else use_memo_cache
    spec = get_product(product)
    ledger = CostLedger()
    budget = BudgetCap(ceiling_usd=settings.per_query_budget_usd)
    store = get_store()

    initial: EngineState = {
        "identifier": identifier,
        "product": product,
        "mode": mode,
        "source": spec.source,
        "budget": budget,
        "store": store,
        "attempts": 0,
        "max_attempts": settings.research_max_attempts,
        "ledger": ledger,
        "cost_in_data": 0.0,
        "cache_hit": False,
        "feedback": None,
        "enable_debate": debate_on,
        "debated": False,
        "debate": None,
        "memo_cache_enabled": memo_cache_on,
        "memo_cache_ttl": settings.memo_cache_ttl_seconds,
        "high_stakes": high_stakes,
        "served_from_cache": False,
    }

    with research_trace(identifier.upper(), mode) as trace:
        try:
            final: EngineState = await _GRAPH.ainvoke(initial)
        except Exception as e:
            final = {"status": "error", "error": str(e)}

        snapshot: Snapshot | None = final.get("snapshot")
        gate: GateResult | None = final.get("gate")
        judge: JudgeResult | None = final.get("judge")
        status = final.get("status", "rejected")
        cost_in_data = float(final.get("cost_in_data", 0.0))
        cache_hit = bool(final.get("cache_hit", False))
        served_from_cache = bool(final.get("served_from_cache", False))
        inference_cost = round(ledger.inference_cost_usd, 6)
        margin = round(spec.price_out_usd - cost_in_data - inference_cost, 6)

        # Completeness: what material facts did the memo leave out? (signal, not gate)
        memo = final.get("memo")
        completeness: CompletenessResult | None = None
        if memo and snapshot is not None:
            completeness = check_completeness(
                memo, snapshot, material_floor=settings.completeness_material_floor
            )

        cost = {
            "input_tokens": ledger.input_tokens,
            "output_tokens": ledger.output_tokens,
            "inference_cost_usd": inference_cost,
            "data_cost_usd": round(cost_in_data, 6),
            "price_out_usd": spec.price_out_usd,
            "margin_usd": margin,
            "cache_hit": cache_hit,
            "served_from_cache": served_from_cache,
        }
        if completeness is not None:
            cost["completeness"] = round(completeness.coverage, 4)
            cost["material_coverage"] = round(completeness.material_coverage, 4)

        if gate is not None:
            trace.score("sourcing_coverage", gate.coverage)
            trace.score("gate_passed", 1 if gate.passed else 0)
        if judge is not None and not judge.skipped:
            trace.score("faithfulness", judge.score)
        trace.cost(inference_cost, cost["input_tokens"], cost["output_tokens"])
        trace.score("data_cost_usd", cost_in_data)
        trace.score("margin_usd", margin)
        if completeness is not None:
            trace.score("completeness", completeness.coverage)
            trace.score("material_coverage", completeness.material_coverage)
        trace.update(
            output={"status": status, "memo": memo},
            metadata={
                "identifier": identifier,
                "product": product,
                "cache_hit": cache_hit,
                "served_from_cache": served_from_cache,
            },
        )

    # Persist economics + insight (billable runs only contribute to margin).
    if status in ("ok", "rejected"):
        await store.record_query(
            product=product,
            identifier=identifier.upper(),
            mode=mode,
            status=status,
            price_out_usd=spec.price_out_usd,
            cost_in_data_usd=cost_in_data,
            cost_inference_usd=inference_cost,
            cache_hit=cache_hit,
            attempts=final.get("attempts", 0),
        )
    # On a freshly synthesized ok run, warm the semantic insight + the memo cache
    # so the next identical query serves from cache (no re-synthesis). A run that
    # was *itself* served from cache is already stored — don't rewrite it.
    if status == "ok" and memo and not served_from_cache:
        await store.upsert_insight(
            key=f"{product}:{identifier.upper()}:{mode}",
            text=memo,
            embedding=embed(memo),
        )
        if memo_cache_on and snapshot is not None:
            await store.put_cached_memo(
                key=f"{product}:{identifier.upper()}:{mode}",
                fingerprint=snapshot.fingerprint(),
                memo=memo,
                debate=final.get("debate"),
            )

    return ResearchResult(
        ticker=identifier.upper(),
        mode=mode,
        status=status,
        product=product,
        entity_name=snapshot.entity_name if snapshot else None,
        cik=snapshot.cik if snapshot else None,
        as_of=snapshot.as_of if snapshot else None,
        memo=memo,
        snapshot=snapshot,
        gate=gate,
        judge=judge,
        debate=final.get("debate"),
        completeness=completeness,
        served_from_cache=served_from_cache,
        attempts=final.get("attempts", 0),
        cost=cost,
        error=final.get("error"),
    )

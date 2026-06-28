"""Eval runner: gate regression (offline) + debate-vs-single-pass lift (live).

The lift question Phase 3 must answer: does the bull/bear/judge debate improve
quality over the Phase-1 single pass? We run each held-out ticker both ways and
compare gate pass-rate, sourcing coverage, and judge faithfulness. Results log
to Langfuse when configured.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from jim.research.facts import Fact, Snapshot
from jim.research.gate import check_sourcing


# --- Gate regression (deterministic, no API key) ----------------------------


def run_gate_regression() -> dict:
    from jim.eval.dataset import GATE_REGRESSION

    results = []
    passed = 0
    for case in GATE_REGRESSION:
        snap = Snapshot(
            ticker="T",
            cik="0",
            entity_name="T",
            facts=[Fact(id=i, label=i, value=v, unit=u) for i, (v, u) in case.facts.items()],
        )
        verdict = check_sourcing(case.memo, snap)
        ok = verdict.passed == case.should_pass
        passed += ok
        results.append(
            {
                "name": case.name,
                "expected_pass": case.should_pass,
                "got_pass": verdict.passed,
                "correct": ok,
            }
        )
    return {"total": len(GATE_REGRESSION), "correct": passed, "cases": results}


# --- Live lift eval ---------------------------------------------------------


@dataclass
class RunMetrics:
    ticker: str
    variant: str  # "single_pass" | "debate"
    status: str
    gate_passed: bool = False
    coverage: float = 0.0
    faithfulness: float | None = None
    n_facts: int = 0
    attempts: int = 0
    inference_cost_usd: float = 0.0
    error: str | None = None


async def _run_one(ticker: str, variant: str) -> RunMetrics:
    from jim.research.engine import run_research

    result = await run_research(ticker, enable_debate=(variant == "debate"))
    return RunMetrics(
        ticker=ticker,
        variant=variant,
        status=result.status,
        gate_passed=bool(result.gate and result.gate.passed),
        coverage=round(result.gate.coverage, 4) if result.gate else 0.0,
        faithfulness=(result.judge.score if result.judge and not result.judge.skipped else None),
        n_facts=len(result.snapshot.facts) if result.snapshot else 0,
        attempts=result.attempts,
        inference_cost_usd=result.cost.get("inference_cost_usd", 0.0),
        error=result.error,
    )


def _aggregate(rows: list[RunMetrics]) -> dict:
    ok = [r for r in rows if r.status == "ok"]
    n = len(rows)
    faiths = [r.faithfulness for r in rows if r.faithfulness is not None]
    return {
        "runs": n,
        "gate_pass_rate": round(sum(r.gate_passed for r in rows) / n, 4) if n else 0.0,
        "ok_rate": round(len(ok) / n, 4) if n else 0.0,
        "mean_coverage": round(sum(r.coverage for r in rows) / n, 4) if n else 0.0,
        "mean_faithfulness": round(sum(faiths) / len(faiths), 4) if faiths else None,
        "mean_facts": round(sum(r.n_facts for r in rows) / n, 1) if n else 0.0,
        "mean_inference_cost_usd": round(sum(r.inference_cost_usd for r in rows) / n, 5)
        if n
        else 0.0,
    }


@dataclass
class EvalReport:
    gate_regression: dict
    single_pass: dict = field(default_factory=dict)
    debate: dict = field(default_factory=dict)
    rows: list[dict] = field(default_factory=list)
    lift: dict = field(default_factory=dict)


async def run_eval(tickers: list[str] | None = None, *, live: bool = True) -> EvalReport:
    """Run the gate regression and (if live) the debate-vs-single-pass comparison."""
    from jim.eval.dataset import HELD_OUT
    from jim.obs.tracing import _langfuse_client

    gate = run_gate_regression()
    report = EvalReport(gate_regression=gate)
    if not live:
        return report

    tickers = tickers or HELD_OUT
    rows: list[RunMetrics] = []
    for variant in ("single_pass", "debate"):
        for ticker in tickers:
            try:
                rows.append(await _run_one(ticker, variant))
            except Exception as e:  # keep the eval going past one bad ticker
                rows.append(
                    RunMetrics(ticker=ticker, variant=variant, status="error", error=str(e))
                )

    sp = _aggregate([r for r in rows if r.variant == "single_pass"])
    db = _aggregate([r for r in rows if r.variant == "debate"])
    report.single_pass = sp
    report.debate = db
    report.rows = [asdict(r) for r in rows]
    report.lift = {
        "gate_pass_rate": round(db["gate_pass_rate"] - sp["gate_pass_rate"], 4),
        "mean_faithfulness": (
            round((db["mean_faithfulness"] or 0) - (sp["mean_faithfulness"] or 0), 4)
            if db["mean_faithfulness"] is not None and sp["mean_faithfulness"] is not None
            else None
        ),
        "mean_facts": round(db["mean_facts"] - sp["mean_facts"], 1),
    }

    # Log aggregate scores to Langfuse if configured.
    client = _langfuse_client()
    if client is not None:
        try:
            with client.start_as_current_observation(
                name="eval.debate_vs_single_pass", as_type="span"
            ):
                for k, v in db.items():
                    if isinstance(v, (int, float)):
                        client.score_current_trace(name=f"debate.{k}", value=float(v))
                for k, v in sp.items():
                    if isinstance(v, (int, float)):
                        client.score_current_trace(name=f"single_pass.{k}", value=float(v))
            client.flush()
        except Exception:
            pass

    return report

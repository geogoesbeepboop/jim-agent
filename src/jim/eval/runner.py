"""Eval runner — executes suites, times/costs every case, persists the run.

Four suites, cheapest first:

  - ``gate``       deterministic sourcing-gate regression (offline, no key).
  - ``guards``     the other deterministic rails: impersonal tone, hostile
                   identifiers, completeness, monitor materiality, NL
                   propose/dispose (offline, no key).
  - ``scenarios``  the real engine end-to-end with scripted I/O seams — retry
                   loop, memo cache, refusal paths, margin ledger (offline).
  - ``live``       held-out tickers through the real pipeline, single-pass vs
                   debate, scored by the rubric (needs ANTHROPIC_API_KEY;
                   spends real tokens; records latency + cost per run).

``run_suites`` returns one self-contained run document (see
:mod:`jim.eval.storage` for persistence) whose ``summary`` block carries the
headline metrics the trend charts plot.
"""

from __future__ import annotations

import sys
import time
from datetime import datetime, timezone

from jim.eval.metrics import CaseResult, aggregate_cases, suite_block

OFFLINE_SUITES = ("gate", "guards", "scenarios")
ALL_SUITES = OFFLINE_SUITES + ("live",)


def _progress(message: str) -> None:
    """Unbuffered one-line status to stderr — stdout stays reserved for the report."""
    print(f"[jim-eval] {message}", file=sys.stderr, flush=True)


# --- gate suite ---------------------------------------------------------------


def run_gate_regression() -> dict:
    """Back-compat summary of the gate suite (used by tests and --gate-only)."""
    cases = run_suite_gate()
    return {
        "total": len(cases),
        "correct": sum(c.passed for c in cases),
        "cases": [
            {
                "name": c.name,
                "expected_pass": c.details["expected_pass"],
                "got_pass": c.details["got_pass"],
                "correct": c.passed,
            }
            for c in cases
        ],
    }


def run_suite_gate() -> list[CaseResult]:
    from jim.eval.dataset import GATE_REGRESSION
    from jim.research.facts import Fact, Snapshot
    from jim.research.gate import check_sourcing

    out: list[CaseResult] = []
    for case in GATE_REGRESSION:
        snap = Snapshot(
            ticker="T",
            cik="0",
            entity_name="T",
            facts=[Fact(id=i, label=i, value=v, unit=u) for i, (v, u) in case.facts.items()],
        )
        t0 = time.perf_counter()
        verdict = check_sourcing(case.memo, snap)
        latency = (time.perf_counter() - t0) * 1000
        out.append(
            CaseResult(
                suite="gate",
                name=case.name,
                passed=verdict.passed == case.should_pass,
                latency_ms=latency,
                details={
                    "memo": case.memo,
                    "expected_pass": case.should_pass,
                    "got_pass": verdict.passed,
                    "coverage": round(verdict.coverage, 4),
                    "violations": [
                        {"figure": v.figure, "reason": v.reason} for v in verdict.violations
                    ],
                },
            )
        )
    return out


# --- guards suite ---------------------------------------------------------------


def run_suite_guards() -> list[CaseResult]:
    from jim.eval.dataset_guards import GUARD_CASES

    out: list[CaseResult] = []
    for case in GUARD_CASES:
        t0 = time.perf_counter()
        try:
            passed, details = case.check()
            error = None
        except Exception as e:  # a crashing guard is a failing case, not a dead run
            passed, details, error = False, {}, f"{type(e).__name__}: {e}"
        out.append(
            CaseResult(
                suite="guards",
                name=f"{case.category}.{case.name}",
                passed=passed,
                latency_ms=(time.perf_counter() - t0) * 1000,
                details=details,
                error=error,
            )
        )
    return out


# --- scenarios suite ------------------------------------------------------------


async def run_suite_scenarios() -> list[CaseResult]:
    from jim.eval.scenarios import SCENARIOS, run_scenario

    out: list[CaseResult] = []
    for scenario in SCENARIOS:
        t0 = time.perf_counter()
        try:
            passed, details = await run_scenario(scenario)
            error = None
        except Exception as e:
            passed, details = False, {}
            error = f"{type(e).__name__}: {e}"
        details = {"description": scenario.description, **details}
        out.append(
            CaseResult(
                suite="scenarios",
                name=scenario.name,
                passed=passed,
                latency_ms=(time.perf_counter() - t0) * 1000,
                details=details,
                error=error,
            )
        )
    return out


# --- live suite -----------------------------------------------------------------

VARIANTS = ("single_pass", "debate")


async def _run_live_case(ticker: str, variant: str, repeat: int) -> CaseResult:
    from jim.eval.rubric import score_memo
    from jim.research.engine import run_research

    name = f"{ticker}:{variant}" + (f"#r{repeat}" if repeat else "")
    t0 = time.perf_counter()
    try:
        # Memo cache off so single_pass vs debate compare head-to-head, never
        # short-circuited by a memo a prior variant cached.
        result = await run_research(
            ticker, enable_debate=(variant == "debate"), use_memo_cache=False
        )
    except Exception as e:
        return CaseResult(
            suite="live",
            name=name,
            passed=False,
            latency_ms=(time.perf_counter() - t0) * 1000,
            details={"ticker": ticker, "variant": variant, "status": "error"},
            error=f"{type(e).__name__}: {e}",
        )
    latency = (time.perf_counter() - t0) * 1000

    faithfulness = result.judge.score if result.judge and not result.judge.skipped else None
    composite = None
    if result.memo and result.snapshot is not None:
        composite = score_memo(
            result.memo,
            result.snapshot,
            gate=result.gate,
            completeness=result.completeness,
            faithfulness=faithfulness,
        ).composite
    cost = result.cost or {}
    return CaseResult(
        suite="live",
        name=name,
        passed=result.status == "ok",
        score=composite,
        latency_ms=latency,
        cost_usd=cost.get("inference_cost_usd", 0.0) + cost.get("data_cost_usd", 0.0),
        input_tokens=cost.get("input_tokens", 0),
        output_tokens=cost.get("output_tokens", 0),
        details={
            "ticker": ticker,
            "variant": variant,
            "repeat": repeat,
            "status": result.status,
            "gate_passed": bool(result.gate and result.gate.passed),
            "coverage": round(result.gate.coverage, 4) if result.gate else None,
            "material_coverage": (
                round(result.completeness.material_coverage, 4) if result.completeness else None
            ),
            "faithfulness": faithfulness,
            "attempts": result.attempts,
            "n_facts": len(result.snapshot.facts) if result.snapshot else 0,
            "memo": result.memo,
            "violations": (
                [{"figure": v.figure, "reason": v.reason} for v in result.gate.violations]
                if result.gate
                else []
            ),
            "judge_issues": result.judge.issues if result.judge else [],
        },
        error=result.error,
    )


def _live_rollup(cases: list[CaseResult]) -> dict:
    agg = aggregate_cases(cases)
    n = len(cases)
    if n:
        gates = sum(1 for c in cases if c.details.get("gate_passed"))
        faiths = [
            c.details["faithfulness"] for c in cases if c.details.get("faithfulness") is not None
        ]
        agg["gate_pass_rate"] = round(gates / n, 4)
        agg["mean_faithfulness"] = round(sum(faiths) / len(faiths), 4) if faiths else None
        agg["mean_attempts"] = round(sum(c.details.get("attempts", 0) for c in cases) / n, 2)
    else:
        agg["gate_pass_rate"] = None
        agg["mean_faithfulness"] = None
        agg["mean_attempts"] = None
    return agg


def _live_lift(variants: dict[str, dict]) -> dict:
    """debate − single_pass on the headline metrics (None when either is missing)."""
    sp, db = variants.get("single_pass"), variants.get("debate")
    if not sp or not db:
        return {}
    lift = {}
    for key in ("pass_rate", "gate_pass_rate", "mean_score", "mean_faithfulness", "mean_cost_usd"):
        a, b = sp.get(key), db.get(key)
        lift[key] = (
            round(b - a, 4) if isinstance(a, (int, float)) and isinstance(b, (int, float)) else None
        )
    return lift


async def run_suite_live(
    tickers: list[str] | None = None,
    *,
    repeats: int = 1,
    variants: tuple[str, ...] = VARIANTS,
) -> tuple[list[CaseResult], dict]:
    """Run the live suite; returns (cases, extras) where extras carries the
    per-variant rollups + debate lift."""
    from jim.eval.dataset import HELD_OUT

    tickers = tickers or HELD_OUT
    total = len(variants) * len(tickers) * repeats
    cases: list[CaseResult] = []
    for variant in variants:
        for ticker in tickers:
            for repeat in range(repeats):
                name = f"{ticker}:{variant}" + (f"#r{repeat}" if repeat else "")
                _progress(f"live {len(cases) + 1}/{total} {name} ...")
                t0 = time.perf_counter()
                case = await _run_live_case(ticker, variant, repeat)
                elapsed = time.perf_counter() - t0
                status = "ok" if case.passed else f"FAIL ({case.error or 'see details'})"
                _progress(f"live {len(cases) + 1}/{total} {name} {status} in {elapsed:.1f}s")
                cases.append(case)

    per_variant = {
        v: _live_rollup([c for c in cases if c.details.get("variant") == v]) for v in variants
    }
    return cases, {"variants": per_variant, "lift": _live_lift(per_variant)}


# --- orchestration ----------------------------------------------------------------


def _config_snapshot() -> dict:
    from jim.config import get_settings
    from jim.llm import resolve_mode, subscription_available

    s = get_settings()
    return {
        "research_model": s.research_model,
        "judge_model": s.judge_model,
        "judge_high_stakes_model": s.judge_high_stakes_model,
        "debate_model": s.debate_model,
        "enable_judge": s.enable_judge,
        "judge_threshold": s.judge_threshold,
        "research_max_attempts": s.research_max_attempts,
        "enable_debate": s.enable_debate,
        "has_anthropic_key": bool(s.anthropic_api_key),
        # Auth mode is recorded so cost metrics stay comparable: under subscription
        # there is no per-token charge, so inference_cost_usd is notional.
        "llm_auth_mode": resolve_mode(),
        "has_subscription": subscription_available(),
    }


def _summarize(suites: dict[str, dict], extras: dict) -> dict:
    offline_cases = offline_passed = 0
    total_cost = 0.0
    for name in OFFLINE_SUITES:
        agg = suites.get(name, {}).get("aggregate")
        if agg:
            offline_cases += agg["cases"]
            offline_passed += agg["passed"]
            total_cost += agg["total_cost_usd"]
    summary: dict = {
        "offline_cases": offline_cases,
        "offline_passed": offline_passed,
        "offline_pass_rate": round(offline_passed / offline_cases, 4) if offline_cases else None,
        "all_offline_passed": offline_cases > 0 and offline_passed == offline_cases,
    }
    live = suites.get("live", {}).get("aggregate")
    if live:
        total_cost += live["total_cost_usd"]
        summary.update(
            {
                "live_cases": live["cases"],
                "live_ok_rate": live["pass_rate"],
                "live_gate_pass_rate": live.get("gate_pass_rate"),
                "live_mean_rubric": live.get("mean_score"),
                "live_mean_faithfulness": live.get("mean_faithfulness"),
                "live_mean_cost_usd": live.get("mean_cost_usd"),
                "live_latency_p50_ms": live.get("latency_p50_ms"),
                "live_latency_p95_ms": live.get("latency_p95_ms"),
                "live_lift": extras.get("live_lift") or {},
            }
        )
    summary["total_cost_usd"] = round(total_cost, 6)
    return summary


async def run_suites(
    names: list[str],
    *,
    tickers: list[str] | None = None,
    repeats: int = 1,
    label: str | None = None,
) -> dict:
    """Run the named suites and return the (unsaved) run document."""
    from jim.eval.storage import SCHEMA_VERSION, git_info, new_run_id

    started = datetime.now(timezone.utc)
    t0 = time.perf_counter()
    git = git_info()

    suites: dict[str, dict] = {}
    extras: dict = {}
    for name in names:
        suite_t0 = time.perf_counter()
        _progress(f"suite {name} starting...")
        if name == "gate":
            suites["gate"] = suite_block(run_suite_gate())
        elif name == "guards":
            suites["guards"] = suite_block(run_suite_guards())
        elif name == "scenarios":
            suites["scenarios"] = suite_block(await run_suite_scenarios())
        elif name == "live":
            cases, live_extras = await run_suite_live(tickers, repeats=repeats)
            block = suite_block(cases)
            block["aggregate"] = _live_rollup(cases)
            block["variants"] = live_extras["variants"]
            block["lift"] = live_extras["lift"]
            suites["live"] = block
            extras["live_lift"] = live_extras["lift"]
        else:
            raise ValueError(f"unknown suite: {name!r} (choose from {ALL_SUITES})")
        _progress(f"suite {name} done in {time.perf_counter() - suite_t0:.1f}s")

    finished = datetime.now(timezone.utc)
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": new_run_id(started, git.get("sha")),
        "label": label,
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "duration_seconds": round(time.perf_counter() - t0, 2),
        "git": git,
        "config": _config_snapshot(),
        "params": {"suites": list(names), "tickers": tickers, "repeats": repeats},
        "suites": suites,
        "summary": _summarize(suites, extras),
    }

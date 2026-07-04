"""Run-over-run comparison — where "is jim improving?" gets a yes/no.

Two regimes, because the two suite families mean different things:

  - **Offline suites** (gate, guards, scenarios) are deterministic. There is no
    tolerance: a case that passed in the base run and fails in the candidate is
    a regression, full stop. The diff names the exact cases.
  - **The live suite** is stochastic (a real model wrote the memos), so it gets
    thresholds instead of exactness: gate pass-rate may not drop more than X,
    the rubric composite more than Y, cost/latency may not blow past their
    allowances. Thresholds live in Settings so the team can tighten them as the
    agent matures.

The overall verdict is conservative: one regression anywhere → "regressed".
"""

from __future__ import annotations

from jim.config import get_settings

# Live metrics we compare, with direction and threshold semantics:
#   kind="rate":  absolute drop allowed (higher is better)
#   kind="cost":  relative increase allowed, in percent (lower is better)
_LIVE_CHECKS: list[dict] = [
    {
        "metric": "gate_pass_rate",
        "label": "live gate pass rate",
        "kind": "rate",
        "setting": "eval_gate_pass_rate_drop",
    },
    {
        "metric": "mean_rubric",
        "label": "live mean rubric",
        "kind": "rate",
        "setting": "eval_rubric_drop",
    },
    {
        "metric": "mean_cost_usd",
        "label": "live mean $/run",
        "kind": "cost",
        "setting": "eval_cost_increase_pct",
    },
    {
        "metric": "latency_p95_ms",
        "label": "live p95 latency",
        "kind": "cost",
        "setting": "eval_latency_increase_pct",
    },
]

_OFFLINE_SUITES = ("gate", "guards", "scenarios")


def _case_verdicts(suite: dict) -> dict[str, bool]:
    return {c["name"]: bool(c["passed"]) for c in suite.get("cases", [])}


def _offline_diff(base: dict, cand: dict) -> dict:
    """Exact per-case diff across the deterministic suites."""
    newly_failing: list[str] = []
    fixed: list[str] = []
    per_suite: dict[str, dict] = {}
    for name in _OFFLINE_SUITES:
        b = (base.get("suites") or {}).get(name)
        c = (cand.get("suites") or {}).get(name)
        if not b or not c:
            continue
        bv, cv = _case_verdicts(b), _case_verdicts(c)
        broke = sorted(k for k in bv.keys() & cv.keys() if bv[k] and not cv[k])
        repaired = sorted(k for k in bv.keys() & cv.keys() if not bv[k] and cv[k])
        failing_new_cases = sorted(k for k in cv.keys() - bv.keys() if not cv[k])
        newly_failing += [f"{name}:{k}" for k in broke + failing_new_cases]
        fixed += [f"{name}:{k}" for k in repaired]
        per_suite[name] = {
            "base_pass_rate": b["aggregate"].get("pass_rate"),
            "cand_pass_rate": c["aggregate"].get("pass_rate"),
            "newly_failing": broke + failing_new_cases,
            "fixed": repaired,
        }
    verdict = "regressed" if newly_failing else ("improved" if fixed else "flat")
    return {
        "suites": per_suite,
        "newly_failing": newly_failing,
        "fixed": fixed,
        "verdict": verdict,
    }


def _live_metrics(run: dict) -> dict | None:
    live = (run.get("suites") or {}).get("live")
    if not live:
        return None
    agg = live.get("aggregate", {})
    return {
        "gate_pass_rate": agg.get("gate_pass_rate"),
        "mean_rubric": agg.get("mean_score"),
        "mean_cost_usd": agg.get("mean_cost_usd"),
        "latency_p95_ms": agg.get("latency_p95_ms"),
    }


def _live_diff(base: dict, cand: dict) -> dict | None:
    b, c = _live_metrics(base), _live_metrics(cand)
    if b is None or c is None:
        return None
    settings = get_settings()
    checks = []
    regressions = []
    improvements = []
    for spec in _LIVE_CHECKS:
        bv, cv = b.get(spec["metric"]), c.get(spec["metric"])
        row = {
            "metric": spec["metric"],
            "label": spec["label"],
            "base": bv,
            "cand": cv,
            "delta": None,
            "status": "n/a",
        }
        if isinstance(bv, (int, float)) and isinstance(cv, (int, float)):
            delta = cv - bv
            row["delta"] = round(delta, 6)
            allowance = float(getattr(settings, spec["setting"]))
            if spec["kind"] == "rate":  # higher is better; allowance is absolute drop
                if delta < -allowance:
                    row["status"] = "regressed"
                elif delta > 0:
                    row["status"] = "improved"
                else:
                    row["status"] = "flat"
            else:  # cost-like: lower is better; allowance is a % increase
                pct = (delta / bv * 100.0) if bv else (100.0 if delta > 0 else 0.0)
                row["delta_pct"] = round(pct, 2)
                if pct > allowance:
                    row["status"] = "regressed"
                elif delta < 0:
                    row["status"] = "improved"
                else:
                    row["status"] = "flat"
        if row["status"] == "regressed":
            regressions.append(spec["label"])
        elif row["status"] == "improved":
            improvements.append(spec["label"])
        checks.append(row)
    verdict = "regressed" if regressions else ("improved" if improvements else "flat")
    return {
        "checks": checks,
        "regressions": regressions,
        "improvements": improvements,
        "verdict": verdict,
    }


def compare_runs(base: dict, cand: dict) -> dict:
    """Full comparison document: offline exact diff + thresholded live diff."""
    offline = _offline_diff(base, cand)
    live = _live_diff(base, cand)
    regressions = list(offline["newly_failing"])
    if live:
        regressions += live["regressions"]
    improved = bool(offline["fixed"]) or bool(live and live["improvements"])
    verdict = "regressed" if regressions else ("improved" if improved else "flat")
    return {
        "base_run": base.get("run_id"),
        "cand_run": cand.get("run_id"),
        "offline": offline,
        "live": live,
        "regressions": regressions,
        "verdict": verdict,
    }

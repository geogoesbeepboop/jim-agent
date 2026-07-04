"""The eval harness's shared metric model.

Every suite — the deterministic gate regression, the guard checks, the scripted
engine scenarios, the live lift eval — reduces each case to one uniform
:class:`CaseResult` row. Uniform rows are what make the harness comparable over
time: aggregation, persistence, run-over-run diffing, and the results UI all
operate on this one shape instead of per-suite formats.

A case carries the three families of signal the harness exists to track:

  - **pass/fail**   ``passed`` (the hard verdict) and optional ``score`` (a 0-1
                    quality number, e.g. the rubric composite) — "is jim right?"
  - **cost**        tokens + estimated USD — "what did being right cost?"
  - **latency**     wall-clock ms — "how long did it take?"
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field


@dataclass
class CaseResult:
    """One evaluated case, in the uniform shape every suite reduces to."""

    suite: str
    name: str
    passed: bool
    score: float | None = None  # optional 0-1 quality signal (rubric, coverage)
    latency_ms: float = 0.0
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    details: dict = field(default_factory=dict)  # suite-specific drill-down payload
    error: str | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["latency_ms"] = round(self.latency_ms, 2)
        d["cost_usd"] = round(self.cost_usd, 6)
        if self.score is not None:
            d["score"] = round(self.score, 4)
        return d


def percentile(values: list[float], pct: float) -> float:
    """Linear-interpolated percentile (pct in [0, 100]); 0.0 on empty input."""
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (pct / 100.0) * (len(ordered) - 1)
    lo = math.floor(rank)
    hi = math.ceil(rank)
    if lo == hi:
        return ordered[lo]
    frac = rank - lo
    return ordered[lo] * (1 - frac) + ordered[hi] * frac


def aggregate_cases(cases: list[CaseResult]) -> dict:
    """Roll a list of case rows up to the suite-level aggregate the UI plots."""
    n = len(cases)
    if n == 0:
        return {
            "cases": 0,
            "passed": 0,
            "pass_rate": None,
            "mean_score": None,
            "latency_p50_ms": 0.0,
            "latency_p95_ms": 0.0,
            "total_latency_ms": 0.0,
            "total_cost_usd": 0.0,
            "mean_cost_usd": 0.0,
            "input_tokens": 0,
            "output_tokens": 0,
            "errors": 0,
        }
    passed = sum(c.passed for c in cases)
    scores = [c.score for c in cases if c.score is not None]
    latencies = [c.latency_ms for c in cases]
    total_cost = sum(c.cost_usd for c in cases)
    return {
        "cases": n,
        "passed": passed,
        "pass_rate": round(passed / n, 4),
        "mean_score": round(sum(scores) / len(scores), 4) if scores else None,
        "latency_p50_ms": round(percentile(latencies, 50), 2),
        "latency_p95_ms": round(percentile(latencies, 95), 2),
        "total_latency_ms": round(sum(latencies), 2),
        "total_cost_usd": round(total_cost, 6),
        "mean_cost_usd": round(total_cost / n, 6),
        "input_tokens": sum(c.input_tokens for c in cases),
        "output_tokens": sum(c.output_tokens for c in cases),
        "errors": sum(1 for c in cases if c.error),
    }


def suite_block(cases: list[CaseResult]) -> dict:
    """The persisted per-suite block: aggregate + full case rows."""
    return {"aggregate": aggregate_cases(cases), "cases": [c.to_dict() for c in cases]}

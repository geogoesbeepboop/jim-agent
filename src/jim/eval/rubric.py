"""A structured quality rubric — what "better output" means, made measurable.

Gate pass-rate and a single faithfulness score answer "is it sourced and not
lying?" but not "is it *good*?" This rubric scores a memo on weighted dimensions
and reduces them to one composite 0–1 quality number, so the eval can rank
variants (single-pass vs debate, model A vs B) on quality, not just pass/fail.

Three of the four dimensions are deterministic and need **no API key**, so the
rubric produces a real quality signal offline:

  - **sourcing**     gate coverage — every figure resolves to a cited fact.
  - **completeness** share of *material* facts the memo actually surfaced.
  - **impersonal**   passes the deterministic impersonal-output guard (1/0).
  - **faithfulness** the LLM judge's groundedness (only when a key is set; the
                     dimension is dropped and the others re-normalised offline).

Weights are explicit and live in one place so "what we optimise for" is legible
and tunable, not buried in code.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from jim.monitors.impersonal import check_impersonal
from jim.research.completeness import CompletenessResult, check_completeness
from jim.research.facts import Snapshot
from jim.research.gate import GateResult, check_sourcing

# Relative importance of each dimension. Sourcing dominates (it's the core
# promise); faithfulness is heavy when available; completeness and tone refine.
WEIGHTS: dict[str, float] = {
    "sourcing": 0.40,
    "faithfulness": 0.30,
    "completeness": 0.20,
    "impersonal": 0.10,
}


@dataclass
class RubricScore:
    dimensions: dict[str, float]
    weights: dict[str, float]
    composite: float
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "composite": round(self.composite, 4),
            "dimensions": {k: round(v, 4) for k, v in self.dimensions.items()},
            "weights": self.weights,
            "notes": self.notes,
        }


def score_memo(
    memo: str,
    snapshot: Snapshot,
    *,
    gate: GateResult | None = None,
    completeness: CompletenessResult | None = None,
    faithfulness: float | None = None,
    material_floor: float = 0.6,
) -> RubricScore:
    """Score one memo. Precomputed ``gate``/``completeness`` are reused if given;
    ``faithfulness`` (the judge score) is included only when provided (live)."""
    gate = gate or check_sourcing(memo, snapshot)
    completeness = completeness or check_completeness(
        memo, snapshot, material_floor=material_floor
    )
    impersonal = check_impersonal(memo)

    dims: dict[str, float] = {
        "sourcing": float(gate.coverage),
        "completeness": float(completeness.material_coverage),
        "impersonal": 1.0 if impersonal.passed else 0.0,
    }
    if faithfulness is not None:
        dims["faithfulness"] = float(faithfulness)

    # Re-normalise weights over the dimensions actually present (faithfulness is
    # absent offline), so the composite is always a clean 0–1 weighted mean.
    active = {k: WEIGHTS[k] for k in dims if k in WEIGHTS}
    total_w = sum(active.values()) or 1.0
    composite = sum(dims[k] * w for k, w in active.items()) / total_w

    notes: list[str] = []
    if not gate.passed:
        notes.append(f"sourcing: {len(gate.violations)} violation(s)")
    if completeness.material_omissions:
        labels = ", ".join(o["label"] for o in completeness.material_omissions[:4])
        notes.append(f"omitted material: {labels}")
    if not impersonal.passed:
        notes.append(f"impersonal: {impersonal.violations[0]}")

    return RubricScore(
        dimensions=dims,
        weights={k: round(w / total_w, 4) for k, w in active.items()},
        composite=composite,
        notes=notes,
    )

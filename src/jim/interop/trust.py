"""Per-source trust: reputation by verification, not by reviews (Phase 7).

The sourcing gate already verifies every figure jim ships, wherever it came
from. That makes the gate a free reputation oracle: a source whose facts keep
verifying is trustworthy; one whose facts keep landing in violations is not.
The trust score is just the Laplace-smoothed gate pass-rate per source —
deterministic, computed from outcomes jim observed itself, impossible to
astroturf with reviews.

Attribution rule (deterministic, documented):
  - a run whose gate PASSED credits every contributing source with one ``ok``;
  - a run whose gate FAILED debits only the sources whose facts appear in a
    violation's citations (their data is what failed to verify); contributing
    sources not implicated get no signal — we don't punish a peer for the
    synthesizer's uncited hallucination.

The buy path uses the score as a routing signal: a peer below the trust floor
(``PEER_TRUST_FLOOR``, after ``PEER_TRUST_MIN_EVENTS`` observations) is refused
before any payment is proposed — jim stops paying sources it can't verify.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # typing only — keep this module import-light for the store
    from jim.research.facts import Snapshot
    from jim.research.gate import GateResult


def laplace_score(ok: int, fail: int) -> float:
    """Smoothed pass-rate: (ok+1)/(ok+fail+2). A new source starts at 0.5."""
    return (ok + 1) / (ok + fail + 2)


def attribute_gate_outcome(
    snapshot: "Snapshot", gate: "GateResult", *, default_source: str
) -> dict[str, bool]:
    """Map each implicated source to its verdict for one gated run.

    ``snapshot.origins`` (fact id → source name) attributes merged facts; facts
    without an origin belong to ``default_source`` (the product's own source).
    """
    origins = getattr(snapshot, "origins", None) or {}

    def origin_of(fact_id: str) -> str:
        return origins.get(fact_id, default_source)

    if gate.passed:
        return {origin_of(f.id): True for f in snapshot.facts}

    blamed: set[str] = set()
    for violation in gate.violations:
        for cid in violation.cited_ids:
            if snapshot.by_id(cid) is not None:
                blamed.add(origin_of(cid))
    return {src: False for src in blamed}

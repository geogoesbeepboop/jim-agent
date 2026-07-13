"""The A2A research artifact — the paid payload, staged then published (S4).

One place owns the shape of what a paid research task delivers, so the three
surfaces that touch it never drift:

- **staged** (encrypted) into ``WithheldArtifacts`` *before* settlement, so the
  memo content exists only ciphertext-at-rest until the money moves;
- **released** and turned into A2A ``Part``\\s (a ``DataPart`` carrying the same
  JSON the REST research endpoint returns + a ``TextPart`` carrying the memo)
  once settlement succeeds;
- **refused** with a diagnostics-only dict when the gate/judge rejected the run —
  verdict and COUNTS, never the memo or the violating figures (mirrors
  :func:`jim.seller.app._deliver_or_refuse`'s no-leak discipline).

Pure and unit-testable: no SDK event queue, no store — just a ``ResearchResult``
in, a dict / list-of-``Part`` out.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from google.protobuf import json_format

from a2a.types import Part

if TYPE_CHECKING:
    from jim.research.engine import ResearchResult


def research_artifact(result: "ResearchResult") -> dict:
    """The canonical paid payload staged in ``WithheldArtifacts`` and later
    published as A2A parts.

    ``response`` is the exact JSON the paid REST endpoint returns (reuse
    :meth:`ResearchResponse.from_result` so the A2A and HTTP products are
    byte-identical); ``memo`` is the human-readable body carried as its own
    ``TextPart``. Only ever staged encrypted, so this is the *only* copy of the
    memo content that exists before payment settles.
    """
    from jim.research.schemas import ResearchResponse

    response = ResearchResponse.from_result(result).model_dump(mode="json")
    return {"response": response, "memo": result.memo}


def artifact_parts(payload: dict) -> list[Part]:
    """Turn a staged payload into the A2A artifact parts for ``add_artifact``.

    One ``DataPart`` (the response JSON, machine-readable) + one ``TextPart``
    (the memo). The proto ``Part`` is a oneof discriminated by the present key:
    ``Part(data=...)`` serializes on the v0.3 wire as ``{"kind": "data", ...}``
    and ``Part(text=...)`` as ``{"kind": "text", ...}``.
    """
    data_part = Part()
    json_format.ParseDict(payload["response"], data_part.data)
    text_part = Part(text=payload.get("memo") or "")
    return [data_part, text_part]


def rejection_details(result: "ResearchResult") -> dict:
    """Diagnostics for a gate/judge-rejected run — verdict + COUNTS only.

    Mirrors :func:`jim.seller.app._deliver_or_refuse`'s refusal detail, but
    deliberately carries **counts** of violations / judge issues, never their
    text: a rejected run's memo and the specific figures it got wrong must never
    leak on an unpaid (refused, never-billed) response. ``billed`` is always
    ``False`` — a rejected task settles nothing (ADR-0008).
    """
    gate = result.gate
    judge = result.judge
    return {
        "status": result.status,
        "billed": False,
        "message": (
            "jim's verification gates rejected this run, so nothing shipped and your "
            "payment was not settled. This is a quality refusal, not an input error — "
            "a fresh attempt may succeed."
        ),
        "sourcing": (
            {
                "passed": gate.passed,
                "figures_checked": gate.n_figures,
                "figures_covered": gate.n_covered,
                "violations": len(gate.violations),
            }
            if gate is not None
            else None
        ),
        "faithfulness": (
            {
                "evaluated": not judge.skipped,
                "score": round(judge.score, 3),
                "issues": len(judge.issues),
            }
            if judge is not None
            else None
        ),
        "attempts": result.attempts,
    }

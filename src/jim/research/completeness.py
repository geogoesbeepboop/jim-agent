"""The completeness check — the gate's mirror image.

The sourcing gate validates what the memo *included* (every figure must resolve
to a cited fact). It says nothing about what the memo *left out*. A memo can pass
the gate while silently dropping the single most important line item.

This check runs the other direction: given the snapshot the synthesizer was
handed and the memo it produced, it reports which facts were never cited — and,
among those, which are **material** (core income-statement / balance-sheet line
items and headline ratios). Deterministic, no model: a fact is "covered" iff its
``[C#]`` id appears in the memo.

It is a *signal, not a gate*. Terse agent-mode output legitimately omits detail,
and we never reject a run for an omission — but a material omission lowers the
quality score (see :mod:`jim.eval.rubric`) and is surfaced to the caller so the
gap is visible rather than hidden.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from jim.research.facts import Snapshot

_CITE_RE = re.compile(r"\[(C\d+(?:\s*,\s*C\d+)*)\]")

# Labels that a fundamentals/token memo should not silently drop. Matched against
# Fact.label (the canonical labels set in edgar.py / thegraph.py / facts.py).
MATERIAL_LABELS: frozenset[str] = frozenset(
    {
        # income statement
        "Revenue",
        "Gross profit",
        "Operating income",
        "Net income",
        "Diluted EPS",
        "Basic EPS",
        # balance sheet
        "Total assets",
        "Total liabilities",
        "Stockholders' equity",
        # headline ratios
        "Net margin",
        "Operating margin",
        "Return on equity",
        # token product headline fields (labels per jim.sources.thegraph)
        "Price (USD)",
        "Market cap (USD)",
        "Liquidity / TVL (USD)",
        "Cumulative volume (USD)",
    }
)


def cited_ids(memo: str) -> set[str]:
    """Every distinct ``C#`` id referenced anywhere in the memo."""
    out: set[str] = set()
    for group in _CITE_RE.findall(memo or ""):
        for cid in group.split(","):
            out.add(cid.strip())
    return out


@dataclass
class CompletenessResult:
    coverage: float  # share of all snapshot facts the memo cites
    material_coverage: float  # share of *material* facts the memo cites
    cited: list[str] = field(default_factory=list)  # fact ids referenced
    omitted: list[dict] = field(default_factory=list)  # facts never cited
    material_omissions: list[dict] = field(default_factory=list)  # the ones that matter
    passed: bool = True  # material_coverage ≥ floor (a signal, never blocks)

    def to_dict(self) -> dict:
        return {
            "coverage": round(self.coverage, 4),
            "material_coverage": round(self.material_coverage, 4),
            "cited": self.cited,
            "omitted": self.omitted,
            "material_omissions": self.material_omissions,
            "passed": self.passed,
        }


def _fact_view(f) -> dict:
    return {"id": f.id, "label": f.label, "value": f.value, "unit": f.unit}


def check_completeness(
    memo: str, snapshot: Snapshot, *, material_floor: float = 0.6
) -> CompletenessResult:
    """Report which snapshot facts the memo omitted, flagging material ones."""
    if not snapshot.facts:
        return CompletenessResult(coverage=1.0, material_coverage=1.0, passed=True)

    cited = cited_ids(memo)
    cited_in_snap = [fid for fid in (f.id for f in snapshot.facts) if fid in cited]

    omitted = [_fact_view(f) for f in snapshot.facts if f.id not in cited]
    material_facts = [f for f in snapshot.facts if f.label in MATERIAL_LABELS]
    material_omissions = [
        _fact_view(f) for f in material_facts if f.id not in cited
    ]

    coverage = len(cited_in_snap) / len(snapshot.facts)
    material_coverage = (
        1.0
        if not material_facts
        else (len(material_facts) - len(material_omissions)) / len(material_facts)
    )
    return CompletenessResult(
        coverage=coverage,
        material_coverage=material_coverage,
        cited=sorted(cited_in_snap, key=lambda c: int(c[1:])),
        omitted=omitted,
        material_omissions=material_omissions,
        passed=material_coverage >= material_floor,
    )

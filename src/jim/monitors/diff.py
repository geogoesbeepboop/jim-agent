"""Deterministic snapshot diffing — the substrate the trigger crew reads.

Given a monitor's stored *baseline* (a compact dict of the facts it last saw) and
a freshly-gathered :class:`~jim.research.facts.Snapshot`, compute a
:class:`SnapshotDiff`: per-metric deltas (matched by label), newly-appeared and
removed metrics, new filing accessions, and whether the reporting date advanced.

No model, no thresholds here — this is pure arithmetic. The *triggers* apply
thresholds on top of this; the *materiality gate* then decides what to publish.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from jim.research.facts import Snapshot


@dataclass(frozen=True)
class FactDelta:
    """How one metric (by label) changed between baseline and the fresh snapshot."""

    label: str
    unit: str
    fact_id: str  # citation id in the CURRENT snapshot
    old_value: float | None
    new_value: float
    is_new: bool = False  # appeared this run (no baseline value)

    @property
    def abs_change(self) -> float | None:
        if self.old_value is None:
            return None
        return self.new_value - self.old_value

    @property
    def pct_change(self) -> float | None:
        if self.old_value is None or self.old_value == 0:
            return None
        return (self.new_value - self.old_value) / abs(self.old_value) * 100.0


@dataclass
class SnapshotDiff:
    """The full deterministic delta between a baseline and a fresh snapshot."""

    identifier: str
    deltas: dict[str, FactDelta] = field(default_factory=dict)  # label -> delta
    removed: list[str] = field(default_factory=list)  # labels gone from the snapshot
    new_accessions: list[str] = field(default_factory=list)  # filings not seen before
    prev_as_of: str | None = None
    new_as_of: str | None = None
    is_first_run: bool = False  # no baseline existed → nothing to compare

    @property
    def as_of_advanced(self) -> bool:
        return bool(self.new_as_of and self.prev_as_of and self.new_as_of > self.prev_as_of)

    def get(self, label: str) -> FactDelta | None:
        return self.deltas.get(label)

    @property
    def changed_labels(self) -> list[str]:
        """Labels whose value actually moved (excludes brand-new metrics)."""
        return [
            lbl for lbl, d in self.deltas.items() if not d.is_new and d.abs_change not in (None, 0)
        ]


def snapshot_to_baseline(snapshot: Snapshot) -> dict:
    """Freeze a snapshot into the compact dict a monitor stores between runs.

    One entry per fact label (the latest wins on duplicate labels, which the
    engine doesn't produce). We keep only what the diff + triggers need.
    """
    baseline: dict = {"as_of": snapshot.as_of, "facts": {}}
    accessions: list[str] = []
    for f in snapshot.facts:
        baseline["facts"][f.label] = {
            "value": f.value,
            "unit": f.unit,
            "id": f.id,
            "accession": f.accession,
        }
        if f.accession and f.accession not in accessions:
            accessions.append(f.accession)
    baseline["accessions"] = accessions
    return baseline


def diff_snapshots(baseline: dict | None, current: Snapshot) -> SnapshotDiff:
    """Compare a stored baseline against a fresh snapshot. Deterministic."""
    diff = SnapshotDiff(identifier=current.ticker, new_as_of=current.as_of)

    if not baseline or not baseline.get("facts"):
        diff.is_first_run = True
        return diff

    prev_facts: dict = baseline.get("facts", {})
    prev_accessions = set(baseline.get("accessions") or [])
    diff.prev_as_of = baseline.get("as_of")

    seen_labels: set[str] = set()
    for f in current.facts:
        seen_labels.add(f.label)
        prev = prev_facts.get(f.label)
        diff.deltas[f.label] = FactDelta(
            label=f.label,
            unit=f.unit,
            fact_id=f.id,
            old_value=(prev["value"] if prev else None),
            new_value=f.value,
            is_new=prev is None,
        )
        if f.accession and f.accession not in prev_accessions:
            if f.accession not in diff.new_accessions:
                diff.new_accessions.append(f.accession)

    diff.removed = [lbl for lbl in prev_facts if lbl not in seen_labels]
    return diff

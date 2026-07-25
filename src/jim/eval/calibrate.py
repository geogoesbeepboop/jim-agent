"""Judge-calibration math — deterministic, offline, fully unit-tested.

``jim-eval judge-calibrate`` runs the pinned judge model over the labeled
dataset (``jim.eval.dataset_judge``) and hands the per-case scores to this
module. Everything here is pure arithmetic on those scores, so the whole
report — confusion matrix, per-family recall, verdict flip-rate, threshold
sweep, chosen threshold, floor verdict — is testable without a model, and the
same numbers appear in the CLI, the persisted run document, and the dashboard.

Conventions:
  - The positive class is **unfaithful** (the thing the judge exists to catch).
  - A case's headline score is the **median** across repeats (robust to one
    flaky sample); the flip-rate reports how often repeats straddle the
    threshold anyway.
  - ``passed`` at the case level means "the judge's verdict agrees with the
    human label at the configured threshold" — so the suite's pass-rate is the
    plain agreement rate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from statistics import median


@dataclass
class JudgeSample:
    """One labeled case's scores across repeats."""

    name: str
    family: str
    label_faithful: bool
    scores: list[float] = field(default_factory=list)

    @property
    def median_score(self) -> float:
        return float(median(self.scores))


# Sweep grid per the E2 contract (docs/EVAL_LADDER.md): 0.5–0.95.
SWEEP_THRESHOLDS: tuple[float, ...] = tuple(round(0.5 + 0.05 * i, 2) for i in range(10))


def confusion_at(samples: list[JudgeSample], threshold: float) -> dict:
    """Confusion matrix + rates at one threshold (median score vs threshold)."""
    tp = fp = tn = fn = 0
    for s in samples:
        flagged = s.median_score < threshold  # judge fails the memo
        if s.label_faithful:
            fp, tn = fp + int(flagged), tn + int(not flagged)
        else:
            tp, fn = tp + int(flagged), fn + int(not flagged)
    n_unfaithful, n_faithful = tp + fn, fp + tn
    sensitivity = tp / n_unfaithful if n_unfaithful else None
    specificity = tn / n_faithful if n_faithful else None
    balanced = (
        round((sensitivity + specificity) / 2, 4)
        if sensitivity is not None and specificity is not None
        else None
    )
    return {
        "threshold": threshold,
        "tp": tp,
        "fp": fp,
        "tn": tn,
        "fn": fn,
        "sensitivity": round(sensitivity, 4) if sensitivity is not None else None,
        "specificity": round(specificity, 4) if specificity is not None else None,
        "balanced_accuracy": balanced,
        # False-reject = a faithful memo the judge would kill. This is the rate
        # that turns into refused revenue, so it gets its own name and floor.
        "false_reject_rate": round(fp / n_faithful, 4) if n_faithful else None,
        "accuracy": round((tp + tn) / len(samples), 4) if samples else None,
    }


def per_family_breakdown(samples: list[JudgeSample], threshold: float) -> dict:
    """Recall per unfaithful family; pass-rate for the faithful family."""
    out: dict[str, dict] = {}
    for s in samples:
        fam = out.setdefault(s.family, {"cases": 0, "correct": 0})
        fam["cases"] += 1
        flagged = s.median_score < threshold
        correct = (not flagged) if s.label_faithful else flagged
        fam["correct"] += int(correct)
    for name, fam in out.items():
        rate = round(fam["correct"] / fam["cases"], 4)
        fam["pass_rate" if name == "faithful" else "recall"] = rate
    return out


def flip_rate(samples: list[JudgeSample], threshold: float) -> float | None:
    """Share of multi-repeat cases whose verdict flips across repeats at ``threshold``.

    Verdict stability is its own quality axis: a judge whose repeats straddle
    the threshold is unreliable even when its median lands on the right side.
    """
    multi = [s for s in samples if len(s.scores) > 1]
    if not multi:
        return None
    flips = sum(
        1
        for s in multi
        if any(x < threshold for x in s.scores) and any(x >= threshold for x in s.scores)
    )
    return round(flips / len(multi), 4)


def threshold_sweep(
    samples: list[JudgeSample], thresholds: tuple[float, ...] = SWEEP_THRESHOLDS
) -> list[dict]:
    out = []
    for t in thresholds:
        row = confusion_at(samples, t)
        row["flip_rate"] = flip_rate(samples, t)
        out.append(row)
    return out


def choose_threshold(
    sweep: list[dict],
    *,
    min_balanced_accuracy: float,
    max_false_reject: float,
) -> dict | None:
    """Pick the operating threshold from a sweep, floor-first.

    Among points within the false-reject cap, take the best balanced accuracy
    (ties → the lower threshold: fewer false rejects at equal accuracy). If no
    point respects the cap, fall back to the best balanced accuracy outright —
    still reported, but ``floor_met`` will be False, and per the E2 contract a
    judge that can't meet the floor at ANY threshold must not co-decide
    ok/rejected.
    """
    scored = [row for row in sweep if row.get("balanced_accuracy") is not None]
    if not scored:
        return None
    capped = [
        row
        for row in scored
        if row.get("false_reject_rate") is not None
        and row["false_reject_rate"] <= max_false_reject
    ]
    pool = capped or scored
    best = sorted(pool, key=lambda r: (-r["balanced_accuracy"], r["threshold"]))[0]
    floor_met = (
        best["balanced_accuracy"] >= min_balanced_accuracy
        and best.get("false_reject_rate") is not None
        and best["false_reject_rate"] <= max_false_reject
    )
    return {
        "threshold": best["threshold"],
        "balanced_accuracy": best["balanced_accuracy"],
        "false_reject_rate": best["false_reject_rate"],
        "sensitivity": best["sensitivity"],
        "specificity": best["specificity"],
        "flip_rate": best.get("flip_rate"),
        "floor_met": floor_met,
    }


def calibration_report(
    samples: list[JudgeSample],
    *,
    configured_threshold: float,
    min_balanced_accuracy: float,
    max_false_reject: float,
) -> dict:
    """The full calibration block persisted inside the run document."""
    graded = [s for s in samples if s.scores]
    sweep = threshold_sweep(graded)
    at_configured = confusion_at(graded, configured_threshold)
    at_configured["flip_rate"] = flip_rate(graded, configured_threshold)
    return {
        "cases": len(graded),
        "faithful": sum(1 for s in graded if s.label_faithful),
        "unfaithful": sum(1 for s in graded if not s.label_faithful),
        "repeats": max((len(s.scores) for s in graded), default=0),
        "floor": {
            "min_balanced_accuracy": min_balanced_accuracy,
            "max_false_reject_rate": max_false_reject,
        },
        "at_configured": at_configured,
        "per_family": per_family_breakdown(graded, configured_threshold),
        "sweep": sweep,
        "chosen": choose_threshold(
            sweep,
            min_balanced_accuracy=min_balanced_accuracy,
            max_false_reject=max_false_reject,
        ),
    }

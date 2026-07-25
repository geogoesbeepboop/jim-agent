"""The judge-calibration harness (docs/EVAL_LADDER.md Phase E2), offline.

Three layers, none needing a model:

  - the labeled dataset is structurally sound AND lives strictly in the
    deterministic blind spot — every memo passes the sourcing gate and the
    impersonal guard, so whatever the judge scores here is signal only the
    judge can provide;
  - the calibration math (confusion matrix, per-family recall, flip rate,
    threshold sweep, chosen threshold, floor) is exercised on synthetic scores;
  - the runner + CLI produce a well-formed ``suite="judge"`` run document with
    a scripted judge, and refuse to run without a credential.

The one thing deliberately NOT tested here is the real judge model's accuracy —
that is the credentialed `jim-eval judge-calibrate` exit run, by design.
"""

from __future__ import annotations

import json

from jim.eval.calibrate import (
    JudgeSample,
    calibration_report,
    choose_threshold,
    confusion_at,
    flip_rate,
    per_family_breakdown,
    threshold_sweep,
)
from jim.eval.dataset_judge import (
    JUDGE_CASES,
    UNFAITHFUL_FAMILIES,
    judge_snapshot,
)
from jim.research.judge import JudgeResult

# --- dataset integrity ----------------------------------------------------------


def test_dataset_composition():
    names = [c.name for c in JUDGE_CASES]
    assert len(names) == len(set(names)), "case names must be unique"
    faithful = [c for c in JUDGE_CASES if c.label_faithful]
    unfaithful = [c for c in JUDGE_CASES if not c.label_faithful]
    assert len(JUDGE_CASES) >= 40
    assert len(faithful) >= 15, "need enough faithful cases to measure false rejects"
    assert len(unfaithful) >= 25
    # Every unfaithful family the judge exists for is represented, ≥4 deep.
    by_family: dict[str, int] = {}
    for c in unfaithful:
        by_family[c.family] = by_family.get(c.family, 0) + 1
    assert set(by_family) == set(UNFAITHFUL_FAMILIES)
    assert all(n >= 4 for n in by_family.values()), by_family
    # Labels are consistent with families, and every case carries its rationale
    # (the human label IS the calibration standard — it must be explainable).
    for c in JUDGE_CASES:
        assert c.rationale.strip(), f"{c.name} has no rationale"
        assert c.label_faithful == (c.family == "faithful")


def test_every_case_is_in_the_deterministic_blind_spot():
    """The dataset's design property: gate-clean AND impersonal-guard-clean.

    If a case trips either deterministic rail, it isn't measuring the judge —
    it's measuring a rail we already trust, and it would inflate the judge's
    apparent value. This is the test that keeps the corpus honest.
    """
    from jim.monitors.impersonal import check_impersonal
    from jim.research.gate import check_sourcing

    snap = judge_snapshot()
    for case in JUDGE_CASES:
        gate = check_sourcing(case.memo, snap)
        assert gate.passed, (
            f"{case.name}: memo must be gate-clean, got "
            f"{[(v.figure, v.reason) for v in gate.violations]}"
        )
        tone = check_impersonal(case.memo)
        assert tone.passed, f"{case.name}: memo must pass the impersonal guard, got {tone.violations}"


# --- calibration math -------------------------------------------------------------


def _sample(name, family, faithful, scores):
    return JudgeSample(name=name, family=family, label_faithful=faithful, scores=scores)


def _perfect_samples() -> list[JudgeSample]:
    out = []
    for i in range(4):
        out.append(_sample(f"f{i}", "faithful", True, [0.97, 0.96, 0.98]))
    for i, fam in enumerate(UNFAITHFUL_FAMILIES):
        out.append(_sample(f"u{i}", fam, False, [0.1, 0.15, 0.2]))
    return out


def test_confusion_perfect_judge():
    at = confusion_at(_perfect_samples(), 0.8)
    assert at["tp"] == 5 and at["tn"] == 4 and at["fp"] == 0 and at["fn"] == 0
    assert at["sensitivity"] == 1.0 and at["specificity"] == 1.0
    assert at["balanced_accuracy"] == 1.0 and at["false_reject_rate"] == 0.0


def test_confusion_useless_judge_scores_everything_high():
    # A judge that passes everything: specificity 1, sensitivity 0 → BA 0.5.
    samples = [
        _sample("f0", "faithful", True, [0.95]),
        _sample("u0", "editorialization", False, [0.95]),
    ]
    at = confusion_at(samples, 0.8)
    assert at["sensitivity"] == 0.0 and at["specificity"] == 1.0
    assert at["balanced_accuracy"] == 0.5


def test_median_is_robust_to_one_flaky_repeat():
    s = _sample("u0", "causal_overreach", False, [0.1, 0.95, 0.2])  # median 0.2
    at = confusion_at([s], 0.8)
    assert at["tp"] == 1 and at["fn"] == 0


def test_flip_rate_counts_straddling_verdicts():
    samples = [
        _sample("a", "faithful", True, [0.75, 0.85]),  # straddles 0.8 → flip
        _sample("b", "faithful", True, [0.9, 0.95]),  # stable
        _sample("c", "wrong_citation", False, [0.1, 0.2]),  # stable
        _sample("d", "faithful", True, [0.9]),  # single repeat: excluded
    ]
    assert flip_rate(samples, 0.8) == round(1 / 3, 4)
    assert flip_rate([_sample("d", "faithful", True, [0.9])], 0.8) is None


def test_per_family_breakdown_shapes():
    fams = per_family_breakdown(_perfect_samples(), 0.8)
    assert fams["faithful"]["pass_rate"] == 1.0
    for fam in UNFAITHFUL_FAMILIES:
        assert fams[fam]["recall"] == 1.0


def test_choose_threshold_respects_false_reject_cap():
    # At t=0.9 the judge catches everything but kills a faithful memo
    # (frr 0.5 > cap); at t=0.7 it misses one lie but never false-rejects.
    samples = [
        _sample("f0", "faithful", True, [0.85]),  # flagged at 0.9, fine at 0.7
        _sample("f1", "faithful", True, [0.95]),
        _sample("u0", "unsupported_claim", False, [0.75]),  # missed at 0.7
        _sample("u1", "editorialization", False, [0.3]),
        _sample("u2", "causal_overreach", False, [0.4]),
        _sample("u3", "wrong_citation", False, [0.5]),
    ]
    sweep = threshold_sweep(samples)
    chosen = choose_threshold(sweep, min_balanced_accuracy=0.6, max_false_reject=0.05)
    assert chosen is not None
    assert chosen["threshold"] < 0.9, "must not pick the threshold that false-rejects"
    assert chosen["false_reject_rate"] == 0.0
    assert chosen["floor_met"] is True


def test_choose_threshold_reports_floor_not_met():
    # Inverted judge: high scores on lies, low on truths — no threshold works.
    samples = [
        _sample("f0", "faithful", True, [0.2]),
        _sample("u0", "editorialization", False, [0.95]),
    ]
    chosen = choose_threshold(
        threshold_sweep(samples), min_balanced_accuracy=0.85, max_false_reject=0.05
    )
    assert chosen is not None and chosen["floor_met"] is False


def test_calibration_report_shape_and_empty_input():
    report = calibration_report(
        _perfect_samples(),
        configured_threshold=0.8,
        min_balanced_accuracy=0.85,
        max_false_reject=0.05,
    )
    assert report["cases"] == 9 and report["faithful"] == 4 and report["unfaithful"] == 5
    assert report["repeats"] == 3
    assert report["at_configured"]["balanced_accuracy"] == 1.0
    assert len(report["sweep"]) == 10  # 0.5 … 0.95
    assert report["chosen"]["floor_met"] is True
    json.dumps(report)  # persists as part of the run document

    empty = calibration_report(
        [], configured_threshold=0.8, min_balanced_accuracy=0.85, max_false_reject=0.05
    )
    assert empty["cases"] == 0 and empty["chosen"] is None


# --- runner + CLI (scripted judge; no model, no key) -------------------------------


def _scripted_judge(label_by_memo: dict[str, bool], *, noise: float = 0.0):
    """A fake judge that scores by the human label: faithful high, unfaithful low."""
    calls = {"n": 0}

    async def fake(memo, snapshot, **_):
        calls["n"] += 1
        faithful = label_by_memo[memo]
        score = (0.95 if faithful else 0.1) + noise
        return JudgeResult(
            skipped=False,
            passed=score >= 0.8,
            score=score,
            issues=[] if faithful else ["unsupported: scripted"],
        )

    return fake, calls


async def test_run_suites_judge_document_shape(monkeypatch, tmp_path):
    from jim.eval.runner import run_suites

    labels = {c.memo: c.label_faithful for c in JUDGE_CASES}
    fake, calls = _scripted_judge(labels)
    monkeypatch.setattr("jim.research.judge.judge_faithfulness", fake)

    run = await run_suites(["judge"], repeats=2)
    block = run["suites"]["judge"]
    assert block["aggregate"]["cases"] == len(JUDGE_CASES) * 2
    assert calls["n"] == len(JUDGE_CASES) * 2
    # Perfect scripted judge: full agreement with labels, floor met.
    assert block["aggregate"]["pass_rate"] == 1.0
    cal = block["calibration"]
    assert cal["cases"] == len(JUDGE_CASES) and cal["repeats"] == 2
    assert cal["at_configured"]["balanced_accuracy"] == 1.0
    assert cal["chosen"]["floor_met"] is True
    s = run["summary"]
    assert s["judge_agreement_rate"] == 1.0
    assert s["judge_floor_met"] is True
    assert s["judge_chosen_threshold"] is not None
    json.dumps(run)  # JSON-serializable as persisted


async def test_run_suites_judge_skipped_verdicts_fail_loudly(monkeypatch):
    from jim.eval.runner import run_suites

    async def skipping(memo, snapshot, **_):
        return JudgeResult.skip()

    monkeypatch.setattr("jim.research.judge.judge_faithfulness", skipping)
    run = await run_suites(["judge"], repeats=1)
    block = run["suites"]["judge"]
    # Every sample unusable → every case an error, zero graded, no chosen point.
    assert block["aggregate"]["passed"] == 0
    assert block["aggregate"]["errors"] == len(JUDGE_CASES)
    assert block["calibration"]["cases"] == 0
    assert block["calibration"]["chosen"] is None
    assert run["summary"]["judge_floor_met"] is None


def test_cli_judge_calibrate_requires_credential(monkeypatch, capsys):
    from jim.eval.cli import main

    monkeypatch.setattr("jim.llm.live_llm_available", lambda *a, **k: False)
    monkeypatch.setattr("sys.argv", ["jim-eval", "judge-calibrate"])
    assert main() == 2
    err = capsys.readouterr().err
    assert "credential" in err and "deliberate spend" in err


def test_cli_judge_calibrate_end_to_end_with_scripted_judge(monkeypatch, capsys, tmp_path):
    from jim.eval.cli import main

    labels = {c.memo: c.label_faithful for c in JUDGE_CASES}
    fake, _ = _scripted_judge(labels)
    monkeypatch.setattr("jim.research.judge.judge_faithfulness", fake)
    monkeypatch.setattr("jim.llm.live_llm_available", lambda *a, **k: True)
    monkeypatch.setattr(
        "sys.argv",
        ["jim-eval", "judge-calibrate", "--repeats", "1", "--no-save", "--label", "unit"],
    )
    assert main() == 0  # floor met → exit 0
    out = capsys.readouterr().out
    assert "chosen threshold" in out and "FLOOR MET" in out

    # An inverted judge (fails everything faithful) must exit nonzero.
    async def inverted(memo, snapshot, **_):
        faithful = labels[memo]
        score = 0.1 if faithful else 0.95
        return JudgeResult(skipped=False, passed=score >= 0.8, score=score, issues=[])

    monkeypatch.setattr("jim.research.judge.judge_faithfulness", inverted)
    monkeypatch.setattr(
        "sys.argv", ["jim-eval", "judge-calibrate", "--repeats", "1", "--no-save"]
    )
    assert main() == 1
    assert "FLOOR NOT MET" in capsys.readouterr().out

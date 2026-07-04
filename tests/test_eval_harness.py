"""The eval harness itself, tested offline: datasets stay truthful, metrics add
up, runs persist + diff correctly, and the dashboard API serves them.

The dataset-integrity tests are the important ones: every labeled expectation in
the gate/guard/scenario suites is executed for real, so a mislabeled case (or a
behavior change in the thing it pins) fails here — the eval can't silently rot.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from jim.eval.metrics import CaseResult, aggregate_cases, percentile


# --- dataset integrity --------------------------------------------------------


def test_every_gate_case_behaves_as_labeled():
    from jim.eval.runner import run_suite_gate

    failures = [c.name for c in run_suite_gate() if not c.passed]
    assert failures == [], f"gate cases disagree with their labels: {failures}"


def test_every_guard_case_passes():
    from jim.eval.runner import run_suite_guards

    cases = run_suite_guards()
    failures = [(c.name, c.details, c.error) for c in cases if not c.passed]
    assert failures == [], f"guard cases failing: {failures}"
    # all five guard families must be represented
    categories = {c.name.split(".", 1)[0] for c in cases}
    assert categories == {"impersonal", "identifier", "completeness", "materiality", "monitor_nl"}


async def test_every_scenario_passes():
    from jim.eval.runner import run_suite_scenarios

    cases = await run_suite_scenarios()
    failures = [(c.name, c.details, c.error) for c in cases if not c.passed]
    assert failures == [], f"scenarios failing: {failures}"


async def test_scenarios_leave_no_trace_in_the_real_store():
    """Scenario runs must be hermetic: fresh MemoryStore per scenario, nothing
    written through get_store()."""
    from jim.eval.runner import run_suite_scenarios
    from jim.store import get_store, reset_store

    reset_store()
    await run_suite_scenarios()
    summary = await get_store().margin_summary()
    assert summary["total_queries"] == 0
    reset_store()


# --- metrics -------------------------------------------------------------------


def test_percentile_interpolates():
    assert percentile([], 95) == 0.0
    assert percentile([5.0], 95) == 5.0
    assert percentile([1.0, 2.0, 3.0, 4.0], 50) == pytest.approx(2.5)
    assert percentile([1.0, 2.0, 3.0, 4.0], 100) == 4.0


def test_aggregate_cases_rolls_up():
    cases = [
        CaseResult(suite="s", name="a", passed=True, score=1.0, latency_ms=10, cost_usd=0.01),
        CaseResult(suite="s", name="b", passed=False, latency_ms=30, cost_usd=0.03, error="boom"),
    ]
    agg = aggregate_cases(cases)
    assert agg["cases"] == 2 and agg["passed"] == 1 and agg["pass_rate"] == 0.5
    assert agg["mean_score"] == 1.0  # None scores excluded
    assert agg["latency_p50_ms"] == pytest.approx(20.0)
    assert agg["total_cost_usd"] == pytest.approx(0.04)
    assert agg["errors"] == 1


# --- storage ---------------------------------------------------------------------


@pytest.fixture
def eval_dir(tmp_path, monkeypatch):
    fake = lambda: SimpleNamespace(eval_runs_dir=str(tmp_path))  # noqa: E731
    monkeypatch.setattr("jim.eval.storage.get_settings", fake)
    return tmp_path


def _mini_run(run_id: str, *, gate_cases=None, live=None) -> dict:
    suites = {}
    if gate_cases is not None:
        n = len(gate_cases)
        ok = sum(1 for _, p in gate_cases if p)
        suites["gate"] = {
            "aggregate": {
                "cases": n,
                "passed": ok,
                "pass_rate": ok / n if n else None,
                "total_cost_usd": 0.0,
                "latency_p50_ms": 0,
                "latency_p95_ms": 0,
            },
            "cases": [{"suite": "gate", "name": name, "passed": p} for name, p in gate_cases],
        }
    if live is not None:
        suites["live"] = {"aggregate": live, "cases": []}
    return {
        "schema_version": 1,
        "run_id": run_id,
        "label": None,
        "started_at": "2026-07-01T00:00:00+00:00",
        "git": {"sha": "abc1234", "branch": "main"},
        "suites": suites,
        "summary": {"offline_pass_rate": 1.0, "total_cost_usd": 0.0},
    }


def test_storage_round_trip_and_baseline(eval_dir):
    from jim.eval import storage

    a = _mini_run("20260701T000000Z-aaa", gate_cases=[("x", True)])
    b = _mini_run("20260702T000000Z-bbb", gate_cases=[("x", True)])
    storage.save_run(a)
    storage.save_run(b)

    assert storage.list_run_ids() == [a["run_id"], b["run_id"]]
    assert storage.load_run("latest")["run_id"] == b["run_id"]
    assert storage.load_run("20260701")["run_id"] == a["run_id"]  # unique prefix
    assert [r["run_id"] for r in storage.list_runs()] == [a["run_id"], b["run_id"]]

    assert storage.get_baseline() is None
    storage.set_baseline("20260701")
    assert storage.get_baseline() == a["run_id"]
    assert storage.load_run("baseline")["run_id"] == a["run_id"]
    storage.clear_baseline()
    assert storage.get_baseline() is None

    with pytest.raises(FileNotFoundError):
        storage.load_run("20260799")
    with pytest.raises(FileNotFoundError):
        storage.load_run("2026")  # ambiguous prefix


def test_torn_run_file_does_not_break_the_index(eval_dir):
    from jim.eval import storage

    storage.save_run(_mini_run("20260701T000000Z-aaa", gate_cases=[("x", True)]))
    (eval_dir / "20260702T000000Z-bad.json").write_text("{not json")
    assert [r["run_id"] for r in storage.list_runs()] == ["20260701T000000Z-aaa"]


def test_unsafe_run_id_refused(eval_dir):
    from jim.eval import storage

    with pytest.raises(ValueError):
        storage.save_run(_mini_run("../escape", gate_cases=[]))


# --- comparison --------------------------------------------------------------------


def test_offline_newly_failing_case_is_a_regression():
    from jim.eval.compare import compare_runs

    base = _mini_run("a", gate_cases=[("x", True), ("y", True)])
    cand = _mini_run("b", gate_cases=[("x", True), ("y", False)])
    cmp = compare_runs(base, cand)
    assert cmp["verdict"] == "regressed"
    assert cmp["offline"]["newly_failing"] == ["gate:y"]


def test_offline_fixed_case_is_an_improvement():
    from jim.eval.compare import compare_runs

    base = _mini_run("a", gate_cases=[("x", False)])
    cand = _mini_run("b", gate_cases=[("x", True)])
    cmp = compare_runs(base, cand)
    assert cmp["verdict"] == "improved"
    assert cmp["offline"]["fixed"] == ["gate:x"]


def _live_agg(gate=0.9, rubric=0.9, cost=0.02, p95=10_000.0):
    return {
        "cases": 16,
        "passed": 15,
        "pass_rate": 0.94,
        "gate_pass_rate": gate,
        "mean_score": rubric,
        "mean_cost_usd": cost,
        "latency_p50_ms": p95 / 2,
        "latency_p95_ms": p95,
        "total_cost_usd": cost * 16,
    }


def test_live_thresholds_tolerate_noise_but_catch_regressions():
    from jim.eval.compare import compare_runs

    base = _mini_run("a", gate_cases=[("x", True)], live=_live_agg())
    # inside every allowance → flat
    small = _mini_run(
        "b",
        gate_cases=[("x", True)],
        live=_live_agg(gate=0.87, rubric=0.89, cost=0.021, p95=11_000),
    )
    assert compare_runs(base, small)["verdict"] == "flat"
    # gate rate falls past the 0.05 allowance → regressed
    bad_gate = _mini_run("c", gate_cases=[("x", True)], live=_live_agg(gate=0.80))
    cmp = compare_runs(base, bad_gate)
    assert cmp["verdict"] == "regressed"
    assert "live gate pass rate" in cmp["regressions"]
    # cost blows past the 25% allowance → regressed
    pricey = _mini_run("d", gate_cases=[("x", True)], live=_live_agg(cost=0.03))
    assert "live mean $/run" in compare_runs(base, pricey)["regressions"]
    # a genuine improvement reads as improved
    better = _mini_run("e", gate_cases=[("x", True)], live=_live_agg(gate=0.97, rubric=0.95))
    assert compare_runs(base, better)["verdict"] == "improved"


# --- runner orchestration -------------------------------------------------------------


async def test_run_suites_offline_document_shape(eval_dir):
    from jim.eval.runner import run_suites

    run = await run_suites(["gate", "guards", "scenarios"], label="unit")
    assert run["schema_version"] == 1
    assert set(run["suites"]) == {"gate", "guards", "scenarios"}
    assert run["label"] == "unit"
    assert run["summary"]["all_offline_passed"] is True
    assert run["summary"]["offline_cases"] == sum(
        b["aggregate"]["cases"] for b in run["suites"].values()
    )
    # the document is JSON-serializable as persisted
    json.dumps(run)


async def test_run_suites_rejects_unknown_suite(eval_dir):
    from jim.eval.runner import run_suites

    with pytest.raises(ValueError):
        await run_suites(["nope"])


# --- CLI ---------------------------------------------------------------------------


def test_cli_offline_run_exits_zero(eval_dir, monkeypatch, capsys):
    from jim.eval.cli import main

    monkeypatch.setattr("sys.argv", ["jim-eval", "run", "--suite", "gate", "--no-save"])
    assert main() == 0
    out = capsys.readouterr().out
    assert "gate" in out and "not saved" in out


def test_cli_gate_only_back_compat(eval_dir, monkeypatch, capsys):
    from jim.eval.cli import main

    monkeypatch.setattr("sys.argv", ["jim-eval", "--gate-only"])
    assert main() == 0
    assert "gate" in capsys.readouterr().out


def test_cli_run_persists_and_baseline_flow(eval_dir, monkeypatch, capsys):
    from jim.eval import storage
    from jim.eval.cli import main

    monkeypatch.setattr("sys.argv", ["jim-eval", "run", "--suite", "gate", "--label", "one"])
    assert main() == 0
    run_ids = storage.list_run_ids()
    assert len(run_ids) == 1

    monkeypatch.setattr("sys.argv", ["jim-eval", "baseline", "set", "latest"])
    assert main() == 0
    assert storage.get_baseline() == run_ids[0]

    monkeypatch.setattr("sys.argv", ["jim-eval", "run", "--suite", "gate", "--compare-baseline"])
    assert main() == 0
    assert "verdict" in capsys.readouterr().out.lower()


# --- dashboard API -------------------------------------------------------------------


def test_ui_endpoints(eval_dir):
    from fastapi.testclient import TestClient

    from jim.eval import storage
    from jim.eval.ui import build_app

    storage.save_run(_mini_run("20260701T000000Z-aaa", gate_cases=[("x", True)]))
    storage.save_run(_mini_run("20260702T000000Z-bbb", gate_cases=[("x", False)]))
    storage.set_baseline("20260701T000000Z-aaa")
    client = TestClient(build_app())

    page = client.get("/")
    assert page.status_code == 200 and "jim — evals" in page.text

    runs = client.get("/api/runs").json()
    assert [r["run_id"] for r in runs["runs"]] == [
        "20260701T000000Z-aaa",
        "20260702T000000Z-bbb",
    ]
    assert runs["baseline"] == "20260701T000000Z-aaa"

    one = client.get("/api/runs/latest").json()
    assert one["run_id"] == "20260702T000000Z-bbb"
    assert client.get("/api/runs/20269999").status_code == 404

    cmp = client.get("/api/compare", params={"base": "baseline", "cand": "latest"}).json()
    assert cmp["verdict"] == "regressed"
    assert cmp["offline"]["newly_failing"] == ["gate:x"]

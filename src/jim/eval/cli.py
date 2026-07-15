"""``jim-eval`` — run, persist, compare, and browse the eval suite.

    uv run jim-eval                          # offline suites (gate+guards+scenarios)
    uv run jim-eval run --suite all          # + the live lift eval (needs a key)
    uv run jim-eval run --suite live AAPL KO # live suite on specific tickers
    uv run jim-eval run --compare-baseline   # fail (exit 1) on regression vs baseline
    uv run jim-eval judge-calibrate          # judge vs labeled corpus (needs a key, ~$1)
    uv run jim-eval list                     # persisted runs
    uv run jim-eval show latest              # one run in detail
    uv run jim-eval compare baseline latest  # diff two runs
    uv run jim-eval baseline set <run_id>    # promote a known-good run
    uv run jim-eval ui                       # results dashboard on :4023
    uv run jim-eval --gate-only              # back-compat: gate regression only

Offline suites are deterministic and CI-gate merges: any failing case exits 1.
Every ``run`` persists a JSON document under EVAL_RUNS_DIR (default
``./eval_runs``) so the dashboard can plot pass-rate/quality/cost/latency over
time.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from jim.eval.runner import ALL_SUITES, OFFLINE_SUITES


# --- printing helpers ---------------------------------------------------------


def _fmt(value, pattern="{:.4f}") -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return pattern.format(value)
    return str(value)


def _print_suite_table(run: dict, *, saved: bool = True) -> None:
    label = f" ({run['label']})" if run.get("label") else ""
    print(f"\n=== eval run {run['run_id']}{label} ===")
    git = run.get("git") or {}
    where = f"saved under {run['run_id']}.json" if saved else "not saved (--no-save)"
    print(
        f"  commit {git.get('sha') or '—'} on {git.get('branch') or '—'} · "
        f"{run.get('duration_seconds', 0)}s · {where}"
    )
    header = f"  {'suite':<12}{'cases':>7}{'passed':>8}{'rate':>8}{'p95 ms':>9}{'cost $':>10}"
    print("\n" + header)
    print("  " + "-" * (len(header) - 2))
    for name, block in run["suites"].items():
        a = block["aggregate"]
        print(
            f"  {name:<12}{a['cases']:>7}{a['passed']:>8}"
            f"{_fmt(a['pass_rate'], '{:.2%}'):>8}{a['latency_p95_ms']:>9.1f}"
            f"{a['total_cost_usd']:>10.4f}"
        )

    failures = [c for block in run["suites"].values() for c in block["cases"] if not c["passed"]]
    if failures:
        print(f"\n  {len(failures)} failing case(s):")
        for c in failures[:25]:
            reason = c.get("error") or _failure_reason(c)
            print(f"    ✗ {c['suite']}:{c['name']}" + (f" — {reason}" if reason else ""))
        if len(failures) > 25:
            print(f"    … and {len(failures) - 25} more (jim-eval show {run['run_id']})")

    live = run["suites"].get("live")
    if live and live.get("variants"):
        print("\n  live: single-pass vs debate")
        head = f"    {'metric':<22}{'single_pass':>13}{'debate':>13}{'lift':>10}"
        print(head)
        print("    " + "-" * (len(head) - 4))
        sp = live["variants"].get("single_pass", {})
        db = live["variants"].get("debate", {})
        lift = live.get("lift", {})
        for label, key in (
            ("ok rate", "pass_rate"),
            ("gate pass rate", "gate_pass_rate"),
            ("mean rubric", "mean_score"),
            ("mean faithfulness", "mean_faithfulness"),
            ("mean $/run", "mean_cost_usd"),
            ("p95 latency ms", "latency_p95_ms"),
        ):
            lift_s = _fmt(lift.get(key), "{:+.4f}") if key in lift else ""
            print(f"    {label:<22}{_fmt(sp.get(key)):>13}{_fmt(db.get(key)):>13}{lift_s:>10}")

    s = run["summary"]
    print(
        f"\n  offline: {s['offline_passed']}/{s['offline_cases']} passed"
        + (
            f" · live ok rate {_fmt(s.get('live_ok_rate'), '{:.2%}')}"
            if "live_ok_rate" in s
            else ""
        )
        + f" · total eval cost ${s['total_cost_usd']:.4f}\n"
    )


def _failure_reason(case: dict) -> str:
    d = case.get("details") or {}
    if "expected_pass" in d:
        return f"expected pass={d['expected_pass']}, got {d.get('got_pass')}"
    if d.get("status"):
        return f"status={d['status']}"
    return ""


def _print_compare(cmp: dict) -> None:
    print(f"\n=== compare {cmp['base_run']} → {cmp['cand_run']} ===")
    off = cmp["offline"]
    for suite, row in off["suites"].items():
        print(
            f"  {suite:<12} pass rate {_fmt(row['base_pass_rate'], '{:.2%}')} → "
            f"{_fmt(row['cand_pass_rate'], '{:.2%}')}"
        )
    for name in off["newly_failing"]:
        print(f"    ✗ newly failing: {name}")
    for name in off["fixed"]:
        print(f"    ✓ fixed: {name}")
    if cmp.get("live"):
        print("  live (thresholded):")
        for row in cmp["live"]["checks"]:
            mark = {"regressed": "✗", "improved": "✓", "flat": "·", "n/a": " "}[row["status"]]
            delta = _fmt(row.get("delta"), "{:+.4f}")
            print(
                f"    {mark} {row['label']:<22} {_fmt(row['base'])} → {_fmt(row['cand'])}"
                f"  Δ {delta}  [{row['status']}]"
            )
    print(f"\n  verdict: {cmp['verdict'].upper()}\n")


# --- subcommands ----------------------------------------------------------------


def _cmd_run(args) -> int:
    from jim.eval import storage
    from jim.eval.compare import compare_runs
    from jim.eval.runner import run_suites

    if args.suite == "offline":
        names = list(OFFLINE_SUITES)
    elif args.suite == "all":
        names = list(ALL_SUITES)
    else:
        names = [args.suite]

    from jim.llm import live_llm_available, set_auth_mode

    set_auth_mode(getattr(args, "auth_mode", None))
    if "live" in names:
        if not live_llm_available():
            print(
                "jim-eval: the live suite needs an LLM credential (ANTHROPIC_API_KEY, or "
                "--auth-mode subscription with `claude login`); running offline suites only.",
                file=sys.stderr,
            )
            names = [n for n in names if n != "live"]
            if not names:
                return 2

    run = asyncio.run(
        run_suites(names, tickers=args.tickers or None, repeats=args.repeats, label=args.label)
    )

    exit_code = 0 if run["summary"].get("all_offline_passed", True) else 1
    if not args.no_save:
        storage.save_run(run)
    _print_suite_table(run, saved=not args.no_save)

    if args.compare_baseline:
        baseline_id = storage.get_baseline()
        if baseline_id is None:
            print("  (no baseline set — skip regression check; jim-eval baseline set <id>)")
        else:
            cmp = compare_runs(storage.load_run("baseline"), run)
            _print_compare(cmp)
            if cmp["verdict"] == "regressed":
                exit_code = 1
    return exit_code


def _print_judge_calibration(run: dict) -> None:
    block = run["suites"].get("judge") or {}
    cal = block.get("calibration") or {}
    if not cal or not cal.get("cases"):
        print("  no graded samples — every judge call was skipped or errored.")
        return
    at = cal.get("at_configured") or {}
    print(
        f"\n  calibration: {cal['cases']} cases ({cal['faithful']} faithful / "
        f"{cal['unfaithful']} unfaithful) × {cal['repeats']} repeat(s)"
    )
    print(
        f"  at configured threshold {at.get('threshold')}: "
        f"balanced accuracy {_fmt(at.get('balanced_accuracy'))} · "
        f"sensitivity {_fmt(at.get('sensitivity'))} · "
        f"false-reject {_fmt(at.get('false_reject_rate'))} · "
        f"flip rate {_fmt(at.get('flip_rate'))}"
    )
    fams = cal.get("per_family") or {}
    if fams:
        print("  per family:")
        for name, fam in fams.items():
            rate = fam.get("recall", fam.get("pass_rate"))
            kind = "pass rate" if name == "faithful" else "recall"
            print(f"    {name:<24}{fam['correct']}/{fam['cases']}  {kind} {_fmt(rate)}")
    print("  threshold sweep:")
    head = f"    {'t':>5}{'bal acc':>9}{'sens':>7}{'f-rej':>7}{'flips':>7}"
    print(head)
    for row in cal.get("sweep") or []:
        print(
            f"    {row['threshold']:>5}{_fmt(row.get('balanced_accuracy')):>9}"
            f"{_fmt(row.get('sensitivity')):>7}{_fmt(row.get('false_reject_rate')):>7}"
            f"{_fmt(row.get('flip_rate')):>7}"
        )
    chosen = cal.get("chosen") or {}
    floor = cal.get("floor") or {}
    if chosen:
        verdict = "FLOOR MET" if chosen.get("floor_met") else "FLOOR NOT MET"
        print(
            f"\n  chosen threshold: {chosen.get('threshold')} "
            f"(balanced accuracy {_fmt(chosen.get('balanced_accuracy'))}, "
            f"false-reject {_fmt(chosen.get('false_reject_rate'))}) — {verdict} "
            f"(floor: ba ≥ {floor.get('min_balanced_accuracy')}, "
            f"f-rej ≤ {floor.get('max_false_reject_rate')})"
        )
        if chosen.get("floor_met"):
            print(
                "  next: set JUDGE_THRESHOLD to the chosen value and record this "
                "run_id beside it in config.py (docs/EVAL_LADDER.md, Phase E2)."
            )
        else:
            print(
                "  per the E2 contract: a judge that can't meet the floor at any "
                "threshold must not co-decide ok/rejected — demote to advisory "
                "(docs/EVAL_LADDER.md, Phase E2 kill criteria)."
            )


def _cmd_judge_calibrate(args) -> int:
    from jim.config import get_settings
    from jim.eval import storage
    from jim.eval.runner import run_suites
    from jim.llm import live_llm_available, set_auth_mode

    set_auth_mode(getattr(args, "auth_mode", None))
    if not get_settings().enable_judge:
        print("jim-eval: judge-calibrate needs ENABLE_JUDGE=true.", file=sys.stderr)
        return 2
    if not live_llm_available():
        print(
            "jim-eval: judge-calibrate runs the real judge model and needs an LLM "
            "credential (ANTHROPIC_API_KEY, or --auth-mode subscription via "
            "`claude login`). This is deliberate spend (~$1 per calibration) — see "
            "docs/EVAL_LADDER.md, Phase E2.",
            file=sys.stderr,
        )
        return 2

    run = asyncio.run(run_suites(["judge"], repeats=args.repeats, label=args.label))
    if not args.no_save:
        storage.save_run(run)
    _print_suite_table(run, saved=not args.no_save)
    _print_judge_calibration(run)
    chosen = (run["suites"].get("judge", {}).get("calibration") or {}).get("chosen") or {}
    return 0 if chosen.get("floor_met") else 1


def _cmd_list(args) -> int:
    from jim.eval import storage

    runs = storage.list_runs()
    baseline = storage.get_baseline()
    if not runs:
        print("no eval runs saved yet — run `jim-eval run` first")
        return 0
    header = f"{'run id':<26}{'label':<18}{'offline':>9}{'live ok':>9}{'rubric':>8}{'cost $':>9}"
    print(header)
    print("-" * len(header))
    for r in runs:
        s = r["summary"]
        mark = " *" if r["run_id"] == baseline else ""
        print(
            f"{r['run_id']:<26}{(r.get('label') or '')[:16]:<18}"
            f"{_fmt(s.get('offline_pass_rate'), '{:.0%}'):>9}"
            f"{_fmt(s.get('live_ok_rate'), '{:.0%}'):>9}"
            f"{_fmt(s.get('live_mean_rubric'), '{:.3f}'):>8}"
            f"{_fmt(s.get('total_cost_usd'), '{:.4f}'):>9}{mark}"
        )
    if baseline:
        print(f"\n* baseline: {baseline}")
    return 0


def _cmd_show(args) -> int:
    from jim.eval import storage

    run = storage.load_run(args.run)
    if args.json:
        print(json.dumps(run, indent=2))
    else:
        _print_suite_table(run)
    return 0


def _cmd_compare(args) -> int:
    from jim.eval import storage
    from jim.eval.compare import compare_runs

    cmp = compare_runs(storage.load_run(args.base), storage.load_run(args.cand))
    if args.json:
        print(json.dumps(cmp, indent=2))
    else:
        _print_compare(cmp)
    return 1 if cmp["verdict"] == "regressed" else 0


def _cmd_baseline(args) -> int:
    from jim.eval import storage

    if args.action == "show" or args.action is None:
        print(storage.get_baseline() or "(no baseline set)")
    elif args.action == "set":
        if not args.run:
            print("usage: jim-eval baseline set <run_id|latest>", file=sys.stderr)
            return 2
        print(f"baseline set to {storage.set_baseline(args.run)}")
    elif args.action == "clear":
        storage.clear_baseline()
        print("baseline cleared")
    return 0


def _cmd_ui(args) -> int:
    import uvicorn

    from jim.eval.ui import build_app

    print(f"jim eval dashboard → http://127.0.0.1:{args.port}")
    uvicorn.run(build_app(), host=args.host, port=args.port, log_level="warning")
    return 0


# --- entry ------------------------------------------------------------------------


def main() -> int:
    argv = sys.argv[1:]
    # Back-compat: `jim-eval --gate-only` predates the subcommands.
    if argv and argv[0] == "--gate-only":
        argv = ["run", "--suite", "gate", "--no-save"]
    if not argv:
        argv = ["run"]

    parser = argparse.ArgumentParser(prog="jim-eval", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="run eval suites and persist the results")
    p_run.add_argument(
        "tickers",
        nargs="*",
        help="restrict the live suite to these tickers (default: held-out set)",
    )
    p_run.add_argument(
        "--suite",
        default="offline",
        choices=["offline", "all", *ALL_SUITES],
        help="offline = gate+guards+scenarios (default); all = offline + live",
    )
    p_run.add_argument("--label", help="human label stored with the run")
    p_run.add_argument(
        "--repeats", type=int, default=1, help="live repeats per (ticker, variant) for variance"
    )
    p_run.add_argument("--no-save", action="store_true", help="don't persist this run")
    p_run.add_argument(
        "--auth-mode",
        choices=["api_key", "subscription", "auto"],
        default=None,
        help="LLM auth for the live suite (default: LLM_AUTH_MODE / api_key). "
        "subscription uses `claude login` via the Claude Agent SDK — dev-loop only.",
    )
    p_run.add_argument(
        "--compare-baseline",
        action="store_true",
        help="diff against the baseline run and exit 1 on regression",
    )
    p_run.set_defaults(fn=_cmd_run)

    p_judge = sub.add_parser(
        "judge-calibrate",
        help="grade the pinned judge model against the labeled corpus (needs a key)",
    )
    p_judge.add_argument("--label", help="human label stored with the run")
    p_judge.add_argument(
        "--repeats",
        type=int,
        default=3,
        help="judge calls per case, for verdict-stability measurement (default 3)",
    )
    p_judge.add_argument("--no-save", action="store_true", help="don't persist this run")
    p_judge.add_argument(
        "--auth-mode",
        choices=["api_key", "subscription", "auto"],
        default=None,
        help="LLM auth (default: LLM_AUTH_MODE / api_key). subscription is dev-loop only.",
    )
    p_judge.set_defaults(fn=_cmd_judge_calibrate)

    p_list = sub.add_parser("list", help="list persisted runs")
    p_list.set_defaults(fn=_cmd_list)

    p_show = sub.add_parser("show", help="show one run")
    p_show.add_argument("run", nargs="?", default="latest", help="run id, prefix, latest, baseline")
    p_show.add_argument("--json", action="store_true")
    p_show.set_defaults(fn=_cmd_show)

    p_cmp = sub.add_parser("compare", help="diff two runs (exit 1 on regression)")
    p_cmp.add_argument("base", help="base run id / latest / baseline")
    p_cmp.add_argument(
        "cand", nargs="?", default="latest", help="candidate run id (default latest)"
    )
    p_cmp.add_argument("--json", action="store_true")
    p_cmp.set_defaults(fn=_cmd_compare)

    p_base = sub.add_parser("baseline", help="show/set/clear the baseline run")
    p_base.add_argument("action", nargs="?", choices=["show", "set", "clear"])
    p_base.add_argument("run", nargs="?", help="run id (for `set`)")
    p_base.set_defaults(fn=_cmd_baseline)

    p_ui = sub.add_parser("ui", help="serve the eval results dashboard")
    p_ui.add_argument("--host", default="127.0.0.1")
    p_ui.add_argument("--port", type=int, default=4023)
    p_ui.set_defaults(fn=_cmd_ui)

    args = parser.parse_args(argv)
    try:
        return args.fn(args)
    except FileNotFoundError as e:
        print(f"jim-eval: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

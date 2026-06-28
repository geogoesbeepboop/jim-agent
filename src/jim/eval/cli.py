"""``jim-eval`` — run the regression eval suite.

    uv run jim-eval --gate-only          # offline: gate regression only (no key)
    uv run jim-eval                      # full: gate + debate-vs-single-pass lift
    uv run jim-eval AAPL MSFT            # restrict the live set to these tickers

Gate regression gates merges and needs no API key. The lift comparison needs
ANTHROPIC_API_KEY and logs to Langfuse if configured.
"""

from __future__ import annotations

import argparse
import asyncio

from jim.eval.runner import run_eval


def _print_report(report) -> None:
    g = report.gate_regression
    print("\n=== Gate regression (deterministic) ===")
    for c in g["cases"]:
        mark = "✓" if c["correct"] else "✗"
        print(f"  {mark} {c['name']:<40} expected_pass={c['expected_pass']} got={c['got_pass']}")
    print(f"  {g['correct']}/{g['total']} correct")

    if not report.single_pass:
        return

    print("\n=== Debate vs single-pass (held-out tickers) ===")
    header = f"  {'metric':<26}{'single_pass':>14}{'debate':>14}{'lift':>12}"
    print(header)
    print("  " + "-" * (len(header) - 2))

    def row(label, key, fmt="{:.4f}"):
        sp = report.single_pass.get(key)
        db = report.debate.get(key)
        lift = report.lift.get(key)
        sp_s = fmt.format(sp) if isinstance(sp, (int, float)) else str(sp)
        db_s = fmt.format(db) if isinstance(db, (int, float)) else str(db)
        lift_s = f"{lift:+.4f}" if isinstance(lift, (int, float)) else ""
        print(f"  {label:<26}{sp_s:>14}{db_s:>14}{lift_s:>12}")

    row("gate pass rate", "gate_pass_rate")
    row("ok rate", "ok_rate")
    row("mean coverage", "mean_coverage")
    row("mean faithfulness", "mean_faithfulness")
    row("mean facts/run", "mean_facts", "{:.1f}")
    row("mean inference $/run", "mean_inference_cost_usd", "{:.5f}")
    print()


def main() -> int:
    parser = argparse.ArgumentParser(prog="jim-eval")
    parser.add_argument("tickers", nargs="*", help="Restrict the live set (default: held-out set)")
    parser.add_argument(
        "--gate-only", action="store_true", help="Run only the offline gate regression"
    )
    args = parser.parse_args()

    report = asyncio.run(run_eval(args.tickers or None, live=not args.gate_only))
    _print_report(report)

    gate_ok = report.gate_regression["correct"] == report.gate_regression["total"]
    return 0 if gate_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

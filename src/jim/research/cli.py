"""``jim-research`` — run the research engine locally (no customer payment).

    uv run jim-research AAPL                      # fundamentals (EDGAR, free upstream)
    uv run jim-research MSFT --mode agent
    uv run jim-research WETH --product token      # on-chain (buys from The Graph/mock)
    uv run jim-research NVDA --json

Needs ANTHROPIC_API_KEY for the synthesizer. The `token` product also buys
upstream data over x402 (mock vendor on testnet unless GRAPH_LIVE=true), so the
seller must be running to serve the mock vendor.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from jim.research.engine import run_research
from jim.research.schemas import ResearchResponse


async def _run(
    identifier: str,
    product: str,
    mode: str,
    as_json: bool,
    high_stakes: bool = False,
    use_memo_cache: bool | None = None,
) -> int:
    result = await run_research(
        identifier,
        product=product,
        mode=mode,
        high_stakes=high_stakes,
        use_memo_cache=use_memo_cache,
    )

    if as_json:
        print(ResearchResponse.from_result(result).model_dump_json(indent=2))
        return 0 if result.ok else 1

    print(f"\n{'=' * 70}")
    print(
        f"{result.entity_name or identifier.upper()}  ({result.ticker})   "
        f"[{result.product}]   status: {result.status.upper()}"
    )
    print(f"ref {result.cik}  ·  as of {result.as_of}  ·  attempts: {result.attempts}")
    print("=" * 70)

    if result.status == "error":
        print(f"\nERROR: {result.error}", file=sys.stderr)
        return 1

    if result.debate:
        print(f"\n--- adversarial review (bull/bear/judge) ---\n{result.debate}\n")

    if result.memo:
        print(f"\n{result.memo}\n")

    if result.gate:
        g = result.gate
        print(
            f"Sourcing gate: {'PASS' if g.passed else 'FAIL'}  "
            f"({g.n_covered}/{g.n_figures} figures sourced, coverage {g.coverage:.0%})"
        )
        for v in g.violations:
            print(f"  ✗ {v.reason}: {v.figure!r}")
    if result.judge and not result.judge.skipped:
        model = f" [{result.judge.model}]" if result.judge.model else ""
        print(
            f"Faithfulness: {result.judge.score:.2f}  "
            f"{'PASS' if result.judge.passed else 'FAIL'}{model}"
        )
        for c in result.judge.unsupported_claims:
            print(f"  ✗ unsupported: {c.claim!r} — {c.reason}")
        for issue in result.judge.issues:
            print(f"  · {issue}")

    if result.completeness:
        comp = result.completeness
        print(
            f"Completeness: {comp.material_coverage:.0%} of material facts cited "
            f"({comp.coverage:.0%} of all facts)"
        )
        for o in comp.material_omissions:
            print(f"  ⚠ omitted material: {o['label']}")

    c = result.cost
    cache_note = ""
    if result.served_from_cache:
        cache_note = "  (memo cache — $0 inference)"
    elif c.get("cache_hit"):
        cache_note = "  (data cache hit)"
    print(
        f"\nEconomics: price_out ${c.get('price_out_usd', 0):.4f}  −  "
        f"data ${c.get('data_cost_usd', 0):.4f}  −  inference ${c.get('inference_cost_usd', 0):.5f}  "
        f"=  margin ${c.get('margin_usd', 0):.4f}{cache_note}"
    )
    print("\nCitations:")
    for line in result.citations():
        print(f"  {line}")
    return 0 if result.ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(prog="jim-research", description="Run a research snapshot.")
    parser.add_argument("identifier", help="Ticker (fundamentals) or token (token product)")
    parser.add_argument("--product", choices=["fundamentals", "token"], default="fundamentals")
    parser.add_argument("--mode", choices=["human", "agent"], default="human")
    parser.add_argument("--json", action="store_true", help="Emit the full JSON response")
    parser.add_argument(
        "--high-stakes",
        action="store_true",
        help="Upgrade the faithfulness judge to the stronger (Sonnet) model",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Bypass the memo cache (always re-synthesize)",
    )
    args = parser.parse_args()
    return asyncio.run(
        _run(
            args.identifier,
            args.product,
            args.mode,
            args.json,
            high_stakes=args.high_stakes,
            use_memo_cache=(False if args.no_cache else None),
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())

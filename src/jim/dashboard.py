"""Per-query margin dashboard — the Phase 2 exit-criteria view.

Reads the margin ledger and shows, per query and in aggregate:
    price_out − data_cost − inference_cost = margin
plus the cache-hit rate (cache hits drive data cost to zero on repackaged sales).
"""

from __future__ import annotations

import asyncio

from jim.store import get_store


async def margin_dashboard(limit: int = 20) -> dict:
    store = get_store()
    return {
        "summary": await store.margin_summary(),
        "recent": await store.recent_queries(limit),
        "monitors": await store.monitor_stats(),
        "trust": await store.trust_scores(),
    }


def render_text(data: dict) -> str:
    s = data["summary"]
    lines = [
        "=" * 78,
        "  jim — per-query margin dashboard",
        "=" * 78,
        f"  Billable queries : {s['billable_queries']}  (of {s['total_queries']} total runs)",
        f"  Revenue          : ${s['revenue_usd']:.4f}",
        f"  Data cost (x402) : ${s['data_cost_usd']:.4f}",
        f"  Inference cost   : ${s['inference_cost_usd']:.4f}",
        f"  Total margin     : ${s['total_margin_usd']:.4f}   ({s['margin_pct']:.1f}% of revenue)",
        f"  Avg margin/query : ${s['avg_margin_usd']:.4f}",
        f"  Cache hit rate   : {s['cache_hit_rate'] * 100:.1f}%",
        "-" * 78,
        f"  {'product':<13}{'id':<10}{'status':<10}{'price':>9}{'data':>9}{'infer':>9}{'margin':>10}{'  cache'}",
        "-" * 78,
    ]
    for q in data["recent"]:
        lines.append(
            f"  {q['product']:<13}{q['identifier']:<10}{q['status']:<10}"
            f"{q['price_out_usd']:>9.4f}{q['cost_in_data_usd']:>9.4f}"
            f"{q['cost_inference_usd']:>9.4f}{q['margin_usd']:>10.4f}"
            f"{'  hit' if q['cache_hit'] else '  miss'}"
        )
    if not data["recent"]:
        lines.append("  (no queries recorded yet — run some research first)")

    trust = data.get("trust") or {}
    if trust:
        lines.extend(
            [
                "-" * 78,
                "  Source trust (Phase 7 — gate pass-rate as reputation)",
                "-" * 78,
            ]
        )
        for row in sorted(trust.values(), key=lambda r: r["score"], reverse=True):
            lines.append(
                f"  {row['source']:<24}score {row['score']:.2f}   "
                f"(pass {row['ok']} · fail {row['fail']})"
            )

    m = data.get("monitors")
    if m and m.get("total_runs"):
        lines.extend(
            [
                "-" * 78,
                "  Monitors (Phase 4)",
                "-" * 78,
                f"  Polls run        : {m['total_runs']}   "
                f"(updates {m['updates_delivered']} · quiet {m['quiet_runs']} · "
                f"baseline {m['baseline_runs']} · errors {m['error_runs']})",
                f"  Materiality rate : {m['materiality_rate'] * 100:.1f}%  "
                f"(share of polls that produced an update)",
                f"  Update revenue   : ${m['revenue_usd']:.4f}   "
                f"data ${m['data_cost_usd']:.4f}   infer ${m['inference_cost_usd']:.4f}   "
                f"margin ${m['total_margin_usd']:.4f}",
                f"  Inference saved  : ${m['inference_saved_usd']:.4f}  "
                f"(quiet polls that paid for NO writing — the materiality gate's value)",
            ]
        )
    lines.append("=" * 78)
    return "\n".join(lines)


def main() -> int:
    data = asyncio.run(margin_dashboard())
    print(render_text(data))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

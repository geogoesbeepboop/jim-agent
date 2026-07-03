"""The public proof page — radical transparency as the pitch (Horizon 1).

Identity registries and KYA frameworks tell you *who* an agent is; nothing in
the 2026 stack tells you whether what it sold you was *true*. jim's answer is
to publish its own evidence, live: every settlement on-chain, every gate
verdict counted, every refusal shown with the money it declined to take, and
every source's trust score computed from outcomes. The page makes one claim —

    research that fails verification is refused before settlement;
    the buyer is never billed for it (ADR-0008)

— and then shows the ledger that proves it. Everything here is a deterministic
rollup of the same store the seller writes; there is no separate marketing
counter to drift. Free to read, like the catalog and the manifest.

Surfaced two ways: ``GET /proof`` (HTML) and ``GET /proof.json`` (machine).
"""

from __future__ import annotations

from jim.admin import _short, address_url, explorer_base, tx_url
from jim.config import get_settings
from jim.research.products import get_products
from jim.store import get_store


async def proof_stats(limit: int = 15, window: int = 500) -> dict:
    """The proof rollup: settlements + verification outcomes + trust.

    ``limit`` bounds the visible feeds; ``window`` bounds the recent-runs slice
    used for the refused-money estimate (priced at *current* catalog prices —
    the honest label rides along in ``refused_note``).
    """
    store = get_store()
    settings = get_settings()

    settlements = await store.receipts_summary()
    recent_settlements = await store.recent_receipts(limit)
    margin = await store.margin_summary()
    trust = await store.trust_scores()
    window_queries = await store.recent_queries(window)

    for r in recent_settlements:
        net = r.get("network") or settings.network
        r["tx_explorer_url"] = tx_url(net, r.get("tx_hash"))
        r["payer_explorer_url"] = address_url(net, r.get("payer"))

    prices = {name: product.price_out_usd for name, product in get_products().items()}
    refused = [q for q in window_queries if q.get("status") == "rejected"]
    refused_usd = round(sum(prices.get(q["product"], 0.0) for q in refused), 6)

    total = margin["total_queries"]
    shipped = margin["billable_queries"]
    return {
        "service": settings.service_name,
        "network": settings.network,
        "is_mainnet": settings.is_mainnet,
        "pay_to": settings.evm_address,
        "explorer": explorer_base(settings.network),
        "invariant": (
            "Research that fails jim's deterministic sourcing gate is refused before "
            "settlement — the buyer is never billed for it."
        ),
        "verification": {
            "runs_gated": total,
            "shipped_verified": shipped,
            "refused_runs": total - shipped,
            "gate_pass_rate": round(shipped / total, 4) if total else None,
            "refused_not_billed_usd": refused_usd,
            "refused_note": (
                f"over the last {len(window_queries)} recorded runs, at current prices"
            ),
            "cache_hit_rate": margin["cache_hit_rate"],
        },
        "settlements": settlements,
        "recent_settlements": recent_settlements,
        "recent_refusals": [
            {
                "product": q["product"],
                "identifier": q["identifier"],
                "declined_usd": prices.get(q["product"], 0.0),
                "created_at": q["created_at"],
            }
            for q in refused[:limit]
        ],
        "trust": sorted(trust.values(), key=lambda r: r["score"], reverse=True),
        "reproduce": (
            "Every number on this page is a deterministic rollup of jim's own store — "
            "settlement receipts (on-chain tx hashes), the query ledger, and the "
            "source-trust events. GET /proof.json for the machine view."
        ),
    }


def _settlement_rows(rows: list[dict]) -> str:
    out = []
    for r in rows:
        ts = str(r.get("created_at") or "")[:19].replace("T", " ")
        tx = r.get("tx_hash")
        tx_cell = (
            f'<a href="{r["tx_explorer_url"]}" target="_blank" rel="noopener">{_short(tx)}</a>'
            if r.get("tx_explorer_url")
            else _short(tx)
        )
        payer = r.get("payer")
        payer_cell = (
            f'<a href="{r["payer_explorer_url"]}" target="_blank" rel="noopener">'
            f"{_short(payer)}</a>"
            if r.get("payer_explorer_url")
            else _short(payer)
        )
        ok = "ok" if r.get("success") else "bad"
        out.append(
            f"<tr><td class='mono muted'>{ts}</td>"
            f"<td>{r.get('product') or '—'}</td>"
            f"<td>{r.get('identifier') or '—'}</td>"
            f"<td class='num'>${r.get('amount_usdc', 0.0):.4f}</td>"
            f"<td class='mono'>{payer_cell}</td>"
            f"<td class='mono'>{tx_cell}</td>"
            f"<td><span class='pill {ok}'>{'settled' if r.get('success') else 'failed'}</span>"
            f"</td></tr>"
        )
    return "\n".join(out) or (
        "<tr><td colspan='7' class='muted'>No settlements yet — the feed fills in "
        "with the first paid x402 call.</td></tr>"
    )


def _refusal_rows(rows: list[dict]) -> str:
    out = []
    for q in rows:
        ts = str(q.get("created_at") or "")[:19].replace("T", " ")
        out.append(
            f"<tr><td class='mono muted'>{ts}</td><td>{q['product']}</td>"
            f"<td>{q['identifier']}</td>"
            f"<td class='num'>${q['declined_usd']:.2f}</td>"
            f"<td><span class='pill bad'>refused — not billed</span></td></tr>"
        )
    return "\n".join(out) or (
        "<tr><td colspan='5' class='muted'>No refusals in the window — every gated run "
        "shipped fully verified.</td></tr>"
    )


def _trust_rows(rows: list[dict]) -> str:
    out = []
    for r in rows:
        out.append(
            f"<tr><td class='mono'>{r['source']}</td>"
            f"<td class='num'>{r['score']:.2f}</td>"
            f"<td class='num'>{r['ok']}</td><td class='num'>{r['fail']}</td>"
            f"<td class='mono muted'>{str(r.get('last_event_at') or '')[:19].replace('T', ' ')}"
            f"</td></tr>"
        )
    return "\n".join(out) or (
        "<tr><td colspan='5' class='muted'>No gated runs yet — trust accrues from "
        "verification outcomes, not reviews.</td></tr>"
    )


def proof_html(data: dict) -> str:
    """Render the proof page — self-contained, dark, auto-refreshing."""
    v = data["verification"]
    s = data["settlements"]
    pass_rate = f"{v['gate_pass_rate'] * 100:.1f}%" if v["gate_pass_rate"] is not None else "—"
    net = "Base mainnet" if data["is_mainnet"] else "Base Sepolia (testnet)"
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<meta http-equiv="refresh" content="30"/>
<title>{data["service"]} — proof</title>
<style>
  :root {{ color-scheme: dark; }}
  body {{ font-family:-apple-system,system-ui,sans-serif; margin:0; background:#0f1419; color:#e6e6e6; }}
  header {{ padding:22px 28px; border-bottom:1px solid #233; }}
  header h1 {{ margin:0; font-size:21px; }}
  header p {{ margin:8px 0 0; color:#9bb; font-size:13px; max-width:760px; }}
  nav {{ margin-top:12px; }} nav a {{ color:#7fd; text-decoration:none; margin-right:14px; font-size:13px; }}
  .wrap {{ max-width:1080px; margin:0 auto; padding:24px 28px; }}
  .invariant {{ background:#10202b; border:1px solid #1f3a4d; border-radius:12px; padding:14px 18px;
                color:#8cf; font-size:14px; margin-bottom:20px; }}
  .grid {{ display:grid; grid-template-columns:repeat(4,1fr); gap:14px; margin-bottom:20px; }}
  .stat {{ background:#1b2733; border:1px solid #2c3e50; border-radius:12px; padding:16px; }}
  .stat .k {{ color:#9bb; font-size:12px; }} .stat .v {{ font-size:24px; margin-top:6px; font-weight:600; }}
  .stat .v.refused {{ color:#f3a; }}
  .card {{ background:#1b2733; border:1px solid #2c3e50; border-radius:12px; padding:16px; margin-bottom:18px; overflow-x:auto; }}
  .card h2 {{ margin:0 0 4px; font-size:14px; color:#cde; }}
  .card p.sub {{ margin:0 0 12px; color:#9bb; font-size:12px; }}
  table {{ width:100%; border-collapse:collapse; font-size:13px; }}
  th {{ text-align:left; color:#9bb; font-weight:500; border-bottom:1px solid #2c3e50; padding:7px 8px; }}
  td {{ padding:7px 8px; border-bottom:1px solid #1f2b38; }}
  .num {{ text-align:right; }} .mono {{ font-family:ui-monospace,SFMono-Regular,Menlo,monospace; }}
  .muted {{ color:#9bb; }} a {{ color:#7fd; }}
  .pill {{ display:inline-block; padding:2px 9px; border-radius:11px; font-size:11px; }}
  .pill.ok {{ background:#1b3a2b; color:#7fe0a8; border:1px solid #2e7d52; }}
  .pill.bad {{ background:#3a1f1f; color:#f3a; border:1px solid #7d2e2e; }}
  footer {{ color:#9bb; font-size:12px; padding:8px 0 28px; }}
</style></head>
<body>
<header>
  <h1>{data["service"]} — proof</h1>
  <p>Identity registries prove who an agent is. This page proves what this one
     <i>does</i>: live settlements, gate verdicts, refused money, and trust computed
     from verification outcomes — all reproducible from the ledger.</p>
  <nav>
    <a href="/">storefront</a><a href="/proof.json">proof (json)</a>
    <a href="/admin">admin</a><a href="/dashboard">margin</a>
    <a href="/.well-known/x402">discovery</a>
    <a href="/.well-known/agent-card.json">agent card</a><a href="/map">system map</a>
  </nav>
</header>
<div class="wrap">
  <div class="invariant">⚖ {data["invariant"]}</div>
  <div class="grid">
    <div class="stat"><div class="k">Settled revenue</div><div class="v">${s["revenue_usdc"]:.4f}</div></div>
    <div class="stat"><div class="k">Unique buyers</div><div class="v">{s["unique_buyers"]}</div></div>
    <div class="stat"><div class="k">Gate pass rate</div><div class="v">{pass_rate}</div></div>
    <div class="stat"><div class="k">Refused — not billed</div>
      <div class="v refused">${v["refused_not_billed_usd"]:.2f}</div></div>
  </div>
  <p class="muted" style="font-size:12px;margin-top:-8px">Network <code>{net}</code> ·
     pay-to <span class="mono">{data.get("pay_to") or "—"}</span> ·
     {v["runs_gated"]} runs gated · {v["shipped_verified"]} shipped verified ·
     {v["refused_runs"]} refused ({v["refused_note"]})</p>

  <div class="card"><h2>Source trust — reputation by verification</h2>
    <p class="sub">Laplace-smoothed sourcing-gate pass-rate per source. Computed from
      outcomes jim observed itself — not reviews, not ratings. Sources below the trust
      floor stop getting paid.</p>
    <table><thead><tr><th>source</th><th class="num">score</th><th class="num">pass</th>
      <th class="num">fail</th><th>last event</th></tr></thead>
    <tbody>{_trust_rows(data["trust"])}</tbody></table></div>

  <div class="card"><h2>Live settlements — the on-chain audit trail</h2>
    <p class="sub">Every paid call, with its settlement transaction. Click through to the
      block explorer.</p>
    <table><thead><tr><th>time</th><th>product</th><th>id</th><th class="num">amount</th>
      <th>buyer</th><th>tx</th><th>status</th></tr></thead>
    <tbody>{_settlement_rows(data["recent_settlements"])}</tbody></table></div>

  <div class="card"><h2>Refusals — money we declined to take</h2>
    <p class="sub">Runs the sourcing gate rejected. Nothing shipped, nothing billed — the
      verified payment is cancelled, not captured.</p>
    <table><thead><tr><th>time</th><th>product</th><th>id</th>
      <th class="num">declined</th><th></th></tr></thead>
    <tbody>{_refusal_rows(data["recent_refusals"])}</tbody></table></div>

  <footer>{data["reproduce"]} · auto-refreshes every 30s</footer>
</div>
</body></html>"""

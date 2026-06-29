"""Admin dashboard — the settlement / revenue / on-chain audit view.

Deliberately separate from :mod:`jim.dashboard` (the per-query *margin* view).
Where the margin dashboard answers "are we profitable per query?" from the
economics ledger, this one answers "who paid us, how much, and on which tx?"
from the settlement audit log (:class:`jim.store.models.PaymentReceipt`).

Surfaced three ways: ``jim-admin`` (CLI), ``GET /admin`` (HTML), and
``GET /admin/audit`` (JSON). All read-only.
"""

from __future__ import annotations

import asyncio

from jim.config import BASE_MAINNET, get_settings
from jim.store import get_store

# Block explorers, keyed by the CAIP-2 network the payment settled on.
_EXPLORERS: dict[str, str] = {
    BASE_MAINNET: "https://basescan.org",
    "eip155:84532": "https://sepolia.basescan.org",
}


def explorer_base(network: str) -> str | None:
    """Base block-explorer URL for a CAIP-2 network (None if unknown)."""
    return _EXPLORERS.get(network)


def tx_url(network: str, tx_hash: str | None) -> str | None:
    base = explorer_base(network)
    return f"{base}/tx/{tx_hash}" if base and tx_hash else None


def address_url(network: str, address: str | None) -> str | None:
    base = explorer_base(network)
    return f"{base}/address/{address}" if base and address else None


async def admin_dashboard(limit: int = 50) -> dict:
    """The admin view: settlement summary + the recent on-chain audit trail."""
    store = get_store()
    settings = get_settings()
    summary = await store.receipts_summary()
    recent = await store.recent_receipts(limit)
    # Decorate each receipt + buyer with an explorer link for the UI/CLI.
    for r in recent:
        r["tx_explorer_url"] = tx_url(r.get("network") or settings.network, r.get("tx_hash"))
        r["payer_explorer_url"] = address_url(
            r.get("network") or settings.network, r.get("payer")
        )
    for b in summary.get("top_buyers", []):
        b["explorer_url"] = address_url(settings.network, b.get("address"))
    return {
        "network": settings.network,
        "pay_to": settings.evm_address,
        "explorer": explorer_base(settings.network),
        "summary": summary,
        "recent": recent,
    }


def _short(value: str | None, head: int = 10, tail: int = 6) -> str:
    if not value:
        return "—"
    if len(value) <= head + tail + 1:
        return value
    return f"{value[:head]}…{value[-tail:]}"


def render_text(data: dict) -> str:
    s = data["summary"]
    lines = [
        "=" * 78,
        "  jim — admin dashboard (settlements · revenue · on-chain audit trail)",
        "=" * 78,
        f"  Network          : {data['network']}",
        f"  Pay-to address   : {data.get('pay_to') or '—'}",
        f"  Settlements      : {s['settlements']}  (of {s['total_receipts']} receipts)",
        f"  Settled revenue  : ${s['revenue_usdc']:.4f} USDC",
        f"  Unique buyers    : {s['unique_buyers']}",
        f"  Avg payment      : ${s['avg_payment_usdc']:.4f}",
    ]
    if s.get("by_product"):
        lines.append("-" * 78)
        lines.append("  Revenue by product")
        lines.append("-" * 78)
        for p in s["by_product"]:
            lines.append(
                f"  {p['product']:<16}{p['payments']:>4} payments   ${p['revenue_usdc']:>10.4f}"
            )
    if s.get("top_buyers"):
        lines.append("-" * 78)
        lines.append("  Top buyers")
        lines.append("-" * 78)
        for b in s["top_buyers"]:
            lines.append(
                f"  {_short(b['address'], 14, 8):<26}{b['payments']:>4} pmts   "
                f"${b['spent_usdc']:>10.4f}"
            )
    lines.append("-" * 78)
    lines.append(
        f"  {'time':<20}{'product':<13}{'id':<8}{'amount':>9}  "
        f"{'buyer':<22}{'tx'}"
    )
    lines.append("-" * 78)
    for r in data["recent"]:
        ts = (r.get("created_at") or "")[:19].replace("T", " ")
        lines.append(
            f"  {ts:<20}{(r.get('product') or '—'):<13}{(r.get('identifier') or '—'):<8}"
            f"{r.get('amount_usdc', 0.0):>9.4f}  {_short(r.get('payer')):<22}{_short(r.get('tx_hash'))}"
        )
    if not data["recent"]:
        lines.append("  (no settlements recorded yet — take a paid x402 payment first)")
    lines.append("=" * 78)
    return "\n".join(lines)


def _row_html(r: dict) -> str:
    ts = (r.get("created_at") or "")[:19].replace("T", " ")
    tx = r.get("tx_hash")
    tx_cell = (
        f'<a href="{r["tx_explorer_url"]}" target="_blank" rel="noopener">{_short(tx)}</a>'
        if r.get("tx_explorer_url")
        else _short(tx)
    )
    payer = r.get("payer")
    payer_cell = (
        f'<a href="{r["payer_explorer_url"]}" target="_blank" rel="noopener">{_short(payer)}</a>'
        if r.get("payer_explorer_url")
        else _short(payer)
    )
    ok = "ok" if r.get("success") else "bad"
    return (
        f"<tr><td class='mono muted'>{ts}</td>"
        f"<td>{r.get('product') or '—'}</td>"
        f"<td>{r.get('identifier') or '—'}</td>"
        f"<td class='num'>${r.get('amount_usdc', 0.0):.4f}</td>"
        f"<td class='mono'>{payer_cell}</td>"
        f"<td class='mono'>{tx_cell}</td>"
        f"<td><span class='pill {ok}'>{'settled' if r.get('success') else 'failed'}</span></td></tr>"
    )


def admin_html(data: dict, service_name: str = "jim") -> str:
    """Render the admin dashboard as a self-contained dark-themed HTML page."""
    s = data["summary"]
    rows = "\n".join(_row_html(r) for r in data["recent"]) or (
        "<tr><td colspan='7' class='muted'>No settlements recorded yet — "
        "take a paid x402 payment first.</td></tr>"
    )
    by_product = "".join(
        f"<tr><td>{p['product']}</td><td class='num'>{p['payments']}</td>"
        f"<td class='num'>${p['revenue_usdc']:.4f}</td></tr>"
        for p in s.get("by_product", [])
    ) or "<tr><td colspan='3' class='muted'>—</td></tr>"
    top_buyers = "".join(
        "<tr><td class='mono'>"
        + (
            f'<a href="{b["explorer_url"]}" target="_blank" rel="noopener">{_short(b["address"], 12, 8)}</a>'
            if b.get("explorer_url")
            else _short(b["address"], 12, 8)
        )
        + f"</td><td class='num'>{b['payments']}</td>"
        f"<td class='num'>${b['spent_usdc']:.4f}</td></tr>"
        for b in s.get("top_buyers", [])
    ) or "<tr><td colspan='3' class='muted'>—</td></tr>"
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>{service_name} — admin</title>
<style>
  :root {{ color-scheme: dark; }}
  body {{ font-family:-apple-system,system-ui,sans-serif; margin:0; background:#0f1419; color:#e6e6e6; }}
  header {{ padding:22px 28px; border-bottom:1px solid #233; }}
  header h1 {{ margin:0; font-size:21px; }}
  nav a {{ color:#7fd; text-decoration:none; margin-right:14px; font-size:13px; }}
  .wrap {{ max-width:1080px; margin:0 auto; padding:24px 28px; }}
  .grid {{ display:grid; grid-template-columns:repeat(4,1fr); gap:14px; margin-bottom:20px; }}
  .stat {{ background:#1b2733; border:1px solid #2c3e50; border-radius:12px; padding:16px; }}
  .stat .k {{ color:#9bb; font-size:12px; }} .stat .v {{ font-size:24px; margin-top:6px; font-weight:600; }}
  .cols {{ display:grid; grid-template-columns:1fr 1fr; gap:18px; margin-bottom:20px; }}
  .card {{ background:#1b2733; border:1px solid #2c3e50; border-radius:12px; padding:16px; overflow-x:auto; }}
  .card h2 {{ margin:0 0 12px; font-size:14px; color:#cde; }}
  table {{ width:100%; border-collapse:collapse; font-size:13px; }}
  th {{ text-align:left; color:#9bb; font-weight:500; border-bottom:1px solid #2c3e50; padding:7px 8px; }}
  td {{ padding:7px 8px; border-bottom:1px solid #1f2b38; }}
  .num {{ text-align:right; }} .mono {{ font-family:ui-monospace,SFMono-Regular,Menlo,monospace; }}
  .muted {{ color:#9bb; }} a {{ color:#7fd; }}
  .pill {{ display:inline-block; padding:2px 9px; border-radius:11px; font-size:11px; }}
  .pill.ok {{ background:#1b3a2b; color:#7fe0a8; border:1px solid #2e7d52; }}
  .pill.bad {{ background:#3a1f1f; color:#f3a; border:1px solid #7d2e2e; }}
</style></head>
<body>
<header>
  <h1>{service_name} — admin · settlements &amp; on-chain audit</h1>
  <nav>
    <a href="/">storefront</a><a href="/dashboard">margin dashboard</a>
    <a href="/admin/audit">audit (json)</a><a href="/map">system map</a>
  </nav>
</header>
<div class="wrap">
  <div class="grid">
    <div class="stat"><div class="k">Settled revenue</div><div class="v">${s['revenue_usdc']:.4f}</div></div>
    <div class="stat"><div class="k">Settlements</div><div class="v">{s['settlements']}</div></div>
    <div class="stat"><div class="k">Unique buyers</div><div class="v">{s['unique_buyers']}</div></div>
    <div class="stat"><div class="k">Avg payment</div><div class="v">${s['avg_payment_usdc']:.4f}</div></div>
  </div>
  <p class="muted" style="font-size:12px;margin-top:-8px">Network <code>{data['network']}</code> ·
     pay-to <span class="mono">{data.get('pay_to') or '—'}</span></p>
  <div class="cols">
    <div class="card"><h2>Revenue by product</h2>
      <table><thead><tr><th>product</th><th class="num">payments</th><th class="num">revenue</th></tr></thead>
      <tbody>{by_product}</tbody></table></div>
    <div class="card"><h2>Top buyers</h2>
      <table><thead><tr><th>address</th><th class="num">payments</th><th class="num">spent</th></tr></thead>
      <tbody>{top_buyers}</tbody></table></div>
  </div>
  <div class="card"><h2>On-chain audit trail</h2>
    <table><thead><tr><th>time</th><th>product</th><th>id</th><th class="num">amount</th>
      <th>buyer</th><th>tx</th><th>status</th></tr></thead>
    <tbody>{rows}</tbody></table>
  </div>
</div>
</body></html>"""


def main() -> int:
    data = asyncio.run(admin_dashboard())
    print(render_text(data))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

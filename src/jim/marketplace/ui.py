"""The thin human UI (Phase 5) — a storefront that pays via x402 under the hood.

Two halves:

  - :func:`storefront_html` — a dependency-free page (vanilla fetch, no build
    step) where a person picks a product + identifier and gets a cited memo back.
  - :func:`checkout` — the backend the page calls. When a wallet is funded and
    ``UI_SETTLE_VIA_X402`` is on (or ``settle=true`` is forced), it proves the rail
    end-to-end by having jim **buy its own endpoint** over x402 — so the visitor
    needs no wallet and a real on-chain settlement still happens. Otherwise it
    runs the engine directly and labels the result a *preview*.

The self-pay is the honest "pays under the hood" demo: production would swap jim's
own wallet for a browser wallet (an x402 browser extension / WalletConnect), but
the settlement path is identical. Either way the **same** sourcing gate decides
what may ship — the UI is just another caller of ``run_research``.
"""

from __future__ import annotations

from html import escape

from jim.config import Settings, get_settings
from jim.marketplace.catalog import build_catalog, listing_for


def _attr(value: str) -> str:
    """Escape a string for safe use inside a double-quoted HTML attribute."""
    return escape(value, quote=True)


async def checkout(
    *,
    product: str,
    identifier: str,
    mode: str = "human",
    settle: bool | None = None,
) -> dict:
    """Fulfil one research request for the UI; settle over x402 when funded."""
    settings = get_settings()
    listing = listing_for(product)
    if listing is None:
        return {"ok": False, "error": f"Unknown product {product!r}."}

    want_settle = settings.ui_settle_via_x402 if settle is None else settle
    can_settle = bool(settings.evm_private_key) and want_settle

    if can_settle:
        return await _checkout_via_x402(
            settings, listing.path, listing.identifier_param, identifier, mode
        )
    return await _checkout_direct(product, identifier, mode, listing.price_usd)


async def _checkout_direct(product: str, identifier: str, mode: str, price_usd: float) -> dict:
    """Run the engine in-process — always works, no wallet/network needed."""
    from jim.research.engine import run_research
    from jim.research.schemas import ResearchResponse

    result = await run_research(identifier, product=product, mode=mode)
    if result.status == "error":
        return {"ok": False, "error": result.error, "settled_via": "direct"}
    if result.status != "ok":
        # Same refusal the paid routes make: never show unverified output, even
        # in a free preview. (Paid callers additionally keep their money.)
        gate = result.gate
        coverage = f"{gate.n_covered}/{gate.n_figures} figures verified" if gate else "no gate run"
        return {
            "ok": False,
            "rejected": True,
            "billed": False,
            "settled_via": "direct",
            "error": (
                f"jim's verification gates rejected this run after "
                f"{result.attempts} attempt(s) ({coverage}) — nothing shipped. "
                "A fresh attempt may succeed."
            ),
        }
    return {
        "ok": True,
        "paid": False,
        "settled_via": "direct",
        "tx_hash": None,
        "price_usd": price_usd,
        "result": ResearchResponse.from_result(result).model_dump(),
    }


async def _checkout_via_x402(
    settings: Settings, path: str, param: str, identifier: str, mode: str
) -> dict:
    """Pay our own endpoint over x402 — proves settlement without a visitor wallet."""
    from jim.buyer import pay
    from jim.research.products import get_product

    url = f"{settings.public_url}{path}?{param}={identifier}&mode={mode}"
    resp = await pay(url, method="GET")
    if resp.status_code != 200:
        # A 502 with our refusal shape means the *research* was rejected — the
        # verified payment was cancelled, so the buyer was not billed.
        refusal = _refusal_detail(resp)
        if refusal is not None:
            src = refusal.get("sourcing") or {}
            coverage = (
                f"{src.get('figures_covered', 0)}/{src.get('figures_checked', 0)} "
                "figures verified"
            )
            return {
                "ok": False,
                "rejected": True,
                "billed": False,
                "settled_via": "x402",
                "error": (
                    f"jim's verification gates rejected this run ({coverage}) — "
                    "nothing shipped, and the payment was NOT settled. "
                    "A fresh attempt may succeed."
                ),
            }
        # Otherwise the settlement itself failed. The seller's 402 body is empty
        # on a failed settlement; the real reason (insufficient funds, facilitator
        # auth, etc.) rides in the PAYMENT-RESPONSE header instead, which `pay()`
        # already decoded into `resp.settlement`.
        detail = ""
        if resp.settlement:
            reason = resp.settlement.get("error_reason")
            message = resp.settlement.get("error_message")
            detail = f" — {reason}: {message}" if reason or message else ""
        return {"ok": False, "error": f"HTTP {resp.status_code}{detail or f': {resp.text[:200]}'}"}
    return {
        "ok": True,
        "paid": resp.paid,
        "settled_via": "x402",
        "tx_hash": resp.tx_hash,
        "price_usd": resp.cost_in_usd or get_product(path.rsplit("/", 1)[-1]).price_out_usd,
        "result": resp.json(),
    }


def _refusal_detail(resp) -> dict | None:
    """The structured refusal a research route returns for a gate-rejected run
    (see ``_deliver_or_refuse`` in the seller), or None for other failures."""
    try:
        detail = resp.json().get("detail")
    except Exception:
        return None
    if isinstance(detail, dict) and detail.get("status") == "rejected":
        return detail
    return None


def storefront_html(settings: Settings | None = None) -> str:
    s = settings or get_settings()
    listings = build_catalog()
    options = "\n".join(
        f'<option value="{listing.product}" data-param="{listing.identifier_param}" '
        f'data-example="{listing.identifier_example}" data-path="{listing.path}" '
        f'data-price="{listing.price_usd:.2f}" data-title="{listing.title}" '
        f'data-paid="{"1" if listing.paid_upstream else "0"}" '
        f'data-upstream="{listing.upstream}" '
        f'data-desc="{_attr(listing.description)}">{listing.title} — '
        f"${listing.price_usd:.2f}</option>"
        for listing in listings
    )
    net = "Base mainnet" if s.is_mainnet else "Base Sepolia (testnet)"
    wallet_kind = "real USDC" if s.is_mainnet else "testnet USDC"
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>{s.service_name} — cited financial research</title>
<style>
  :root {{ color-scheme: dark; --bg:#0f1419; --panel:#1b2733; --line:#2c3e50; --muted:#9bb;
           --accent:#1e88e5; --accent2:#22a06b; --ink:#e6e6e6; }}
  * {{ box-sizing:border-box; }}
  body {{ font-family:-apple-system,system-ui,sans-serif; margin:0; background:var(--bg); color:var(--ink); }}
  header {{ padding:22px 28px; border-bottom:1px solid #233; }}
  header h1 {{ margin:0; font-size:22px; letter-spacing:.2px; }}
  header p {{ margin:6px 0 0; color:var(--muted); font-size:13px; max-width:680px; }}
  nav {{ margin-top:12px; }}
  nav a {{ color:#7fd; text-decoration:none; margin-right:14px; font-size:13px; }}
  .wrap {{ max-width:900px; margin:0 auto; padding:24px 28px; }}
  .card {{ background:var(--panel); border:1px solid var(--line); border-radius:12px; padding:18px; margin-bottom:18px; }}
  label {{ display:block; font-size:12px; color:var(--muted); margin:10px 0 4px; }}
  select, input {{ width:100%; padding:10px; border-radius:8px; border:1px solid var(--line);
                   background:var(--bg); color:var(--ink); font-size:14px; }}
  .actions {{ display:flex; gap:12px; margin-top:18px; flex-wrap:wrap; }}
  button {{ padding:11px 18px; border:0; border-radius:8px; font-size:14px; cursor:pointer; color:#fff; }}
  button.primary {{ background:var(--accent); }}
  button.wallet {{ background:var(--accent2); }}
  button.ghost {{ background:transparent; border:1px solid var(--line); color:var(--ink); }}
  button:disabled {{ opacity:.5; cursor:wait; }}
  .row {{ display:flex; gap:14px; }} .row > div {{ flex:1; }}
  .detail {{ background:var(--bg); border:1px solid var(--line); border-radius:10px; padding:13px 15px; margin-top:14px; font-size:13px; color:#cde; }}
  .detail .meta {{ color:var(--muted); margin-top:6px; font-size:12px; }}
  .memo {{ white-space:pre-wrap; line-height:1.55; }}
  .pill {{ display:inline-block; padding:2px 9px; border-radius:11px; font-size:12px; margin-right:6px; }}
  .ok {{ background:#1b3a2b; color:#7fe0a8; border:1px solid #2e7d52; }}
  .bad {{ background:#3a1f1f; color:#f3a; border:1px solid #7d2e2e; }}
  .tag {{ background:#10202b; color:#8cf; border:1px solid #1f3a4d; }}
  .muted {{ color:var(--muted); font-size:12px; }}
  .cite {{ font-size:12px; color:#cde; border-top:1px dashed var(--line); padding-top:8px; margin-top:12px; }}
  code {{ background:var(--bg); padding:1px 5px; border-radius:4px; }}
</style>
</head>
<body>
<header>
  <h1>{s.service_name} — cited financial research</h1>
  <p>{s.service_description}</p>
  <nav>
    <a href="/proof">proof</a><a href="/catalog">catalog</a><a href="/pricing">pricing</a>
    <a href="/map">system map</a><a href="/dashboard">margin</a>
    <a href="/admin">admin</a><a href="/.well-known/x402">discovery</a>
  </nav>
</header>
<div class="wrap">
  <div class="card">
    <div class="row">
      <div>
        <label for="product">Product</label>
        <select id="product">{options}</select>
      </div>
      <div>
        <label for="mode">Mode</label>
        <select id="mode">
          <option value="human">human — narrative</option>
          <option value="agent">agent — terse, metric-dense</option>
        </select>
      </div>
    </div>
    <label for="identifier">Identifier</label>
    <input id="identifier" placeholder="AAPL"/>
    <div id="detail" class="detail"></div>
    <div class="actions">
      <button id="preview" class="primary">Preview — free</button>
      <button id="wallet" class="wallet">Pay with wallet</button>
    </div>
    <p class="muted" style="margin-top:14px">
      Network <code>{net}</code>. <b>Preview</b> runs the same sourcing-gated engine and
      renders the memo here, unpaid. <b>Pay with wallet</b> opens the x402 checkout — connect
      MetaMask or Coinbase Wallet and settle in {wallet_kind} for the real, billed report.
    </p>
  </div>
  <div id="out"></div>
</div>
<script>
const productSel = document.getElementById('product');
const idInput = document.getElementById('identifier');
const modeSel = document.getElementById('mode');
const detail = document.getElementById('detail');

function opt() {{ return productSel.selectedOptions[0]; }}
function currentId() {{ return (idInput.value || idInput.placeholder).trim(); }}

function syncDetail() {{
  const o = opt();
  idInput.placeholder = o.dataset.example || 'AAPL';
  const paid = o.dataset.paid === '1'
    ? '<span class="pill tag">jim pays upstream</span>' : '';
  detail.innerHTML = '<b>' + o.dataset.title + '</b> · <span class="muted">$' + o.dataset.price + '/report</span> ' + paid +
    '<div style="margin-top:6px">' + o.dataset.desc + '</div>' +
    '<div class="meta">Upstream: ' + o.dataset.upstream + '</div>';
}}
productSel.addEventListener('change', syncDetail); syncDetail();

document.getElementById('preview').addEventListener('click', async () => {{
  const btn = document.getElementById('preview');
  const out = document.getElementById('out');
  const product = productSel.value, identifier = currentId(), mode = modeSel.value;
  btn.disabled = true;
  out.innerHTML = '<div class="card muted">Researching ' + identifier + ' …</div>';
  try {{
    const r = await fetch('/ui/checkout', {{
      method: 'POST', headers: {{'Content-Type':'application/json'}},
      body: JSON.stringify({{product, identifier, mode}})
    }});
    out.innerHTML = render(await r.json());
  }} catch (e) {{
    out.innerHTML = '<div class="card"><span class="pill bad">error</span> ' + e + '</div>';
  }} finally {{ btn.disabled = false; }}
}});

document.getElementById('wallet').addEventListener('click', () => {{
  const o = opt();
  const url = o.dataset.path + '?' + encodeURIComponent(o.dataset.param) + '=' +
    encodeURIComponent(currentId()) + '&mode=' + encodeURIComponent(modeSel.value);
  // Navigating a browser to the paid route serves x402's wallet paywall, which
  // connects MetaMask / Coinbase Wallet, signs EIP-3009, and settles on-chain.
  window.open(url, '_blank', 'noopener');
}});

function render(data) {{
  if (!data.ok) {{
    // A gate refusal is jim working as designed: nothing shipped, nothing billed.
    if (data.rejected) return '<div class="card"><span class="pill bad">research rejected</span> ' +
      '<span class="pill tag">not billed</span><div style="margin-top:8px">' + (data.error||'') + '</div></div>';
    return '<div class="card"><span class="pill bad">error</span> ' + (data.error||'failed') + '</div>';
  }}
  const res = data.result || {{}};
  const src = res.sourcing || {{}};
  const cost = res.cost || {{}};
  const paid = data.paid ? '<span class="pill ok">paid · x402</span>' : '<span class="pill ok">preview · free</span>';
  const verified = '<span class="pill ok">gate-verified</span>';
  const tx = data.tx_hash ? '<span class="muted">tx ' + data.tx_hash.slice(0,12) + '…</span>' : '';
  const cites = (res.citations||[]).slice(0,8).map(c =>
    '[' + c.id + '] ' + c.label + ' = ' + c.value + ' ' + (c.unit||'')).join(' · ');
  return '<div class="card">' +
    '<div>' + paid + verified + ' <b>' + (res.company || res.ticker || '') + '</b> ' + tx + '</div>' +
    '<div class="memo" style="margin-top:12px">' + (res.memo || '(no memo)') + '</div>' +
    '<div class="cite">Sourcing: ' + (src.passed ? 'PASS' : 'FAIL') + ' — ' +
      (src.figures_covered||0) + '/' + (src.figures_checked||0) + ' figures · ' +
      'price $' + (cost.price_out_usd||0) + ' · margin $' + (cost.margin_usd||0) + '</div>' +
    '<div class="cite">' + cites + '</div>' +
    '<div class="cite muted">' + (res.disclaimer || '') + '</div>' +
  '</div>';
}}
</script>
</body>
</html>"""

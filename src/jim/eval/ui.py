"""The eval results dashboard — ``jim-eval ui``.

A small FastAPI app over the persisted run documents (:mod:`jim.eval.storage`):
trend charts for the headline metrics (pass rate, quality, cost, latency), the
run history table, per-run drill-down to every case (memos, violations, judge
issues), and a run-vs-run comparison view powered by the same
:mod:`jim.eval.compare` logic the CLI uses — so the dashboard and CI can never
disagree about what "regressed" means.

Same construction as jim's other pages (storefront/proof/admin): one
self-contained HTML document, inline CSS + vanilla JS, no CDN or build step —
the dashboard works fully offline, like the suites it displays. Charts are
hand-rolled inline SVG for the same reason.

    GET /                → dashboard (HTML)
    GET /api/runs        → run summaries + baseline (the trend/table feed)
    GET /api/runs/{id}   → one full run document
    GET /api/compare     → ?base=X&cand=Y regression diff
"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse


def build_app() -> FastAPI:
    from jim.eval import storage
    from jim.eval.compare import compare_runs

    app = FastAPI(title="jim — evals", docs_url=None, redoc_url=None)

    @app.get("/api/runs")
    async def api_runs() -> dict:
        return {"runs": storage.list_runs(), "baseline": storage.get_baseline()}

    @app.get("/api/runs/{run_id}")
    async def api_run(run_id: str) -> dict:
        try:
            return storage.load_run(run_id)
        except FileNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e))

    @app.get("/api/compare")
    async def api_compare(base: str, cand: str = "latest") -> dict:
        try:
            return compare_runs(storage.load_run(base), storage.load_run(cand))
        except FileNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e))

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return _HTML

    return app


_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>jim — evals</title>
<style>
  :root { color-scheme: dark; }
  body { font-family:-apple-system,system-ui,sans-serif; margin:0; background:#0f1419; color:#e6e6e6; }
  header { padding:22px 28px; border-bottom:1px solid #233; }
  header h1 { margin:0; font-size:21px; }
  header p { margin:8px 0 0; color:#9bb; font-size:13px; max-width:820px; }
  .wrap { max-width:1180px; margin:0 auto; padding:24px 28px; }
  .grid { display:grid; grid-template-columns:repeat(4,1fr); gap:14px; margin-bottom:20px; }
  .stat { background:#1b2733; border:1px solid #2c3e50; border-radius:12px; padding:16px; }
  .stat .k { color:#9bb; font-size:12px; }
  .stat .v { font-size:24px; margin-top:6px; font-weight:600; }
  .stat .d { font-size:12px; margin-top:4px; color:#9bb; }
  .stat .d.up { color:#7fe0a8; } .stat .d.down { color:#f3a; }
  .charts { display:grid; grid-template-columns:repeat(2,1fr); gap:14px; margin-bottom:20px; }
  .card { background:#1b2733; border:1px solid #2c3e50; border-radius:12px; padding:16px; margin-bottom:18px; overflow-x:auto; }
  .card h2 { margin:0 0 4px; font-size:14px; color:#cde; }
  .card p.sub { margin:0 0 12px; color:#9bb; font-size:12px; }
  table { width:100%; border-collapse:collapse; font-size:13px; }
  th { text-align:left; color:#9bb; font-weight:500; border-bottom:1px solid #2c3e50; padding:7px 8px; }
  td { padding:7px 8px; border-bottom:1px solid #1f2b38; vertical-align:top; }
  tr.clickable { cursor:pointer; } tr.clickable:hover td { background:#20303f; }
  tr.selected td { background:#24384a; }
  .num { text-align:right; } .mono { font-family:ui-monospace,SFMono-Regular,Menlo,monospace; }
  .muted { color:#9bb; } a { color:#7fd; }
  .pill { display:inline-block; padding:2px 9px; border-radius:11px; font-size:11px; white-space:nowrap; }
  .pill.ok { background:#1b3a2b; color:#7fe0a8; border:1px solid #2e7d52; }
  .pill.bad { background:#3a1f1f; color:#f3a; border:1px solid #7d2e2e; }
  .pill.flat { background:#2c3138; color:#9bb; border:1px solid #445; }
  .pill.base { background:#10202b; color:#8cf; border:1px solid #1f3a4d; }
  details { margin:2px 0; }
  details summary { cursor:pointer; color:#7fd; font-size:12px; }
  pre { background:#10161d; border:1px solid #233; border-radius:8px; padding:10px;
        font-size:11.5px; white-space:pre-wrap; word-break:break-word; max-height:340px; overflow:auto; }
  select, button { background:#1b2733; color:#e6e6e6; border:1px solid #2c3e50; border-radius:8px;
                   padding:6px 10px; font-size:13px; }
  button { cursor:pointer; } button:hover { border-color:#7fd; }
  svg text { font-family:-apple-system,system-ui,sans-serif; }
  .empty { color:#9bb; font-size:13px; padding:14px 0; }
  footer { color:#9bb; font-size:12px; padding:8px 0 28px; }
</style></head>
<body>
<header>
  <h1>jim — evals</h1>
  <p>Every <span class="mono">jim-eval run</span> lands here: deterministic suites (gate,
     guards, engine scenarios) that must stay at 100%, and the live suite's quality /
     cost / latency trends. The regression verdicts use the exact thresholds CI uses.</p>
</header>
<div class="wrap">
  <div id="stats" class="grid"></div>
  <div id="charts" class="charts"></div>

  <div class="card">
    <h2>Run history</h2>
    <p class="sub">Newest first. Click a run to inspect every case; ★ marks the baseline.</p>
    <table><thead><tr>
      <th>run</th><th>label</th><th>commit</th><th class="num">offline</th>
      <th class="num">live ok</th><th class="num">gate rate</th><th class="num">rubric</th>
      <th class="num">$ / live run</th><th class="num">p95 ms</th><th class="num">eval cost $</th>
    </tr></thead><tbody id="runs"></tbody></table>
  </div>

  <div class="card">
    <h2>Compare two runs</h2>
    <p class="sub">Offline suites diff exactly (any newly-failing case = regression);
       live metrics use the configured thresholds.</p>
    <div style="display:flex; gap:10px; align-items:center; flex-wrap:wrap;">
      <label class="muted">base</label><select id="cmp-base"></select>
      <label class="muted">candidate</label><select id="cmp-cand"></select>
      <button onclick="runCompare()">compare</button>
      <span id="cmp-verdict"></span>
    </div>
    <div id="cmp-out"></div>
  </div>

  <div id="detail"></div>
  <footer>jim eval dashboard · reads EVAL_RUNS_DIR · refresh the page after new runs</footer>
</div>
<script>
"use strict";
let RUNS = [], BASELINE = null;

const esc = s => String(s ?? "").replace(/[&<>"']/g,
  c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const fmt = (v, digits=4) => (v === null || v === undefined) ? "—"
  : (typeof v === "number" ? v.toFixed(digits) : esc(v));
const pct = v => (v === null || v === undefined) ? "—" : (v * 100).toFixed(1) + "%";

async function boot() {
  const data = await (await fetch("api/runs")).json();
  RUNS = data.runs; BASELINE = data.baseline;
  renderStats(); renderCharts(); renderRuns(); fillCompare();
  if (RUNS.length) showRun(RUNS[RUNS.length - 1].run_id, { scroll: false });
}

function latestWith(key) {
  for (let i = RUNS.length - 1; i >= 0; i--)
    if (RUNS[i].summary && RUNS[i].summary[key] !== undefined && RUNS[i].summary[key] !== null)
      return RUNS[i];
  return null;
}

function delta(key, better="up", digits=4) {
  const withKey = RUNS.filter(r => r.summary && r.summary[key] !== null && r.summary[key] !== undefined);
  if (withKey.length < 2) return "";
  const cur = withKey[withKey.length-1].summary[key], prev = withKey[withKey.length-2].summary[key];
  const d = cur - prev;
  if (Math.abs(d) < 1e-9) return '<div class="d">— vs prior</div>';
  const good = (better === "up") === (d > 0);
  const arrow = d > 0 ? "▲" : "▼";
  return `<div class="d ${good ? "up" : "down"}">${arrow} ${Math.abs(d).toFixed(digits)} vs prior</div>`;
}

function renderStats() {
  const el = document.getElementById("stats");
  if (!RUNS.length) { el.innerHTML = '<div class="empty">No runs yet — run <span class="mono">jim-eval run</span>.</div>'; return; }
  const off = latestWith("offline_pass_rate"), live = latestWith("live_ok_rate");
  const cost = latestWith("live_mean_cost_usd"), lat = latestWith("live_latency_p95_ms");
  el.innerHTML = `
    <div class="stat"><div class="k">Offline pass rate (must be 100%)</div>
      <div class="v">${off ? pct(off.summary.offline_pass_rate) : "—"}</div>
      ${delta("offline_pass_rate")}</div>
    <div class="stat"><div class="k">Live ok rate</div>
      <div class="v">${live ? pct(live.summary.live_ok_rate) : "—"}</div>
      ${delta("live_ok_rate")}</div>
    <div class="stat"><div class="k">Live rubric (quality 0–1)</div>
      <div class="v">${live ? fmt(live.summary.live_mean_rubric, 3) : "—"}</div>
      ${delta("live_mean_rubric", "up", 3)}</div>
    <div class="stat"><div class="k">Cost per live run</div>
      <div class="v">${cost ? "$" + fmt(cost.summary.live_mean_cost_usd, 4) : "—"}</div>
      ${delta("live_mean_cost_usd", "down")}</div>`;
}

function chart(title, series, opts={}) {
  // series: [{label, color, points: [{x: runIndex, y}]}]
  const W = 540, H = 150, PL = 46, PR = 10, PT = 18, PB = 22;
  const all = series.flatMap(s => s.points.map(p => p.y)).filter(y => y !== null);
  if (!all.length) return `<div class="card"><h2>${esc(title)}</h2><div class="empty">no data yet</div></div>`;
  let lo = Math.min(...all), hi = Math.max(...all);
  if (opts.zeroBase) lo = Math.min(lo, 0);
  if (hi === lo) { hi += (hi || 1) * 0.1; lo -= (lo || 1) * 0.1; }
  const n = Math.max(RUNS.length - 1, 1);
  const X = i => PL + (W - PL - PR) * (RUNS.length === 1 ? 0.5 : i / n);
  const Y = v => PT + (H - PT - PB) * (1 - (v - lo) / (hi - lo));
  let svg = `<svg viewBox="0 0 ${W} ${H}" width="100%" preserveAspectRatio="xMidYMid meet">`;
  for (const frac of [0, 0.5, 1]) {
    const v = lo + (hi - lo) * frac, y = Y(v);
    svg += `<line x1="${PL}" y1="${y}" x2="${W-PR}" y2="${y}" stroke="#233" stroke-width="1"/>
      <text x="${PL-6}" y="${y+4}" text-anchor="end" font-size="10" fill="#9bb">${(opts.fmt || (x=>x.toFixed(2)))(v)}</text>`;
  }
  for (const s of series) {
    const pts = s.points.filter(p => p.y !== null);
    if (pts.length > 1)
      svg += `<polyline fill="none" stroke="${s.color}" stroke-width="2" points="${pts.map(p => X(p.x)+","+Y(p.y)).join(" ")}"/>`;
    for (const p of pts)
      svg += `<circle cx="${X(p.x)}" cy="${Y(p.y)}" r="3" fill="${s.color}"><title>${esc(RUNS[p.x].run_id)}: ${p.y}</title></circle>`;
  }
  const legend = series.map(s => `<tspan fill="${s.color}">● ${esc(s.label)}</tspan>`).join("  ");
  svg += `<text x="${PL}" y="${H-6}" font-size="10" fill="#9bb">oldest → newest</text>
    <text x="${W-PR}" y="12" text-anchor="end" font-size="10">${legend}</text></svg>`;
  return `<div class="card"><h2>${esc(title)}</h2>${svg}</div>`;
}

function seriesOf(key) {
  return RUNS.map((r, i) => ({ x: i, y: (r.summary && r.summary[key] !== undefined) ? r.summary[key] : null }));
}

function renderCharts() {
  const el = document.getElementById("charts");
  if (!RUNS.length) { el.innerHTML = ""; return; }
  el.innerHTML =
    chart("Pass rates", [
      { label: "offline", color: "#7fe0a8", points: seriesOf("offline_pass_rate") },
      { label: "live ok", color: "#7fd", points: seriesOf("live_ok_rate") },
      { label: "live gate", color: "#c9f", points: seriesOf("live_gate_pass_rate") },
    ], { fmt: v => (v*100).toFixed(0) + "%" }) +
    chart("Quality (rubric / faithfulness)", [
      { label: "rubric", color: "#7fd", points: seriesOf("live_mean_rubric") },
      { label: "faithfulness", color: "#fc7", points: seriesOf("live_mean_faithfulness") },
    ]) +
    chart("Cost per live run ($)", [
      { label: "$ / run", color: "#f3a", points: seriesOf("live_mean_cost_usd") },
    ], { zeroBase: true, fmt: v => "$" + v.toFixed(3) }) +
    chart("Live latency (ms)", [
      { label: "p95", color: "#fc7", points: seriesOf("live_latency_p95_ms") },
      { label: "p50", color: "#7fd", points: seriesOf("live_latency_p50_ms") },
    ], { zeroBase: true, fmt: v => v.toFixed(0) });
}

function renderRuns() {
  const rows = [...RUNS].reverse().map(r => {
    const s = r.summary || {};
    const star = r.run_id === BASELINE ? " <span class='pill base'>★ baseline</span>" : "";
    return `<tr class="clickable" id="row-${esc(r.run_id)}" onclick="showRun('${esc(r.run_id)}')">
      <td class="mono">${esc(r.run_id)}${star}</td>
      <td>${esc(r.label || "")}</td>
      <td class="mono muted">${esc((r.git||{}).sha || "—")}</td>
      <td class="num">${pct(s.offline_pass_rate)}</td>
      <td class="num">${pct(s.live_ok_rate)}</td>
      <td class="num">${pct(s.live_gate_pass_rate)}</td>
      <td class="num">${fmt(s.live_mean_rubric, 3)}</td>
      <td class="num">${s.live_mean_cost_usd != null ? "$" + fmt(s.live_mean_cost_usd) : "—"}</td>
      <td class="num">${fmt(s.live_latency_p95_ms, 0)}</td>
      <td class="num">${s.total_cost_usd != null ? "$" + fmt(s.total_cost_usd) : "—"}</td></tr>`;
  });
  document.getElementById("runs").innerHTML = rows.join("") ||
    `<tr><td colspan="10" class="muted">No runs yet.</td></tr>`;
}

function caseRow(c) {
  const status = c.passed ? '<span class="pill ok">pass</span>' : '<span class="pill bad">fail</span>';
  const d = c.details || {};
  const bits = [];
  if (d.variant) bits.push(`${esc(d.ticker)} · ${esc(d.variant)}`);
  if (d.status) bits.push(`status=${esc(d.status)}`);
  if (d.attempts) bits.push(`attempts=${d.attempts}`);
  if (c.error) bits.push(`<span style="color:#f3a">${esc(c.error)}</span>`);
  const payload = esc(JSON.stringify({ ...d, error: c.error || undefined }, null, 2));
  return `<tr>
    <td class="mono">${esc(c.name)}</td><td>${status}</td>
    <td class="num">${c.score != null ? fmt(c.score, 3) : "—"}</td>
    <td class="num">${fmt(c.latency_ms, 1)}</td>
    <td class="num">${c.cost_usd ? "$" + fmt(c.cost_usd) : "—"}</td>
    <td>${bits.join(" · ")}<details><summary>details</summary><pre>${payload}</pre></details></td></tr>`;
}

async function showRun(runId, opts = {}) {
  document.querySelectorAll("#runs tr").forEach(tr => tr.classList.remove("selected"));
  const row = document.getElementById("row-" + runId);
  if (row) row.classList.add("selected");
  const run = await (await fetch("api/runs/" + encodeURIComponent(runId))).json();
  const el = document.getElementById("detail");
  const cfg = run.config || {};
  let html = `<div class="card"><h2>Run ${esc(run.run_id)}${run.label ? " — " + esc(run.label) : ""}</h2>
    <p class="sub">commit ${esc((run.git||{}).sha || "—")} on ${esc((run.git||{}).branch || "—")}
      · ${esc(run.started_at || "")} · ${run.duration_seconds}s
      · model ${esc(cfg.research_model || "—")} · judge ${esc(cfg.judge_model || "—")}
      · key ${cfg.has_anthropic_key ? "set" : "unset"}</p>`;
  for (const [name, block] of Object.entries(run.suites || {})) {
    const a = block.aggregate;
    html += `<h2 style="margin-top:14px">${esc(name)} — ${a.passed}/${a.cases}
        (${a.pass_rate != null ? pct(a.pass_rate) : "—"})</h2>
      <p class="sub">p50 ${a.latency_p50_ms}ms · p95 ${a.latency_p95_ms}ms
        · cost $${fmt(a.total_cost_usd)}${a.mean_score != null ? " · mean score " + fmt(a.mean_score, 3) : ""}${
        a.gate_pass_rate != null ? " · gate rate " + pct(a.gate_pass_rate) : ""}</p>`;
    if (name === "live" && block.variants) {
      html += `<table><thead><tr><th>variant</th><th class="num">ok</th><th class="num">gate</th>
        <th class="num">rubric</th><th class="num">$ / run</th><th class="num">p95 ms</th></tr></thead><tbody>`;
      for (const [v, agg] of Object.entries(block.variants))
        html += `<tr><td>${esc(v)}</td><td class="num">${pct(agg.pass_rate)}</td>
          <td class="num">${pct(agg.gate_pass_rate)}</td><td class="num">${fmt(agg.mean_score, 3)}</td>
          <td class="num">$${fmt(agg.mean_cost_usd)}</td><td class="num">${fmt(agg.latency_p95_ms, 0)}</td></tr>`;
      html += `</tbody></table>`;
    }
    const failed = block.cases.filter(c => !c.passed), passed = block.cases.filter(c => c.passed);
    html += `<table><thead><tr><th>case</th><th></th><th class="num">score</th>
      <th class="num">ms</th><th class="num">cost</th><th>notes</th></tr></thead>
      <tbody>${[...failed, ...passed].map(caseRow).join("")}</tbody></table>`;
  }
  html += `</div>`;
  el.innerHTML = html;
  if (opts.scroll !== false) el.scrollIntoView({ behavior: "smooth", block: "start" });
}

function fillCompare() {
  const ids = RUNS.map(r => r.run_id);
  const opts = sel => ids.map(i =>
    `<option value="${esc(i)}" ${i === sel ? "selected" : ""}>${esc(i)}${i === BASELINE ? " ★" : ""}</option>`).join("");
  document.getElementById("cmp-base").innerHTML = opts(BASELINE || ids[0]);
  document.getElementById("cmp-cand").innerHTML = opts(ids[ids.length - 1]);
}

async function runCompare() {
  const base = document.getElementById("cmp-base").value;
  const cand = document.getElementById("cmp-cand").value;
  if (!base || !cand) return;
  const cmp = await (await fetch(`api/compare?base=${encodeURIComponent(base)}&cand=${encodeURIComponent(cand)}`)).json();
  const cls = { regressed: "bad", improved: "ok", flat: "flat" }[cmp.verdict] || "flat";
  document.getElementById("cmp-verdict").innerHTML = `<span class="pill ${cls}">${esc(cmp.verdict)}</span>`;
  let html = "";
  const off = cmp.offline || { suites: {} };
  html += `<table><thead><tr><th>offline suite</th><th class="num">base</th><th class="num">candidate</th><th>changes</th></tr></thead><tbody>`;
  for (const [suite, row] of Object.entries(off.suites)) {
    const changes = [
      ...row.newly_failing.map(n => `<span class="pill bad">✗ ${esc(n)}</span>`),
      ...row.fixed.map(n => `<span class="pill ok">✓ ${esc(n)}</span>`),
    ].join(" ") || '<span class="muted">no case changes</span>';
    html += `<tr><td>${esc(suite)}</td><td class="num">${pct(row.base_pass_rate)}</td>
      <td class="num">${pct(row.cand_pass_rate)}</td><td>${changes}</td></tr>`;
  }
  html += `</tbody></table>`;
  if (cmp.live) {
    html += `<table style="margin-top:10px"><thead><tr><th>live metric</th><th class="num">base</th>
      <th class="num">candidate</th><th class="num">Δ</th><th>verdict</th></tr></thead><tbody>`;
    for (const row of cmp.live.checks) {
      const cls2 = { regressed: "bad", improved: "ok", flat: "flat", "n/a": "flat" }[row.status];
      html += `<tr><td>${esc(row.label)}</td><td class="num">${fmt(row.base)}</td>
        <td class="num">${fmt(row.cand)}</td>
        <td class="num">${row.delta != null ? fmt(row.delta) : "—"}${row.delta_pct != null ? " (" + row.delta_pct + "%)" : ""}</td>
        <td><span class="pill ${cls2}">${esc(row.status)}</span></td></tr>`;
    }
    html += `</tbody></table>`;
  }
  document.getElementById("cmp-out").innerHTML = html;
}

boot();
</script>
</body></html>"""

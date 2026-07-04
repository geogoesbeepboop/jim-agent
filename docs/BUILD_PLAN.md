# jim — phased build plan

**North star:** an impersonal, fully-cited financial research service that sells
over x402 and pays for its own data over x402, built so every number traces to a
public-domain primary source — which simultaneously makes it trustworthy,
resaleable, and inside the publisher's-exclusion lane.

---

## Phase 0 — Prove the payment layer first (~1 week) — **IN PROGRESS**

Goal: end-to-end x402 payment working on testnet before any AI is involved.
De-risks the #1 concern up front.

- [x] Pin `x402==2.12.*`, `[fastapi,httpx,evm]` extras; confirm all components are V2.
- [x] Paywall a trivial `/ping` at `$0.01` via `PaymentMiddlewareASGI`.
- [x] Build an `x402[httpx]` buy client that pays a 402 challenge.
- [x] Offline proof: 402 challenge advertises correct `accepts[]`
      (`exact` / `eip155:84532` / USDC / `amount=10000`) via the V2
      `payment-required` header.
- [ ] **Live settlement:** fund a Base Sepolia wallet (ETH for gas + USDC to
      spend) and run `scripts/ping_demo.py` to settle a real $0.01 payment.

**V2 notes (learned, not guessed):**
- The 402 challenge body is empty `{}`; requirements ride in a base64
  `payment-required` response header (v1 used `X-PAYMENT`).
- Network ids are CAIP-2 (`eip155:84532`), not bare chain names.
- A `$`-denominated `price` is auto-converted to USDC base units by the SDK.

**Custody note:** Phase 0 uses a plain local `eth_account` key for minimum moving
parts. Coinbase CDP MPC server wallets are the production-custody upgrade and
slot in behind the same `address` / signer interface in `jim.wallet`.

**Exit criteria:** a USDC payment settles on Base Sepolia for an API call we
control, as both seller and buyer.

---

## Phase 1 — Sell side MVP on redistributable data (~2–3 weeks) — **IN PROGRESS**

Goal: one paywalled research product on public-domain SEC EDGAR, sourcing gate live.

- [x] One research type: "company fundamentals snapshot" for a ticker, from EDGAR
      (`jim.research.edgar` → ~20 cited facts: income statement, balance sheet,
      cash flow, EPS, plus derived margins/returns/leverage/growth).
- [x] Research engine v1 (LangGraph): `gather → synthesize → sourcing-gate`, with
      retry-then-fail-closed on a gate failure (`jim.research.engine`).
- [x] **Provable** sourcing gate (`jim.research.gate`): deterministic, no LLM —
      every figure must match a cited EDGAR fact within tolerance, or the run is
      rejected. Tested to block planted hallucinations, uncited and phantom cites.
- [x] LLM faithfulness judge as a semantic second layer (`jim.research.judge`).
- [x] Paywalled x402 endpoint `GET /research/fundamentals?ticker=&mode=`.
- [x] Langfuse traces every run with cost + factuality scores (optional, no-op
      if unconfigured) (`jim.obs.tracing`).
- [x] Impersonal output + verbatim disclaimer (publisher's-exclusion lane).
- [ ] **Live exit run:** with `ANTHROPIC_API_KEY` set, a paid call returns a
      `status="ok"` memo at 100% sourcing coverage for a real ticker.

**Decision:** Phase 1's factuality gate is the *deterministic* sourcing gate plus
a lightweight native-Anthropic faithfulness judge. DeepEval (named in the
original plan) is deferred to Phase 3, where it becomes the regression eval suite
— it needs a labelled dataset to be meaningful, which Phase 3 builds. This keeps
Phase 1 shippable now while honoring "fails the run on any unsupported number."

**Exit criteria:** a paid call returns a memo where 100% of figures resolve to an
EDGAR citation, and the gate provably blocks a planted hallucination. *(The gate
proof is done and tested; the live paid run needs an Anthropic key.)*

---

## Phase 2 — Buy side + the margin engine (~2–3 weeks) — **IN PROGRESS**

Goal: genuinely two-sided, with positive unit economics.

- [x] Buy client purchases upstream data over x402 (`jim.buyer.pay`: POST+JSON,
      reports `cost_in` + tx hash via an unpaid price pre-flight).
- [x] The Graph integration (`jim.sources.thegraph`): real `gateway.thegraph.com`
      on Base **mainnet** behind `GRAPH_LIVE`, with a local Sepolia **mock**
      (`jim.vendor.mock_graph`, same Uniswap-v3 JSON shape) so the loop verifies
      on testnet with free USDC. One flag flips live.
- [x] Deterministic per-query budget cap (`jim.research.budget`): source proposes,
      code disposes; hard ceiling on data spend.
- [x] Cache + repackage layer, Postgres + pgvector (`jim.store`): buy a datum once
      (`data_purchases`, TTL), reuse at zero marginal cost; per-query economics
      (`query_records`); semantic insight cache (`insights`, pgvector cosine).
      In-memory fallback when `DATABASE_URL` is unset, so tests need no infra.
- [x] New product `token` (on-chain snapshot) reusing the whole engine + gate;
      `price_out` − `data_cost` − `inference_cost` = margin, recorded per run.
- [x] Margin dashboard: `jim-dashboard` CLI + free `GET /dashboard` endpoint.
- [x] Pre-compute popular tokens (`scripts/precompute.py`) to warm the cache.
- [ ] **Live exit run:** with the seller up + a funded Sepolia wallet, buy several
      `/research/token` reports and confirm `/dashboard` shows positive per-query
      margin (cache hits drive data cost → $0 on repackaged sales).

**Decision:** The Graph charges **real USDC on Base mainnet** (no usable testnet
endpoint — probed and confirmed). To verify Phase 2 economics without spending
real money, the same source code path runs against a local testnet mock by
default; `GRAPH_LIVE=true` switches to the real mainnet gateway once a mainnet
wallet is funded. This pulls only the *buy-leg* mainnet cutover (Phase 5) forward.

**Verified:** budget, cache, margin math, pgvector cosine search, and the full
token product (real parser + gate + margin) — offline + against real
Postgres+pgvector. The live exit run needs the running seller + funded wallet.

**Exit criteria:** a dashboard showing per-query margin positive after data +
inference cost (payment overhead ≈ 0 on the free facilitator tier).

---

## Phase 3 — Adversarial verification (~2 weeks) — **IN PROGRESS**

Goal: research quality + the best "how I built it" story.

- [x] Bull-agent / bear-agent / judge debate before publishing
      ([debate.py](../src/jim/research/debate.py)); the thesis is attacked from
      both sides, then the judge's verdict feeds the synthesizer.
- [x] Multi-source metrics: EDGAR (income statement, EBITDA, shares, DPS) + Yahoo
      (price, 52W range, volume) → Market Cap, P/E (TTM), P/B, dividend yield, and
      technicals (SMA50/200, RSI, MACD). Each cites its source(s).
- [x] Two output modes — `human` (narrative, plain-language) vs `agent` (terse,
      metric-dense, grouped) — differentiated in the synthesizer prompt.
- [x] Gate hardened: now also validates bare-decimal indicators (RSI/MACD) via a
      citation-anchored check; gate regression is a merge gate (`jim-eval --gate-only`).
- [x] Regression eval suite ([eval/](../src/jim/eval/), `jim-eval`): gate
      regression (offline) + debate-vs-single-pass lift, logged to Langfuse.
      Grown into the full harness — tiered offline suites, persisted runs,
      baseline compare, dashboard (see ADR-0009).
- [ ] **Live exit run:** `jim-eval run --suite live` over the held-out set shows
      debate ≥ single-pass on gate pass-rate / faithfulness (needs ANTHROPIC_API_KEY).

**Notes:**
- "P/E (Fwd)" needs forward EPS estimates (an analyst-estimates source we don't
  have cleanly/freely); we publish **P/E (TTM)** honestly instead.
- DeepEval (deferred from Phase 1) is the natural drop-in for the lift metric's
  faithfulness scoring; the suite currently uses the native judge.
- Catalyst analysis / sensitivity tables are future additions to the metric set.

**Verified:** expanded metrics live (37 facts for AAPL incl. Market Cap, P/E,
RSI, MACD), the hardened gate (RSI hallucination blocked), and the gate
regression (5/5). The lift comparison needs an Anthropic key.

**Exit criteria:** measurable lift on a held-out eval set vs. the Phase-1
single-pass synthesizer.

---

## Phase 4 — Monitors: the continuous "motley crew" (~2–3 weeks) — **IMPLEMENTED**

Goal: user-directed continuous research.

- [x] Saved, scheduled monitors ([monitors/](../src/jim/monitors/)): a monitor
      re-runs a product on a cadence, **diffs** the fresh facts against its last
      baseline ([diff.py](../src/jim/monitors/diff.py)), and runs a deterministic
      crew of triggers ([triggers.py](../src/jim/monitors/triggers.py): price
      move, metric change, threshold crossings like RSI 70/30, MA golden/death
      cross, new SEC filing).
- [x] **Materiality gate** ([materiality.py](../src/jim/monitors/materiality.py)):
      deterministic — a severity floor + per-signal cooldown decide whether to
      alert. No model in the loop, mirroring the sourcing gate (see
      [ADR-0001](adr/0001-deterministic-materiality-gate.md)).
- [x] Re-run on new data + **push updates**: a material change synthesizes a short
      cited update ([update.py](../src/jim/monitors/update.py)) that passes the
      sourcing gate *and* a deterministic impersonal guard
      ([impersonal.py](../src/jim/monitors/impersonal.py)), delivered over
      console / **HMAC-signed webhooks** ([notify.py](../src/jim/monitors/notify.py))
      plus an always-on pull feed.
- [x] **Lightweight scheduler first** ([scheduler.py](../src/jim/monitors/scheduler.py)):
      a dependency-free asyncio due-loop; durable state (`next_run_at`, baseline,
      cooldowns) lives in the store so restarts resume. `run_monitor_once` is the
      isolated unit of work, so APScheduler/Temporal is a one-file swap later
      (see [ADR-0002](adr/0002-lightweight-asyncio-scheduler.md)).
- [x] **Every monitor output stays general, not personalized** — enforced
      deterministically (impersonal guard rejects second-person address, advice,
      buy/sell/hold, price targets).
- [x] Surface area: `jim-monitor` CLI (add/list/rm/run/run-all/preview/serve/feed),
      free seller endpoints (`GET/POST /monitors`, `POST /monitors/{id}/run`,
      `GET /monitors/feed`), optional in-seller scheduler (`MONITOR_AUTOSTART`),
      and a monitor-economics section on `jim-dashboard`.
- [x] **Bell/whistle — NL → spec** ([nl.py](../src/jim/monitors/nl.py)): a
      plain-English request is parsed into validated triggers (LLM structured
      output when a key is set, deterministic keyword parser otherwise); every
      proposed trigger is checked against the registry + clamped (propose/dispose).
- [ ] **Live exit run:** stand up a Postgres-backed fleet on a schedule and
      observe a real material change push a cited update (needs a running seller
      + DB; ANTHROPIC_API_KEY only for nicer prose).

**Decision:** the alert/no-alert decision is *deterministic*, same as the sourcing
gate — code decides whether there's news; the model only writes once there is.
This keeps alerts reproducible/auditable and makes quiet polls cost **$0
inference** (the dashboard's `inference_saved_usd` quantifies what the materiality
gate avoids). Monitoring works with **no API key** via a gate-safe deterministic
update fallback.

**Decision (sources):** Unusual Whales is intentionally *not* wired in yet — it
only enters behind an Enterprise redistribution license or purely as an internal
signal informing general published analysis. The crew runs today on the same
redistributable EDGAR + (flagged) market data and The Graph sources Phases 1–3
already use; a new watcher is a pure function added to the registry.

**Verified:** offline tests for the diff, every trigger, the materiality gate +
cooldown, the impersonal guard, the gate-safe update fallback, the
baseline→quiet→material lifecycle + economics, the scheduler's due-loop +
reschedule, monitor persistence, NL→spec parsing, and signed-webhook delivery
(44 monitor tests; 75 total). The live exit run needs a running seller + DB.

**Exit criteria:** a scheduled monitor detects a real change in fresh data and
pushes a general, fully-cited update — with quiet polls incurring no inference
cost.

---

## Phase 5 — Marketplace, discovery, mainnet (~1–2 weeks) — **IMPLEMENTED**

Goal: other agents auto-discover and pay us; real money.

- [x] **Bazaar discovery metadata** ([marketplace/catalog.py](../src/jim/marketplace/catalog.py)):
      each paid route carries an x402 `declare_discovery_extension` (input/output
      JSON schemas + example) plus `service_name`/`tags`/`icon_url`, so the **first
      successful settlement auto-indexes us** on a Bazaar-speaking facilitator —
      zero manual submission (see [ADR-0003](adr/0003-bazaar-discovery.md)).
- [x] **Pull discovery**: a deterministic `GET /.well-known/x402` manifest
      ([discovery.py](../src/jim/marketplace/discovery.py)) + `GET /catalog` +
      `GET /pricing`, all reading one catalog so nothing drifts.
- [x] **Published pricing tiers** ([pricing.py](../src/jim/marketplace/pricing.py)):
      `oneshot` / `agent` (machine-buyer discount) / `bundle` / `monitor`, derived
      deterministically from config — honest about the prices the system charges.
- [x] **Thin human UI** ([ui.py](../src/jim/marketplace/ui.py), `GET /`): a
      dependency-free storefront that **pays via x402 under the hood** — when
      funded, jim buys its own endpoint (`UI_SETTLE_VIA_X402`); otherwise a
      labelled preview. Same `run_research` + sourcing gate behind it.
- [x] **jim as an MCP server** ([mcp_server.py](../src/jim/marketplace/mcp_server.py),
      `jim-mcp`): `research_fundamentals` / `research_token` exposed as **x402-gated
      MCP tools** — the tool call triggers the 402 → pay → settle cycle, the same
      gate/budget apply (the "agents discover + pay us over MCP" story; optional
      `mcp` extra).
- [x] **The system visualization** ([sysmap.py](../src/jim/marketplace/sysmap.py),
      `jim-map`, `GET /map` · `/map.mmd` · `/map.json`): a **live Mermaid map** of
      the whole system generated from the running config + catalog, plus the
      hand-drawn diagram set in [SYSTEM_MAP.md](SYSTEM_MAP.md).
- [x] **Guarded mainnet cutover** ([mainnet.py](../src/jim/marketplace/mainnet.py),
      `jim-market mainnet`, `GET /mainnet/readiness`): a read-only preflight that
      reports network/wallet/facilitator/balance readiness and **moves no money**;
      operator-supplied facilitator min/fee are checked against our prices (see
      [ADR-0004](adr/0004-mainnet-cutover-and-ui-self-pay.md)). The buy leg was
      already mainnet-capable since Phase 2 (`GRAPH_LIVE`), so this is the sell-leg
      cutover plus guardrails.
- [ ] **Live exit run:** on Base mainnet with a funded wallet + a mainnet
      facilitator, settle a real paid call, confirm a Bazaar facilitator indexes
      us, and read real fees/minimums off the receipt.

**Decision:** discovery rides x402's **own** Bazaar rail rather than a bespoke
registry (ADR-0003); the catalog is the single source of truth for routes, the
manifest, MCP tools, the UI, and the map. The mainnet cutover is gated behind a
preflight that can't spend, and the UI proves the rail by self-paying when funded
(ADR-0004). The advertised output schema is intentionally ref-free so indexers
accept it.

**Verified:** offline tests for the catalog + Bazaar extension shape, pricing
tiers, the discovery manifest (deterministic), the MCP tool surface, the mainnet
readiness checklist (testnet/mainnet/fail paths), the live system-map generator
(flag-reflecting, no dangling edges), and the seller's new discovery/UI/map
endpoints + the 402 advertising the Bazaar extension (33 Phase-5 tests; 114
total). The live exit run needs a funded mainnet wallet + a mainnet facilitator.

**Exit criteria:** a third-party agent discovers jim (via the manifest or a Bazaar
index), pays on Base mainnet with real USDC, and the receipt shows the real
facilitator fee/minimum.

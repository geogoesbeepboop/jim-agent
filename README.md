# jim

An impersonal, fully-cited financial research service that **sells over x402**
and **pays for its own data over x402** — built so every number traces to a
public-domain primary source.

See [docs/BUILD_PLAN.md](docs/BUILD_PLAN.md) for the full phased plan,
**[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** for a deep dive into how it all
works (pipeline, sources, the gate, payments, tools/MCP, evals), and
**[docs/SYSTEM_MAP.md](docs/SYSTEM_MAP.md)** for the system visualized end-to-end
in Mermaid (or run `uv run jim-map` for your live config).

Looking forward: **[docs/NORTH_STAR.md](docs/NORTH_STAR.md)** is the strategy —
jim as the *verifiable delivery* layer of agentic commerce — with
**[docs/DEPLOY.md](docs/DEPLOY.md)** (the go-live runbook) and
**[docs/LAUNCH.md](docs/LAUNCH.md)** (the demo + listings kit) as its Horizon 1
arms; **[docs/ROADMAP.md](docs/ROADMAP.md)** is the buildable backlog;
**[docs/ENTERPRISE_VISION.md](docs/ENTERPRISE_VISION.md)** is the "how I'd scale
this at Stripe / VISA / Coinbase" thinking; and
**[docs/AGENT_INTEROP.md](docs/AGENT_INTEROP.md)** covers whether and how jim
should talk to other agents.

> **Status: Phase 7 — the agent economy (+ Track 0 & Phase 6 hardening).** jim
> now **composes peer agents**: a `PeerSource` buys cited signals from other
> x402 agents through the same budget/cache path as The Graph, the sourcing
> gate verifies their figures like everything else (the composition firewall),
> a **trust ledger** scores every source by its gate pass-rate and refuses
> peers that stop verifying, and a propagated **`X-Jim-Call-Chain`** refuses
> payment loops / over-depth request trees before any money moves. An
> **A2A agent card** (`/.well-known/agent-card.json`) makes jim delegable, not
> just callable. The gate itself is now **fuzz-hardened** (Hypothesis) against
> exotic number renderings, and the seller enforces a new billing invariant:
> **gate-rejected research is refused, never billed** (see ADR-0008 — the fix
> for our first mainnet settlement, which charged for a rejected memo).
> Phases 0–5 (payment rail; cited EDGAR fundamentals + sourcing gate; two-sided
> buy + margin engine; debate + metrics + regression eval; monitors with a
> deterministic materiality gate; marketplace, discovery, mainnet) are done.

## Layout

```
src/jim/
  config.py             # env-driven settings (network, wallet, models, db, Graph)
  wallet/               # local eth_account wallet (CDP MPC is the prod upgrade)
  seller/app.py         # FastAPI; /ping, /research/{fundamentals,token}, /dashboard
  buyer/client.py       # x402[httpx] client: pays a 402, reports cost_in + tx
  research/
    edgar.py            # SEC EDGAR client: ticker→CIK→XBRL facts (free upstream)
    facts.py            # cited data model + derived-metric computation
    gate.py             # the provable sourcing gate (deterministic, no LLM)
    completeness.py     # the gate's mirror: flags material facts the memo omitted
    synthesize.py       # Anthropic synthesizer → cited memo (human/agent modes)
    judge.py            # LLM faithfulness judge: per-claim checklist + Sonnet tier
    budget.py           # per-query budget cap (propose/dispose)
    products.py         # registry: fundamentals→EDGAR, token→The Graph
    engine.py           # LangGraph: gather → memo-cache → synthesize → gate(retry) → judge
  eval/                 # eval harness: offline suites + live lift, persisted runs,
                        #   baseline compare, dashboard (jim-eval / jim-eval ui)
  interop/              # Phase 7: the seam between agents (model proposes, code disposes)
    callchain.py        # X-Jim-Call-Chain: loop + depth refusal BEFORE any payment
    trust.py            # per-source trust = gate pass-rate (reputation by verification)
  sources/              # Source interface: EDGAR + macro (free) · The Graph (paid x402)
    thegraph.py         # multi-chain Uniswap-v3 (ETH/Base/Arbitrum/Polygon) over x402
    macro.py            # free public-domain macro: Fed funds · CPI · Treasury yields
    peer.py             # Phase 7: buy cited signals from PEER AGENTS over x402 + compose
  monitors/             # Phase 4: scheduled diff-driven monitors (the "motley crew")
    diff.py             # deterministic snapshot diffing (baseline → fresh)
    triggers.py         # the crew: price/threshold/MA-cross/new-filing watchers
    materiality.py      # deterministic alert gate (severity floor + cooldown)
    update.py           # short cited update memo (gated; no-key fallback)
    impersonal.py       # deterministic "stays general, not personalized" guard
    notify.py           # delivery channels: console + HMAC-signed webhook
    engine.py           # run_monitor_once: gather→diff→crew→gate→write→push
    scheduler.py        # lightweight asyncio due-loop (APScheduler/Temporal later)
    nl.py / create.py   # natural-language → validated trigger spec (propose/dispose)
  marketplace/          # Phase 5: discovery, pricing, MCP server, human UI, system map
    catalog.py          # one source of truth: listings + Bazaar discovery extensions
    pricing.py          # published pricing tiers (deterministic)
    discovery.py        # the /.well-known/x402 manifest
    mcp_server.py       # jim as an x402-gated MCP server (jim-mcp)
    ui.py               # human storefront: free Preview + real browser-wallet checkout
    sysmap.py           # live Mermaid system map (jim-map; GET /map)
    mainnet.py          # read-only mainnet-cutover readiness preflight
    cli.py              # jim-market: catalog / pricing / manifest / mainnet
  seller/
    app.py              # FastAPI app + route wiring + x402 wallet paywall
    audit.py            # settlement audit middleware → one receipt per paid call
  store/                # Postgres+pgvector cache + margin ledger + monitors + receipts
  vendor/mock_graph.py  # testnet stand-in for The Graph (same JSON shape)
  dashboard.py          # per-query margin + monitor-economics dashboard (/dashboard)
  admin.py              # settlement audit dashboard: revenue, buyers, on-chain tx (/admin)
  obs/tracing.py        # optional Langfuse trace (cost + factuality), no-op if unset
scripts/
  ping_demo.py          # Phase 0: pay our own /ping, print the receipt
  research_demo.py      # Phase 1: pay for a fundamentals memo, print it
  precompute.py         # Phase 2: warm the cache for popular tokens
  graph_probe.py        # Phase 2: decode the Graph 402 (live vs mock) before paying
  peer_demo.py          # Phase 7: jim subcontracts a peer agent over x402, gate-verified
tests/                  # offline proofs: payment, gate, graph logic, budget, margin
docker-compose.yml      # local Postgres + pgvector
```

## The sourcing gate (the core idea)

Every number jim publishes is a `Fact` carrying the SEC accession of the filing
it came from. After the model writes a memo, [gate.py](src/jim/research/gate.py)
**deterministically** checks that every dollar amount, percentage, and ratio in
the prose sits next to a `[C#]` citation whose fact value *matches it* within a
rounding tolerance. A fabricated number has no fact it matches, so it can't be
covered — the run is rejected and never billed as `ok`. No model is in this loop,
so the verdict is reproducible and auditable.

## Quickstart (Phase 0)

```bash
# 1. Install
uv sync --extra dev

# 2. Make a testnet wallet, then fund it from the faucets it prints
uv run jim-wallet new
#    -> copy EVM_PRIVATE_KEY + EVM_ADDRESS into .env  (cp .env.example .env first)
#    -> Base Sepolia ETH : https://www.alchemy.com/faucets/base-sepolia
#    -> Base Sepolia USDC: https://faucet.circle.com   (network: Base Sepolia)

# 3. Run the seller
uv run jim-seller            # serves on http://localhost:4021

# 4. In another terminal, pay for /ping end-to-end
uv run python scripts/ping_demo.py
```

A `✅` from step 4 with a settlement receipt = Phase 0 exit criterion met.

## Research (Phase 1)

```bash
# Set ANTHROPIC_API_KEY in .env (the synthesizer needs it; the gate does not).

# Run the engine locally — no payment, prints memo + gate verdict + citations
uv run jim-research AAPL
uv run jim-research MSFT --mode agent      # terse, metric-dense output
uv run jim-research NVDA --json            # full machine-readable response

# Or buy it over x402 from a running seller (mirrors the upstream-data buy path)
uv run python scripts/research_demo.py AAPL
```

The gather + sourcing-gate path runs **without any API key** — you can fetch a
live EDGAR snapshot and exercise the gate offline. Only the prose synthesis
needs Anthropic.

Phase 3 adds the bull/bear/judge debate and price-derived metrics automatically
(`ENABLE_DEBATE` / `ENABLE_PRICES`), and a regression eval:

```bash
uv run jim-research AAPL                  # now includes debate + Market Cap, P/E, RSI, MACD...
uv run jim-eval --gate-only               # offline: planted hallucinations must be blocked (no key)
uv run jim-eval run --suite live AAPL MSFT  # live: debate vs single-pass lift (needs key)
```

## Research quality: memo cache, completeness, judge, rubric

Four upgrades to "is the answer good?" — see
[ADR-0006](docs/adr/0006-research-quality-memo-cache-completeness-judge-rubric.md).

- **Memo cache.** After `gather`, jim fingerprints the fresh snapshot; if a recent
  memo for `{product}:{identifier}:{mode}` was written from **identical** data and
  still passes the deterministic gate, it's served directly — synthesis/debate/judge
  are skipped and **inference cost is $0**. Moved data (a new price) changes the
  fingerprint and correctly re-synthesizes, so the cache only hits when nothing
  changed. The gate re-check means a cached memo can never ship unsourced.
- **Completeness check.** The gate's mirror image — it flags **material** snapshot
  facts the memo *omitted* (deterministic, no key). A signal, not a gate: it lowers
  the quality score and is surfaced, but never rejects a run.
- **Structured judge.** The faithfulness judge returns a **per-claim checklist**
  (each claim → supported? which citation? why), and high-stakes runs upgrade to a
  stronger model (`JUDGE_HIGH_STAKES_MODEL`).
- **Eval rubric.** A weighted composite over sourcing + completeness + impersonal
  (all deterministic, no key) plus faithfulness when live — so "better output" is a
  number, computable offline.

```bash
uv run jim-research AAPL                  # 2nd identical run → "(memo cache — $0 inference)"
uv run jim-research AAPL --no-cache       # force a fresh synthesis
uv run jim-research NVDA --high-stakes    # upgrade the faithfulness judge to Sonnet
uv run jim-eval run --suite live AAPL MSFT  # live report: material coverage + composite rubric
```
> New tables: run `uv run jim-initdb` once to create `memo_cache` (and, for
> Phase 7, `source_trust_events`) — a no-op for tables that already exist.

## Two-sided + margin (Phase 2)

jim's `token` product **buys** its upstream data over x402. By default it buys
from a local **mock** Graph vendor on Base Sepolia (free testnet USDC); set
`GRAPH_LIVE=true` to buy from the real `gateway.thegraph.com` on Base mainnet
(real USDC, ~$0.01/query).

```bash
# 1. Stand up the cache + margin ledger (Postgres + pgvector)
docker compose up -d
export DATABASE_URL=postgresql+asyncpg://jim:jim@localhost:5432/jim
uv run jim-initdb

# 2. Run the seller (serves the token product AND the mock-Graph vendor)
uv run jim-seller

# 3. Buy on-chain token research; jim pays The Graph (mock) under the hood
uv run jim-research WETH --product token         # Uniswap v3 · Ethereum (default)
uv run jim-research AERO:base --product token     # multi-chain: :chain suffix (ADR-0007)
uv run jim-research ARB:arbitrum --product token  # Base / Arbitrum / Polygon supported
uv run python scripts/precompute.py              # warm WETH/WBTC/UNI into cache

# 4. See the economics: price_out − data_cost − inference = margin
uv run jim-dashboard                             # or: GET http://localhost:4021/dashboard
```

`price_out − data_cost − inference_cost = margin`. The first buy of a token costs
`data_cost`; the cache makes every later sale within the TTL pure margin —
"buy a datum once, resell derived insight many times". The live x402 price is
**dynamic and unpublished**, so the buy path enforces a hard **price cap** (refuses
above the per-query budget) and `graph_probe` audits it before a mainnet cutover:

```bash
uv run python scripts/graph_probe.py WETH         # decode the live price + PASS/FAIL vs budget
uv run python scripts/graph_probe.py AERO:base    # chain-aware
```

### Macro context (free, public-domain)

A third product, `macro`, cites **US-government primary sources** (Fed funds, CPI,
Treasury yields + 2s10s) — public domain, redistributable, $0 data cost (pure
margin). Deliberately not FRED (its ToS forbids redistribution); jim goes straight
to the Fed / BLS / Treasury. Proprietary sources (earnings transcripts, forward-EPS
estimates, index levels) were **researched and refused** — they prohibit
redistribution and break the public-domain invariant. See
[ADR-0007](docs/adr/0007-data-source-economics-multichain-macro.md).

```bash
uv run jim-research US --product macro            # cited Fed funds / CPI / Treasury snapshot
```

## Monitors (Phase 4)

A **monitor** is a saved, scheduled research directive. On each tick it gathers a
fresh snapshot, **diffs** it against the last baseline, and runs a deterministic
crew of **triggers** (price moves, RSI/threshold crossings, golden/death cross,
new SEC filings, any metric change). A **materiality gate** (deterministic, no
model) decides whether anything cleared the bar — applying a severity floor and a
per-signal cooldown. Only then does an LLM write a short, fully-cited, impersonal
update, which still passes the sourcing gate **and** a deterministic
impersonal-output guard before it's pushed.

```bash
# Watch with explicit triggers, or describe it in English (parsed → validated triggers)
uv run jim-monitor add AAPL --watch price:5 rsi:70/30 filing --every 1d
uv run jim-monitor add NVDA --describe "ping me on big earnings moves or overbought RSI"
uv run jim-monitor add WETH --product token --watch price:8 --channel webhook:https://you/hook

uv run jim-monitor list                 # the fleet + each crew + next run
uv run jim-monitor run <id>             # run one cycle now (delivers)
uv run jim-monitor preview <id>         # dry-run: what WOULD fire (no deliver/persist)
uv run jim-monitor run-all              # run everything currently due
uv run jim-monitor serve                # the scheduler loop (or MONITOR_AUTOSTART in the seller)
uv run jim-monitor feed                 # recent material updates (cited, impersonal)
```

The whole detection path — gather, diff, crew, materiality gate — needs **no API
key**; if none is set, a deterministic update memo is rendered straight from the
cited facts (gate-safe by construction), exactly like the sourcing gate. The key
only buys nicer prose. **Quiet polls cost $0 inference**, so most of a monitor's
life is free; `jim-dashboard` surfaces the materiality rate and the
`inference_saved_usd` the gate avoided. The seller also exposes free management
endpoints (`GET/POST /monitors`, `POST /monitors/{id}/run`, `GET /monitors/feed`).
Use Postgres (`DATABASE_URL`) for a standing fleet — the in-memory store does not
persist across separate CLI invocations.

## Marketplace, discovery & the system map (Phase 5)

jim is now discoverable and payable by other agents, exposes itself over MCP, and
can draw a picture of itself. None of this needs a key, wallet, or network.

```bash
# Inspect the marketplace surface
uv run jim-market catalog        # products: routes, prices, tiers, sources, upstreams
uv run jim-market pricing         # the published pricing schedule (oneshot/agent/bundle/monitor)
uv run jim-market manifest        # the /.well-known/x402 discovery manifest (JSON)
uv run jim-market mainnet         # mainnet-cutover readiness preflight (reads only; moves no money)

# See the whole system, generated from your live config
uv run jim-map                    # Mermaid (paste into GitHub / any renderer)
uv run jim-map --format html -o map.html   # self-contained page (mermaid.js)

# Run a seller and browse it
uv run jim-seller
#   GET /            → the human storefront: free Preview + "Pay with wallet"
#   GET /catalog     → machine-readable product list
#   GET /pricing     → pricing tiers
#   GET /admin       → settlement audit dashboard (revenue · buyers · on-chain tx)
#   GET /admin/audit → the same audit trail as JSON
#   GET /.well-known/x402  → the discovery manifest agents fetch
#   GET /map         → the live system map in your browser
#   a paid route's 402 now carries a Bazaar discovery extension → first settle auto-indexes us
#   a *browser* hitting a paid route gets x402's wallet paywall (MetaMask / Coinbase Wallet);
#     agents still get the machine-readable 402 — see ADR-0005

# Expose jim as an MCP server (agents discover + pay our tools over MCP)
uv sync --extra mcp && uv run jim-mcp     # stdio; --transport http for streamable-http
```

Discovery rides x402's **own** Bazaar rail (no bespoke registry): each paid route
advertises input/output JSON schemas + price, so the first successful settlement
auto-indexes jim on a Bazaar-speaking facilitator. The catalog is the single
source of truth behind the routes, the manifest, the MCP tools, the UI, and the
map — so prices and schemas never drift. The mainnet preflight is a dry-run that
can't spend; the buy leg has been mainnet-capable since Phase 2 (`GRAPH_LIVE`). See
**[docs/SYSTEM_MAP.md](docs/SYSTEM_MAP.md)**, [ADR-0003](docs/adr/0003-bazaar-discovery.md),
and [ADR-0004](docs/adr/0004-mainnet-cutover-and-ui-self-pay.md).

## The agent economy (Phase 7): peers, trust, call-chain safety

jim is now a **general contractor**: it can buy specialized, cited signals from
peer agents over x402, verify them with the same sourcing gate that guards its
own prose, and resell the composed synthesis. See
[ADR-0008](docs/adr/0008-agent-economy-trust-callchain-billing.md) and
[docs/AGENT_INTEROP.md](docs/AGENT_INTEROP.md).

```bash
# Configure peers (JSON env). Each becomes a Source composed into the named
# products, buying through the same procure() → budget → cache path as The Graph.
PEER_SOURCES='[{"name":"mock-sentiment",
                "url":"http://localhost:4021/mock-peer/research",
                "price_estimate_usd":0.01,
                "products":["fundamentals"]}]'

uv run jim-seller               # also serves the mock peer vendor (testnet stand-in)
uv run jim-research AAPL        # the memo can now cite peer facts — gate-verified
uv run jim-dashboard            # + per-source trust: gate pass-rate as reputation
uv run jim-market agent-card    # the A2A card peers use to delegate tasks to jim
```

Three deterministic guards make composition safe (model proposes, code disposes):

- **The gate is the firewall.** A peer's figure must match its cited fact like
  any EDGAR number — jim can buy from agents it does not trust and still only
  ship what it can verify.
- **Trust = verification.** Every gated run credits/debits the sources whose
  facts it used; the Laplace-smoothed pass-rate is the score. Peers below
  `PEER_TRUST_FLOOR` are refused *before* payment. No reviews, no ratings —
  outcomes jim observed itself.
- **The call chain is bounded.** Buys carry `X-Jim-Call-Chain`; the seller
  refuses payment loops (our address already in the chain) and over-depth
  trees with a 409 **before** the paywall verifies anything.

## Billing invariant: rejected research is never billed

The x402 middleware only settles 2xx responses — so the seller now *refuses*
gate-rejected runs (HTTP 502 + diagnostics, MCP tool error, UI "not billed"
notice), which cancels the verified payment. The engine books rejected runs at
$0 revenue, so `/dashboard` shows the true loss. This closes the incident from
our first mainnet settlement, where a rejected COIN memo settled anyway.

## Go live: the proof page, receipts & identity (Horizon 1)

The outward-facing layer — see [docs/NORTH_STAR.md](docs/NORTH_STAR.md) for why.

```bash
uv run jim-seller
#   GET /proof        → the public proof page: live settlements (tx links), gate
#                       pass-rate, source-trust table, and money REFUSED because
#                       verification failed — radical transparency, reproducible
#                       from the store (GET /proof.json for machines)

uv run jim-identity card       # the ERC-8004-shaped identity payload (offline)
uv run jim-identity register   # prepares the onchain registration — NEVER sends;
                               # the operator executes it (guarded by design)
uv run jim-identity attest AAPL   # run + sign a gate-verdict receipt: memo hash →
                                  # fingerprint → verdict → settlement, EIP-191
uv run jim-identity verify receipt.json   # anyone can verify offline

docker build -t jim .          # the deployable seller (docs/DEPLOY.md runbook)
```

A rejected run is never attested and never billed — receipts only exist for
research the gate verified. The three-minute demo script (including the
lying-peer trust-decay beat, `MOCK_PEER_CORRUPT=true`) lives in
[docs/LAUNCH.md](docs/LAUNCH.md).

## Payments: audit log, admin dashboard & browser wallets

Every x402 payment that settles at the paywall is recorded as an **on-chain audit
receipt** — buyer address, settlement tx hash, settled USDC, and which query —
captured by a thin middleware that decodes the settlement header *after* the
handler returns (the only point where the tx hash exists). It's append-only and
best-effort: a store outage is logged and swallowed, never blocking a paid
delivery. This is deliberately separate from the margin ledger — settlement
(*who paid us, on which tx*) vs. economics (*margin per query*).

```bash
uv run jim-admin                 # revenue · unique buyers · per-tx audit trail (CLI)
#   GET /admin                   → the same, in the browser (Basescan links)
#   GET /admin/audit             → JSON
```

A **human can now pay with their own wallet**: a browser that hits a paid route is
served x402's bundled paywall (MetaMask / Coinbase Wallet / WalletConnect → real
EIP-3009 settlement). The storefront keeps a free **Preview** and adds a **Pay
with wallet** action; **agents still get the machine-readable 402** (the paywall
is gated on a browser `Accept` + UA), so nothing about the agent path changes. No
new config or hand-rolled crypto. See
[ADR-0005](docs/adr/0005-settlement-audit-and-browser-wallet.md).
> New table: run `uv run jim-initdb` once to create `payment_receipts` before the
> admin view populates (no-op if it already exists).

## The eval harness: is jim improving?

Every property jim sells — lies blocked, tone impersonal, rejected runs never
billed — plus quality/cost/latency, measured per run and tracked over time. See
[ADR-0009](docs/adr/0009-eval-harness-persisted-runs-tiered-suites.md).

```bash
uv run jim-eval                            # offline suites (87 cases, ~1.5s, no key/DB/network):
                                           #  · gate: 38 memos — every notation the extractor
                                           #    knows, truthful (must pass) + planted (must reject)
                                           #  · guards: impersonal tone, hostile identifiers,
                                           #    completeness, materiality, NL propose/dispose
                                           #  · scenarios: the real engine with scripted seams —
                                           #    retry loop, memo cache, fail-closed paths, and the
                                           #    never-bill-rejected invariant checked at the ledger
uv run jim-eval run --suite all --label "sonnet-4-6"   # + live: held-out tickers, single-pass vs
                                           #    debate, rubric/latency/tokens/$ per run (needs key)
uv run jim-eval list                       # run history (persisted to ./eval_runs, gitignored)
uv run jim-eval baseline set latest        # promote a known-good run
uv run jim-eval run --compare-baseline     # exit 1 on regression vs the baseline:
                                           #    offline = any newly-failing case (zero tolerance)
                                           #    live    = configurable thresholds (gate-rate drop,
                                           #              rubric drop, cost/latency increases)
uv run jim-eval compare baseline latest    # the same diff, on demand
uv run jim-eval ui                         # dashboard on :4023 — trend charts (pass rates, rubric,
                                           #    $/run, p50/p95 latency), per-case drill-down with
                                           #    memos + violations, run-vs-run comparison
```

The offline suites are the merge gate (`jim-eval --gate-only` still works for
the gate alone). Every dataset label is itself enforced by
`tests/test_eval_harness.py`, so a mislabeled case fails `pytest` — the eval
can't silently rot.

## Tests

```bash
uv run pytest          # all offline, no wallet/network/API key/DB:
                       #  · payment: /health free, /ping issues a valid 402
                       #  · gate: planted hallucination + uncited/phantom blocked
                       #  · engine: retries on gate failure, then fails closed
                       #  · budget: propose/dispose ceiling enforced
                       #  · store: cache TTL, margin math, pgvector-style search
                       #  · token: end-to-end margin + cache-improves-margin
                       #  · monitors: diff, the trigger crew, materiality+cooldown,
                       #    impersonal guard, gate-safe update fallback, scheduler,
                       #    monitor persistence, NL→spec parsing, signed webhooks
                       #  · marketplace: catalog + Bazaar extension shape, pricing
                       #    tiers, discovery manifest, MCP tool surface, mainnet
                       #    readiness, live system-map generator, discovery/UI endpoints
                       #  · payments: settlement receipt decode, audit middleware
                       #    (records buyer + tx, fails open), admin revenue/buyer
                       #    rollup, wallet paywall served to browsers not agents
                       #  · research quality: memo cache (fingerprint hit/miss/TTL,
                       #    engine serves 2nd identical query at $0 inference),
                       #    completeness omissions, per-claim judge + Sonnet tier,
                       #    weighted eval rubric (offline composite)
                       #  · data sources: multi-chain token resolve + cache isolation,
                       #    free macro source (cited gov data, 2s10s, degrades),
                       #    dynamic-price cap guard (refuses over-budget x402 price)
                       #  · gate fuzzing (Track 0): Hypothesis property suite — no
                       #    fabricated figure passes (sci notation, "5 billion", 5B,
                       #    spelled-out, ranges...), no formatter-true figure rejects
                       #  · billing invariant: rejected runs refused (502 + diagnostics,
                       #    payment cancelled), $0 revenue booked, preview refuses too
                       #  · agent economy (Phase 7): peer facts → cited snapshot via
                       #    budget/cache, purchase cache, trust-floor refusal, composite
                       #    merge + origins, gate-as-firewall, outcome attribution,
                       #    trust ledger, call-chain codec + 409 loop/depth refusals
                       #  · eval harness: every gate/guard/scenario dataset label is
                       #    executed for real (a mislabeled case fails pytest), run
                       #    storage + baseline round-trip, regression verdicts (exact
                       #    offline / thresholded live), CLI exit codes, dashboard API
```

> Tests are hermetic by design (a `conftest.py` neutralises `DATABASE_URL` /
> `ANTHROPIC_API_KEY` from your `.env`), so `uv run pytest` uses the in-memory
> store and no API key regardless of local config.

## Key x402 V2 facts (so you don't relearn them)

- Network ids are CAIP-2: Base Sepolia = `eip155:84532`, Base mainnet = `eip155:8453`.
- The 402 challenge body is empty; requirements ride in a base64
  `payment-required` **header** (v1 used `X-PAYMENT`).
- A `$`-priced route is auto-converted to USDC base units (`$0.01` → `10000`).
- The free testnet facilitator is `https://x402.org/facilitator`.

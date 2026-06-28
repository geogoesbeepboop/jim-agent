# jim

An impersonal, fully-cited financial research service that **sells over x402**
and **pays for its own data over x402** — built so every number traces to a
public-domain primary source.

See [docs/BUILD_PLAN.md](docs/BUILD_PLAN.md) for the full phased plan,
**[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** for a deep dive into how it all
works (pipeline, sources, the gate, payments, tools/MCP, evals), and
**[docs/SYSTEM_MAP.md](docs/SYSTEM_MAP.md)** for the system visualized end-to-end
in Mermaid (or run `uv run jim-map` for your live config).

Looking forward: **[docs/ROADMAP.md](docs/ROADMAP.md)** is the buildable backlog
beyond Phase 5; **[docs/ENTERPRISE_VISION.md](docs/ENTERPRISE_VISION.md)** is the
"how I'd scale this at Stripe / VISA / Coinbase" thinking; and
**[docs/AGENT_INTEROP.md](docs/AGENT_INTEROP.md)** covers whether and how jim should
talk to other agents.

> **Status: Phase 5 — marketplace, discovery, mainnet.** jim is now
> machine-discoverable: each paid route advertises **Bazaar** metadata (so the
> first settlement auto-indexes us), a deterministic **`/.well-known/x402`**
> manifest + `/catalog` + `/pricing` describe every product, and jim is exposed as
> an **x402-gated MCP server** (`jim-mcp`). A thin **human UI** (`GET /`) pays via
> x402 under the hood, a **live system map** (`jim-map` / `GET /map`) renders the
> whole system from the running config, and a read-only **mainnet preflight**
> (`jim-market mainnet`) guards the cutover to real USDC — it never moves money.
> Phases 0–4 (payment rail; cited EDGAR fundamentals + sourcing gate; two-sided
> buy + margin engine; bull/bear/judge debate + expanded metrics + regression
> eval; continuous monitors with a deterministic materiality gate) are done.

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
    synthesize.py       # Anthropic synthesizer → cited memo (human/agent modes)
    judge.py            # optional LLM faithfulness judge (semantic backstop)
    budget.py           # per-query budget cap (propose/dispose)
    products.py         # registry: fundamentals→EDGAR, token→The Graph
    engine.py           # LangGraph: gather → synthesize → gate(retry) → judge
  sources/              # Source interface: EDGAR (free) + The Graph (paid x402)
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
uv run jim-eval AAPL MSFT                 # live: debate vs single-pass lift (needs key)
```

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
uv run jim-research WETH --product token        # local run
uv run python scripts/precompute.py             # warm WETH/WBTC/UNI into cache
uv run python scripts/research_demo.py WETH     # full paid round-trip (optional)

# 4. See the economics: price_out − data_cost − inference = margin
uv run jim-dashboard                             # or: GET http://localhost:4021/dashboard
```

`price_out − data_cost − inference_cost = margin`. The first buy of a token costs
`data_cost`; the cache makes every later sale within the TTL pure margin —
"buy a datum once, resell derived insight many times". Check the live endpoint
price before spending with `uv run python scripts/graph_probe.py WETH`.

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

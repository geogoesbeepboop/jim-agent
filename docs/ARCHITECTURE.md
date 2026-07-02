# jim — Architecture

A deep dive into how jim works: the pipeline, the data model, the sources, the
payment rails, the sourcing gate, the margin engine, and where "tools" / MCP fit.

> **See it as a whole:** [SYSTEM_MAP.md](SYSTEM_MAP.md) has the full set of Mermaid
> diagrams (the whole-system picture, the sell/buy payment sequences, discovery,
> the trust boundary, MCP, monitors). Or run `uv run jim-map` / open `GET /map`
> for a diagram of your *live* configuration.

> **One-line mental model.** jim is a deterministic pipeline that turns a ticker
> (or token) into a prose memo in which **every number is provably traceable to a
> cited primary source**. An LLM writes the prose; deterministic code decides
> whether that prose is allowed to ship. jim **sells** the memo over x402 and
> **buys** some of its inputs over x402, and tracks the margin between the two.

---

## 1. The core invariant

Everything is in service of one property:

> **No number reaches the customer unless it matches a fact jim fetched from a
> cited source, within a rounding tolerance.**

This is enforced by the **sourcing gate** ([gate.py](../src/jim/research/gate.py)),
which contains **no model** — it is pure, reproducible Python. The LLM proposes;
the gate disposes. A fabricated number has no fact whose value it matches, so it
cannot be "covered," and the run is rejected before billing as `ok`.

This single invariant is what makes the output simultaneously **trustworthy**
(auditable to a filing), **resaleable** (you can show the buyer the provenance),
and **inside the publisher's-exclusion lane** (impersonal, general analysis).

---

## 2. Request lifecycle

### 2.1 Sell side (customer → jim)

```
customer GET /research/fundamentals?ticker=AAPL
  │
  ├─ x402 PaymentMiddlewareASGI sees no payment → 402 PAYMENT REQUIRED
  │     (base64 `payment-required` header advertises: exact / network / USDC / amount)
  │
  ├─ customer signs an EIP-3009 USDC authorization, retries with `payment-signature`
  │
  ├─ middleware → facilitator /verify → handler runs → facilitator /settle
  │
  └─ 200 OK + `payment-response` header (settlement receipt)
        body = the cited memo + citations + sourcing verdict + economics
```

The handler is [seller/app.py](../src/jim/seller/app.py); the payment cycle is
x402 V2 (see §8).

### 2.2 Buy side (jim → upstream, inside one request)

While serving a `token` request, jim itself becomes an x402 **buyer**: the
research engine needs on-chain data, so it pays The Graph (or the testnet mock)
for it. That nested purchase is the `cost_in` half of the margin equation.

```
run_research("WETH", product="token")
  └─ GraphSource.gather
       └─ procure():  cache? → budget.propose() → buyer.pay() (x402) → store.record_purchase()
```

So a single customer request can contain a full second x402 settlement. The
customer's payment (`price_out`) funds a run during which jim spends `cost_in`.

---

## 3. The research engine (LangGraph)

[engine.py](../src/jim/research/engine.py) compiles a `StateGraph` once at import.
Nodes are async; state is a `TypedDict` carried (in memory) through the run.

```
        ┌─────────── gather ───────────┐
        │ (source.gather → Snapshot,   │  error → finalize
        │  cost_in, cache_hit)         │
        └──────────────┬───────────────┘
                       │ ok
              enable_debate? ──no──┐
                       │ yes       │
                    debate         │   bull ∥ bear → judge  (adversarial review)
                       │           │
                       └────► synthesize ◄───────────┐  (LLM writes cited memo)
                                  │                   │
                                gate                  │  gate fails &
                          (deterministic)             │  attempts < max
                          pass │   fail ──────────────┘
                               ▼            fail & exhausted → finalize (rejected)
                             judge  (LLM faithfulness, optional)
                               │
                             END → record economics + insight, return ResearchResult
```

**Why a fixed graph instead of an LLM agent loop?** Three reasons:

1. **Provability.** The gate must run on *every* output, deterministically. A
   free-form agent that "decides" when to verify can skip verification.
2. **Cost control.** Data purchases go through a hard budget cap, not model
   discretion (§6.1). The model can *want* to buy; only code can spend.
3. **Debuggability & evals.** A fixed topology gives stable trace shapes and a
   meaningful A/B (debate on vs off) — see §11.

The model is used where judgment helps (writing prose, arguing bull/bear,
scoring faithfulness) and nowhere it could compromise the invariant.

### Nodes

| Node | File | Role | Model? |
|---|---|---|---|
| `gather` | source `.gather()` | identifier → cited `Snapshot` (+ buy upstream) | no |
| `debate` | [debate.py](../src/jim/research/debate.py) | bull ∥ bear → judge verdict | yes |
| `synthesize` | [synthesize.py](../src/jim/research/synthesize.py) | facts (+verdict) → cited memo | yes |
| `gate` | [gate.py](../src/jim/research/gate.py) | verify every figure is sourced | **no** |
| `judge` | [judge.py](../src/jim/research/judge.py) | faithfulness score, fail-closed | yes |
| `finalize` | inline | set terminal status | no |

The `gate → synthesize` back-edge is the self-repair loop: a gate failure feeds
its violations back as `feedback`, and the synthesizer rewrites (bounded by
`research_max_attempts`). If it still fails, the run ends `rejected` — never `ok`.

---

## 4. The data model — `Fact` and `Snapshot`

[facts.py](../src/jim/research/facts.py). Every number jim can publish is a `Fact`:

```python
Fact(id="C1", label="Revenue", value=394_328_000_000, unit="USD",
     source_label="SEC EDGAR", accession="0000320193-25-...", form="10-K",
     fiscal_year=2025, fiscal_period="FY", filed="2025-07-30", source_url="https://...")
```

- **Primary facts** carry provenance (`accession`/`source_url`) to a real
  document — that's the citation anchor.
- **Derived facts** (`is_derived=True`) are computed in code (margins, ratios,
  Market Cap) and cite their inputs by id in `derived_from` — so a derived number
  is still fully traceable, just transitively.

Units (`USD`, `USD/shares`, `shares`, `%`, `x`, `count`, `index`) tell the gate
how to interpret a figure and the renderer how to format it. A `Snapshot` is the
set of facts for one entity, with `facts_block()` (the model-readable table fed
to the LLM) and `citations_block()` (the human-readable provenance list).

`[C#]` ids are assigned sequentially as facts are gathered, then continued by the
price-enrichment and derived-metric passes, so the ids are stable within a run.

---

## 5. Sources — the `Source` interface

[sources/](../src/jim/sources/). A source turns an identifier into a `Snapshot`,
reporting `cost_in_usd` and `cache_hit`:

```python
class Source(Protocol):
    name: str
    is_paid: bool
    async def gather(self, identifier, *, budget, store) -> GatherResult: ...
```

| Source | Upstream | Paid? | Citation anchor |
|---|---|---|---|
| `FundamentalsSource` | SEC EDGAR + Yahoo | free | filing accession / price observation |
| `GraphSource` | The Graph (Uniswap v3, **multi-chain**) | **x402** | subgraph query (chain-qualified) |
| `MacroSource` | US gov (Fed / BLS / Treasury) | free | gov release date |
| `EdgarSource` | SEC EDGAR only | free | filing accession |

### 5.1 EDGAR ([edgar.py](../src/jim/research/edgar.py))

Public-domain XBRL "company facts" (one request per company). A curated concept
list maps GAAP tags (with fallbacks) to labels and picks the latest annual 10-K
value, keeping its accession. Shares outstanding comes from the `dei` taxonomy.
Derived metrics (margins, ROE/ROA, leverage, EBITDA, YoY growth) are computed in
[facts.py](../src/jim/research/facts.py) `compute_derived`.

### 5.2 Market enrichment ([yahoo.py](../src/jim/sources/yahoo.py))

`FundamentalsSource` enriches EDGAR with Yahoo's free chart API: latest price,
52-week range, volume, and a daily close series. From these it computes
**technicals** (SMA50/200, RSI, MACD via [indicators.py](../src/jim/research/indicators.py))
and **market-derived metrics** (Market Cap, P/E TTM, P/B, dividend yield) that
cite *both* the price and the EDGAR input. Best-effort: a feed failure degrades
to EDGAR-only rather than failing the run.

> **Provenance caveat.** EDGAR is public domain; market price feeds carry ToS.
> For a licensed deployment, swap Yahoo for a redistributable market-data vendor
> — the `Fact`/citation model is unchanged.

### 5.3 The Graph ([thegraph.py](../src/jim/sources/thegraph.py))

A **paid** x402 source. It POSTs a GraphQL query for a token's Uniswap-v3 entity
and parses price / TVL / volume / supply into cited facts. One code path, two
wirings selected by `GRAPH_LIVE`:

- `GRAPH_LIVE=true` → `gateway.thegraph.com` on **Base mainnet** (real USDC, ~$0.01/query).
- `GRAPH_LIVE=false` → local `/mock-graph` vendor on **Base Sepolia** (free testnet USDC).

The mock ([vendor/mock_graph.py](../src/jim/vendor/mock_graph.py)) returns the
*identical* JSON shape, so the parser is the same either way. (The real gateway
only settles on mainnet — there is no testnet endpoint, confirmed by probing —
which is why the mock exists for verification.)

**Multi-chain (ADR-0007).** All Uniswap-v3 deployments share one schema, so a
`ChainSpec` registry (Ethereum, Base, Arbitrum, Polygon) reuses one query + one
parser — only the subgraph id, the per-chain token map, and the citation label
change. The identifier carries the chain: `WETH`, `WETH:base`, `0x…:arbitrum`
(default Ethereum). The cache key is chain-qualified so cross-chain data never
collides, and settlement is unchanged (x402 on Base). On-chain data is public and
The Graph charges a service fee (not a data license), so derived insight is freely
redistributable. Aerodrome-on-Base (a Solidly fork, different schema) is a registry
entry awaiting its own parser.

### 5.4 Macro ([macro.py](../src/jim/sources/macro.py))

A **free, public-domain** source (product `macro`) that keeps macro context inside
jim's invariant: Fed funds (NY Fed EFFR), CPI (BLS), 2y/10y Treasury yields (U.S.
Treasury), and a derived 2s10s spread — each figure cited to a **US-government
primary source**. Deliberately **not FRED**: FRED's API ToS forbids
caching/redistribution, whereas the underlying agency data is public domain
(17 U.S.C. §105), so jim goes straight to the agencies. Best-effort like the Yahoo
enrichment (a down upstream drops its reading, never fails the run). Free data → a
pure-margin product. Equity index levels (S&P 500, sector benchmarks) are
intentionally absent — proprietary, with no public-domain path. See ADR-0007 for
the economics and what was refused (proprietary transcripts / EPS estimates).

### 5.5 Resilience ([net/resilience.py](../src/jim/net/resilience.py)) — Phase 6

jim already degrades well by *absence* (no key → deterministic fallback, no DB →
memory store, no Yahoo → EDGAR-only); `resilient_call` adds degradation by
*failure*. Every free upstream request (EDGAR ticker map + companyfacts, Yahoo
chart, the three macro agencies) runs under one small helper: a wall-clock
timeout per attempt, bounded retries with exponential backoff + jitter, and a
per-host circuit breaker (open after N consecutive transport failures → fail
fast with `CircuitOpen`; one half-open probe after the cooldown). Only
transport-level failures and timeouts retry — HTTP 4xx/5xx are semantic and stay
with the sources' existing handling, so `EdgarError` and the best-effort `None`/
drop-the-reading paths surface exactly as before. Knobs live in `Settings`
(`RESILIENCE_*`); like tracing, the wrapper is invisible when upstreams are
healthy (one request per call).

### 6.1 Propose / dispose ([base.py](../src/jim/sources/base.py) `procure`, [budget.py](../src/jim/research/budget.py))

A paid fetch never spends freely. The flow is:

```
procure():
  cache hit?  ───────────────► return cached payload, cost_in = 0   (repackaged sale)
  budget.propose(estimate) ──► denied → raise BudgetExceeded
  buyer.pay(max_price=cap)  ──► pre-flight reads the REAL advertised 402 price;
                                over cap → PriceCapExceeded (never settles)
                            └─► otherwise settle on-chain
  budget.commit(actual)
  store.record_purchase()   ──► cache for next time (TTL)
```

`BudgetCap` holds a **hard per-query ceiling** on data spend. The source
*proposes* (it reasons about cost-vs-value); the budget *disposes* (code enforces
the ceiling). This is the same split as the gate: model wants, code decides.

**The dynamic-price guard (ADR-0007).** The x402 price is set by the seller in the
402 header and is *not* pre-published. `propose()` only checks the *estimate*; the
guard checks what the seller *actually* advertises. `procure` passes the remaining
ceiling as `max_price_usd`, so the unpaid pre-flight refuses an over-cap price
*before* any settlement (`PriceCapExceeded`, surfaced as `BudgetExceeded`). This is
what keeps jim from overpaying on mainnet, where the price is real USDC.
`scripts/graph_probe.py` is the pre-cutover audit: it decodes the live price and
PASS/FAILs it against the same ceiling.

### 6.2 Buy client ([buyer/client.py](../src/jim/buyer/client.py))

Wraps the V2 `x402HttpxClient`. `pay()` supports GET/POST+JSON (GraphQL) and
returns the body **plus `cost_in_usd` and the settlement tx hash**. It learns the
price with a cheap **unpaid pre-flight** (the seller returns 402 before doing any
work, so we read the advertised amount), then makes the paying request. The read
timeout is generous (a paid call buys *work*, often tens of seconds).

### 6.3 Store ([store/](../src/jim/store/))

Postgres + pgvector, with an in-memory fallback when `DATABASE_URL` is unset (so
tests and dev need no infra). One `Store` Protocol, two backends (`SqlStore`,
`MemoryStore`). Three tables:

- `data_purchases` — every bought datum (the cache; TTL-bounded reuse).
- `query_records` — per-query economics: `price_out`, `cost_in_data`,
  `cost_inference`, `margin`, `cache_hit`. This is what `/dashboard` reads.
- `insights` — derived memos with a `pgvector` embedding for semantic reuse /
  popular-ticker precompute. Embeddings are local + deterministic
  ([embed.py](../src/jim/store/embed.py): hashed char-trigrams), so a real model
  is a drop-in without schema change.

**Margin** = `price_out − cost_in_data − cost_inference`. Cache hits drive
`cost_in_data → 0`, so a repackaged sale of an already-bought datum is nearly all
margin — the "buy once, resell many" economics. The dashboard
([dashboard.py](../src/jim/dashboard.py)) aggregates this.

---

## 7. The sourcing gate (deep dive)

[gate.py](../src/jim/research/gate.py). For each sentence/bullet ("segment"):

1. Collect the `[C#]` ids cited in that segment. Any id not in the snapshot →
   **phantom citation** violation.
2. **Pass A** — regex-extract `$`-amounts, `%`, `x`-multiples, and comma-grouped
   numbers (ignoring digits inside `[C30]`).
3. **Pass B** — a bare decimal immediately before a citation (e.g. the `62.5` in
   "RSI is 62.5 [C30]"), skipping any that overlap a Pass-A match. This catches
   indicators with no `$`/`%`/`x` marker.
4. Each figure must match — within `max(2% relative, 0.05 absolute)` and a
   compatible unit — at least one fact cited *in the same segment*. Otherwise:
   **uncited** (no citation present) or **value mismatch** (cited, doesn't match).

A run **passes** iff there are zero violations. The result reports `coverage`
(`covered / figures`) and actionable feedback used for the retry. Because the
check is value-matching against fetched facts, a hallucinated number is
structurally impossible to pass — proven by the gate-regression eval (§11) and
[tests/test_gate.py](../tests/test_gate.py).

---

## 8. Payments — x402 V2 (deep dive)

jim pins `x402==2.12.*` and is V2 throughout. Things worth knowing:

- **Networks are CAIP-2**: Base Sepolia = `eip155:84532`, Base mainnet = `eip155:8453`.
- **The 402 body is empty**; requirements ride in a base64 **`payment-required`
  header** (v1 used `X-PAYMENT`). The challenge lists `accepts[]` with
  `scheme=exact`, network, `amount` (USDC base units, 6 decimals), `payTo`, asset.
- **`$`-priced routes auto-convert** to USDC base units (`$0.01` → `"10000"`).
- **Settlement** uses EIP-3009 (`transferWithAuthorization`); the receipt comes
  back in a `payment-response` header.
- **Seller**: `PaymentMiddlewareASGI` + `x402ResourceServer` + a facilitator
  (`https://x402.org/facilitator` on testnet). **Buyer**: `x402HttpxClient` with
  an `eth_account` signer.

Sell side and buy side are **independent legs** with their own network/wallet —
jim can sell on Sepolia while buying from The Graph on mainnet.

> **Custody.** Phase 0 uses a local `eth_account` key for minimum moving parts.
> Coinbase CDP MPC server wallets are the production-custody upgrade and slot in
> behind the same `address`/signer interface in [wallet/](../src/jim/wallet/).

---

## 9. Tools, "function tools", and MCP

This deserves a direct answer because jim's design is a deliberate contrast to a
"give an LLM a bag of tools and let it loop" agent.

### 9.1 jim's "tools" are deterministic capabilities, not model-chosen calls

The capabilities jim composes — fetch EDGAR, fetch a price, buy from The Graph,
check sourcing, embed, record margin — are ordinary async functions wired into
the LangGraph topology in a fixed order. The LLM does **not** choose which to
call or when. This is intentional (§3): the invariant and the budget must hold
regardless of what the model would prefer to do. So jim has **function-level
tools** (the source `.gather()`, `procure()`, `check_sourcing()`, `pay()`,
`compute_indicators()`), but no model-driven tool-calling on the critical path.

Where the model *does* have latitude, it's sandboxed: the synthesizer can write
any prose it likes, but the gate vetoes any unsourced number; the bull/bear
agents can argue anything, but their numbers must still be facts.

### 9.2 Could jim use LLM function-calling? Where it would fit

The natural place is **inside a node**, never as the orchestrator. For example, a
future `gather` could let the model emit a structured "data request" (which
metrics it wants) that deterministic code then fulfills and budget-checks — the
model proposing a *shopping list*, code disposing the *purchases*. The Anthropic
SDK's tool-use (with `StructuredOutput`-style schemas) is the mechanism; the
propose/dispose boundary stays in code. The judge and debate could likewise emit
structured verdicts via a tool schema instead of parsed JSON.

### 9.3 MCP (Model Context Protocol) — two directions

jim doesn't ship an MCP server today, but the architecture is MCP-ready in both
directions, and x402 has first-class MCP support (`mcp>=1.0` is a transitive dep
via the x402 extras):

- **jim as an MCP server (sell side).** Expose `research_fundamentals` /
  `research_token` as MCP tools so any MCP-speaking agent (Claude Desktop, an
  IDE, another service) can call jim. The x402 payment becomes the auth: the MCP
  tool call triggers the 402 → pay → settle cycle under the hood. **Implemented in
  Phase 5** ([marketplace/mcp_server.py](../src/jim/marketplace/mcp_server.py),
  `jim-mcp`): each catalogued product becomes an x402-gated FastMCP tool via
  `x402.mcp.create_payment_wrapper`, and the handler is just another caller of
  `run_research`, so the same sourcing gate + budget apply. See §15.

- **jim as an MCP client (buy side).** A new `Source` could gather facts from an
  MCP data server the same way `GraphSource` gathers from an HTTP x402 endpoint —
  the `Source` Protocol is transport-agnostic. An x402-gated MCP tool would route
  through the same `procure()` → budget → cache path.

The key design point: MCP is a **transport for tools**, and jim already isolates
"where data/prose comes from" (sources, synthesizer) from "what is allowed to
ship" (the gate, the budget). MCP plugs into the former without touching the
latter.

---

## 10. Observability

[obs/tracing.py](../src/jim/obs/tracing.py). Each run opens a Langfuse trace and
records scores: `sourcing_coverage`, `gate_passed`, `faithfulness`,
`data_cost_usd`, `margin_usd`, token usage. Entirely **best-effort** — with no
`LANGFUSE_PUBLIC_KEY`/`SECRET_KEY` it's a no-op and the pipeline is identical.
Every Langfuse call is guarded so tracing can never break a run.

---

## 11. Evals

[eval/](../src/jim/eval/), run via `jim-eval`.

- **Gate regression** (offline, no key): planted memos the gate *must* reject
  (dollar/RSI hallucinations, uncited, phantom) plus a clean memo it must pass.
  This is the merge gate — deterministic and always runnable.
- **Lift comparison** (live): each held-out ticker is run **single-pass** and
  **with debate**; the harness compares gate pass-rate, coverage, faithfulness,
  and fact count, and logs aggregates to Langfuse. This answers Phase 3's
  question — does adversarial review measurably help vs. the Phase-1 single pass?

---

## 12. Monitors — continuous research (Phase 4)

[monitors/](../src/jim/monitors/). A **monitor** turns a one-shot research call
into a standing one: re-run product P on identifier I every N seconds, and push a
cited update **only when something material changes**. It reuses the entire
Phase 1–3 machinery (sources, budget, cache, sourcing gate) and adds five
deterministic pieces around it.

### 12.1 Lifecycle

```
run_monitor_once(monitor):
  gather  ── source.gather() → fresh Snapshot (paid sources hit budget + cache)
    │
  diff    ── diff_snapshots(baseline, snapshot)   (deterministic deltas)
    │           first run? → store baseline, status=baseline, DONE (no spend)
  crew    ── evaluate_all(triggers, diff)          (pure-function watchers)
    │
  gate    ── assess(signals, floor, cooldown)      (the MATERIALITY gate)
    │           not material? → status=quiet, DONE  ($0 inference, no push)
    │ material
  write   ── synthesize_update() → sourcing gate + impersonal guard
    │           (LLM optional; deterministic gate-safe fallback with no key)
  push    ── console / HMAC-signed webhook  (+ always-on pull feed)
    │
  record  ── roll baseline forward, reschedule next_run_at, persist run + economics
```

The split is the same invariant as the rest of jim (§1, §3): **deterministic code
decides whether to act; the model only writes once code says there's news.** The
materiality gate is to "should we speak?" what the sourcing gate is to "is this
number true?" — see [ADR-0001](adr/0001-deterministic-materiality-gate.md).

### 12.2 The diff and the crew

[diff.py](../src/jim/monitors/diff.py) compares a monitor's compact stored
*baseline* (last facts seen) to the fresh snapshot: per-label value deltas
(abs + %), newly-appeared / removed metrics, new SEC accessions, and whether the
reporting date advanced. No thresholds — pure arithmetic.

[triggers.py](../src/jim/monitors/triggers.py) is the "motley crew": a registry
of pure functions, each reading the diff and emitting cited
[`Signal`](../src/jim/monitors/models.py)s — `price_move` (≥ X%), `metric_change`
(≥ X% or ≥ abs), `threshold` (direction-aware crossings like RSI 70/30 that fire
only on the *cross*, not while parked past the line), `ma_cross` (50/200
golden/death), `new_filing`. Each signal carries the `[C#]` ids that back it, so
an update can cite them and the gate can verify them. A watcher is a pure
function added to the registry — no model, no I/O.

### 12.3 The materiality gate, cooldown, and economics

[materiality.py](../src/jim/monitors/materiality.py) `assess()` filters signals
by a **severity floor** (`info < notable < critical`) and a **per-signal
cooldown** (don't re-alert the same key within a window), and reports
`material`. Because this is deterministic, *most polls are quiet and cost
nothing*: a quiet run pays only for data (usually $0 via the cache) and **$0
inference**. The dashboard surfaces this as `inference_saved_usd` — the estimated
inference the gate avoided by not writing on quiet polls. This is the monitoring
analog of "buy a datum once, resell many" (§6.3): *write only when there's news.*

### 12.4 The update — gated, impersonal, key-optional

[update.py](../src/jim/monitors/update.py) writes a short "what changed" note over
the published signals + current facts. It must pass **both** the sourcing gate
(§7) and a deterministic **impersonal guard**
([impersonal.py](../src/jim/monitors/impersonal.py): rejects second-person
address, advice, buy/sell/hold, price targets — the "stays general, not
personalized" rule, enforced not just prompted). A failure retries with feedback,
then falls back to a memo built straight from each signal's cited fact — gate-safe
*by construction*. So, exactly like the sourcing gate, **monitors run with no API
key**; the key only buys nicer prose.

### 12.5 Delivery and the scheduler

[notify.py](../src/jim/monitors/notify.py) delivers the impersonal, cited payload
to external channels — `console` and `webhook:<url>`. Webhook POSTs carry three
headers: `X-Jim-Timestamp` (unix seconds), `X-Jim-Nonce` (uuid4 hex), and
`X-Jim-Signature: sha256=…` — an HMAC over `f"{timestamp}.{nonce}." + body`, so
tampering with any of the three breaks verification and a captured delivery
can't be replayed. Subscribers verify with `verify_delivery()` (constant-time
compare, staleness window, optional nonce dedup). The store is always the system
of record, so a pull *feed* exists regardless of channels. Delivery is
best-effort: a failing channel is logged, not fatal.

[scheduler.py](../src/jim/monitors/scheduler.py) is a dependency-free asyncio
loop: each tick asks the store for **due** monitors (`next_run_at <= now`), runs
them under a concurrency semaphore, and reschedules. All durable state
(`next_run_at`, baseline, cooldowns) lives in the store, so a restart resumes;
`run_monitor_once` is the isolated unit of work, so APScheduler/Temporal is a
one-file swap if fan-out/exactly-once ever demands it — see
[ADR-0002](adr/0002-lightweight-asyncio-scheduler.md). It runs standalone
(`jim-monitor serve`) or in-process in the seller (`MONITOR_AUTOSTART`).

### 12.6 Natural-language monitors (propose / dispose)

[nl.py](../src/jim/monitors/nl.py) turns *"watch AAPL for big earnings moves and
overbought RSI"* into validated triggers. With a key, the model **proposes** a
structured spec via tool-use; with no key, a deterministic keyword parser does
the same. Either way `validate_triggers` **disposes** — every proposed trigger is
checked against the real registry and its thresholds clamped before it can run.
This is exactly the model-proposes/code-disposes boundary §9.2 anticipated.

---

## 13. Configuration & module map

All config is env-driven via [config.py](../src/jim/config.py) (`pydantic-settings`);
see [.env.example](../.env.example). Highlights: `NETWORK`, `FACILITATOR_URL`,
`EVM_ADDRESS`/`EVM_PRIVATE_KEY`, `ANTHROPIC_API_KEY`, `DATABASE_URL`,
`GRAPH_LIVE`, `PER_QUERY_BUDGET_USD`, `ENABLE_DEBATE`, `ENABLE_PRICES`,
`MONITOR_*` (interval, cooldown, thresholds, webhook secret, autostart).

```
src/jim/
  config.py              settings (network, wallet, models, db, sources, budgets, monitors)
  wallet/                local eth_account wallet (+ CLI)
  buyer/client.py        x402 buy client (pay → cost_in + tx)
  seller/app.py          FastAPI: /ping, /research/*, /dashboard, /mock-graph, /monitors*
  research/
    facts.py             Fact/Snapshot, derived-metric computation
    edgar.py             SEC EDGAR client (free, public-domain)
    indicators.py        SMA/EMA/RSI/MACD (pure)
    gate.py              the provable sourcing gate (deterministic)
    synthesize.py        Anthropic synthesizer (human/agent modes)
    debate.py            bull / bear / judge
    judge.py             faithfulness judge
    budget.py            per-query budget cap (propose/dispose)
    products.py          product registry (fundamentals, token)
    cost.py              token usage → USD
    engine.py            LangGraph pipeline + run_research
  sources/               Source interface + EDGAR/Yahoo/Graph/peer + procurement
    peer.py              Phase 7: PeerSource (buy facts from a peer agent over x402)
                         + CompositeSource (merge peer signals into a product snapshot)
  net/resilience.py      Phase 6: timeout + retries + per-host circuit breaker for
                         every free upstream fetch (EDGAR, Yahoo, macro)
  interop/               Phase 7: the seam between agents
    callchain.py         X-Jim-Call-Chain: loop + depth refusal before any payment
    trust.py             per-source trust = gate pass-rate (attribution + Laplace score)
  monitors/              Phase 4: diff, triggers (crew), materiality gate, update,
                         impersonal guard, notify, engine, scheduler, nl, create, CLI
  marketplace/           Phase 5: catalog (+ Bazaar extensions), pricing tiers,
                         discovery manifest, agent card, MCP server, human UI, system
                         map, mainnet preflight, CLI (jim-market / jim-map / jim-mcp)
  vendor/                testnet stand-ins: mock_graph (The Graph), mock_peer (a peer agent)
  store/                 Postgres+pgvector cache + margin ledger + monitors + trust
                         events (+ embed, CLI)
  obs/tracing.py         Langfuse (best-effort)
  eval/                  gate regression + debate-vs-single-pass lift
  dashboard.py           margin + monitor-economics dashboard
```

---

## 14. Key design decisions

| Decision | Why |
|---|---|
| Deterministic gate, no LLM | The trust property must be reproducible/auditable. |
| Fixed LangGraph topology over agent loop | Guarantees verification + budget run every time. |
| Propose/dispose for spend | Model can want; only code can spend (no runaway buys). |
| Local key (not CDP) for now | Fewest moving parts to prove the rails; CDP slots in later. |
| Native judge over DeepEval in Phase 1 | Ships now; DeepEval becomes the Phase-3 regression harness. |
| Real Graph (mainnet) + testnet mock | Graph is mainnet-only; mock proves economics for free. |
| Yahoo for prices (flagged) | Free + keyless to demo technicals; licensed feed swaps in. |
| pgvector with local embeddings | Proves the vector path with zero external API; real model drops in. |
| Deterministic materiality gate, no LLM ([ADR-0001](adr/0001-deterministic-materiality-gate.md)) | Alerts must be reproducible; quiet polls must cost $0 inference. |
| Lightweight asyncio scheduler ([ADR-0002](adr/0002-lightweight-asyncio-scheduler.md)) | Durability lives in the store; APScheduler/Temporal is a one-file swap later. |
| Gate-safe deterministic update fallback | Monitors work with no API key, like the sourcing gate; the key only buys prose. |
| Impersonal guard (deterministic) | "Stays general, not personalized" is enforced, not just prompted. |
| Native x402 Bazaar discovery + one catalog ([ADR-0003](adr/0003-bazaar-discovery.md)) | Ride the ecosystem's index rail, not a bespoke registry; one source of truth so nothing drifts. |
| Ref-free advertised output schema ([ADR-0003](adr/0003-bazaar-discovery.md)) | A nested `$ref` dangles and indexers reject the listing; inline keeps it valid anywhere. |
| Read-only mainnet preflight (no auto-spend) ([ADR-0004](adr/0004-mainnet-cutover-and-ui-self-pay.md)) | The one irreversible step gets a dry-run that can't fire the gun. |
| UI self-pay when funded, else preview ([ADR-0004](adr/0004-mainnet-cutover-and-ui-self-pay.md)) | A wallet-less visitor still exercises the real rail; honest about preview vs paid. |
| Rejected research is refused, never billed ([ADR-0008](adr/0008-agent-economy-trust-callchain-billing.md)) | The x402 middleware only settles 2xx; a 502 refusal cancels the verified payment. |
| The gate as composition firewall ([ADR-0008](adr/0008-agent-economy-trust-callchain-billing.md)) | Peer facts face the same value-match as EDGAR facts — buy from anyone, ship only what verifies. |
| Trust = gate pass-rate, not reviews ([ADR-0008](adr/0008-agent-economy-trust-callchain-billing.md)) | Reputation computed from outcomes jim observed itself; refuses to keep paying unverifiable peers. |
| Call-chain refusal before the paywall ([ADR-0008](adr/0008-agent-economy-trust-callchain-billing.md)) | Payment loops and runaway depth are graph properties; refuse at 409 before money moves. |

---

## 15. Marketplace, discovery & MCP (Phase 5)

[marketplace/](../src/jim/marketplace/). Phase 5 makes jim *findable and payable
by other agents*, publishes a price schedule, exposes jim over MCP, lets a human
buy through a browser, renders the system as a whole, and guards the cutover to
real money. See [SYSTEM_MAP.md](SYSTEM_MAP.md) for the diagrams and
[ADR-0003](adr/0003-bazaar-discovery.md)/[ADR-0004](adr/0004-mainnet-cutover-and-ui-self-pay.md).

### 15.1 One catalog, many faces

[catalog.py](../src/jim/marketplace/catalog.py) is the single source of truth: each
product becomes a `Listing` fusing static framing (title, tags, the external
upstream) with live facts from the product registry (price, source, paid-ness) and
a compact, **ref-free** output JSON schema mirroring `ResearchResponse`. The
routes, the discovery manifest, the MCP tools, the human UI, and the system map
all read from it, so a price or schema change propagates everywhere at once.

### 15.2 Discovery — index + pull

- **Index (automatic):** each paid `RouteConfig` carries an x402
  `declare_discovery_extension` (input/output schemas + example) plus
  `service_name`/`tags`/`icon_url`. The extension rides on the 402 challenge, so
  the **first successful settlement** hands our discovery card to a
  Bazaar-speaking facilitator — no manual submission.
- **Pull (deterministic):** [`GET /.well-known/x402`](../src/jim/marketplace/discovery.py)
  returns a byte-stable manifest (identity, network, USDC asset, pay-to, every
  product's call shape + price, the MCP endpoint). `GET /catalog` and
  `GET /pricing` expose the same data.

### 15.3 Pricing tiers

[pricing.py](../src/jim/marketplace/pricing.py) publishes a deterministic schedule
— `oneshot` / `agent` (a machine-buyer discount) / `bundle` / `monitor` — derived
from the prices the system already charges (config-driven), not invented.

### 15.4 jim as an MCP server

[mcp_server.py](../src/jim/marketplace/mcp_server.py) (`jim-mcp`) registers each
product as an **x402-gated FastMCP tool** via `x402.mcp.create_payment_wrapper`,
reusing the same facilitator + EXACT-EVM scheme as the HTTP seller, so MCP and HTTP
settle identically. The tool handler is just another caller of `run_research` —
the sourcing gate and budget are unchanged. `mcp` is an optional extra; the tool
*surface* (`mcp_tool_catalog`) is pure and always importable for discovery + tests.

### 15.5 The human UI

[ui.py](../src/jim/marketplace/ui.py) (`GET /`) is a dependency-free storefront. Its
`POST /ui/checkout` **pays via x402 under the hood**: when a wallet is funded and
`UI_SETTLE_VIA_X402` is on, jim buys its own endpoint (a real settlement, no
visitor wallet needed); otherwise it runs the engine directly and labels the
result a preview. Same gate either way.

### 15.6 The live system map

[sysmap.py](../src/jim/marketplace/sysmap.py) (`jim-map`, `GET /map` · `/map.mmd` ·
`/map.json`) introspects the running config + catalog and emits a Mermaid graph of
the entire system — buyers → discovery → rails → seller → engine → trust gates →
sources → external tools → store → monitors → observability. Because it reads
config, it tracks reality: flip `GRAPH_LIVE` and the upstream repoints; set
`DATABASE_URL` and the store node flips to Postgres. The hand-drawn companion set
lives in [SYSTEM_MAP.md](SYSTEM_MAP.md).

### 15.7 Mainnet cutover, guarded

[mainnet.py](../src/jim/marketplace/mainnet.py) (`jim-market mainnet`,
`GET /mainnet/readiness`) is a **read-only** preflight: it grades network, pay-to,
buyer key, facilitator (a testnet facilitator on mainnet is a hard fail), prices vs
the facilitator minimum, the Graph buy leg, and — if `MAINNET_RPC_URL` is set —
on-chain ETH/USDC balances. It **never moves money**. The buy leg has been
mainnet-capable since Phase 2 (`GRAPH_LIVE`), so this is the sell-leg cutover plus
guardrails; the two legs stay independent (§8).

---

## 16. The agent economy (Phase 7) + billing invariant

Full rationale in [ADR-0008](adr/0008-agent-economy-trust-callchain-billing.md)
and [AGENT_INTEROP.md](AGENT_INTEROP.md); the shape:

### 16.1 Source-as-agent ([sources/peer.py](../src/jim/sources/peer.py))

`PeerSource` is a `Source` (§5) whose upstream is *another agent*: it buys a
facts payload (bare `facts` list or a jim-shaped `citations` list) over x402,
through the **same** `procure()` → budget → cache path as The Graph — so the
per-query ceiling, the dynamic-price cap (ADR-0007), and buy-once-resell-many
economics all apply unchanged. `CompositeSource` merges peer facts into the
primary product snapshot with renumbered citations and per-fact `origins`; a
failing peer degrades to a sourcing note in the response's cost block, never a
failed run. Peers are configuration (`PEER_SOURCES`), not code. The **sourcing
gate (§7) is the composition firewall**: a peer figure that doesn't match its
cited fact fails exactly like a self-hallucination, so composition never
launders an unverifiable claim into a memo.

### 16.2 Trust ([interop/trust.py](../src/jim/interop/trust.py), store `source_trust_events`)

After every gated run the engine attributes the verdict to the sources whose
facts it used: a pass credits all contributors; a failure debits only sources
whose facts appear in a violation's citations. Events append to the trust
ledger; the score is the Laplace-smoothed pass-rate ((ok+1)/(ok+fail+2) — a new
source starts at 0.5). The buy path refuses peers below `PEER_TRUST_FLOOR`
(after `PEER_TRUST_MIN_EVENTS`), and `/dashboard` surfaces the table. This is
jim's native reputation primitive: verification, not reviews.

### 16.3 Call-chain safety ([interop/callchain.py](../src/jim/interop/callchain.py))

Every buy stamps `X-Jim-Call-Chain` (the paying agents so far + jim). The
seller's **outermost** middleware refuses — 409, before the paywall verifies
anything — a chain that already contains jim's address (a payment loop) or one
at `CALL_CHAIN_MAX_DEPTH`; the buyer refuses to *extend* a chain past the same
ceiling. Deterministic, cooperative, and bounded to what jim controls: its own
spend and its own participation in cycles.

### 16.4 The billing invariant ([seller/app.py](../src/jim/seller/app.py) `_deliver_or_refuse`)

The x402 middleware settles only 2xx responses. A run the gates rejected is
therefore *refused*: HTTP 502 with structured diagnostics (the verified payment
is cancelled — the buyer keeps their money), the MCP tool raises, the UI
preview declines to render, and the engine books \$0 revenue so the margin
ledger shows the loss. "Paid but rejected" — our first mainnet settlement — is
no longer a reachable state.

### 16.5 The agent card ([marketplace/agentcard.py](../src/jim/marketplace/agentcard.py))

`GET /.well-known/agent-card.json` publishes an A2A-style card — skills derived
from the same catalog as everything else (§15.1), an x402 payment binding, and
jim's trust/call-chain contract — linked from the `/.well-known/x402` manifest.
MCP exposes *tools*; the card is what lets a peer *delegate a task*.

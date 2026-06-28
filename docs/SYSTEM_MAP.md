# jim — System Map

A visual tour of jim **as a whole**: how a request enters (over HTTP, MCP, or the
human UI), how it's discovered and paid for over x402, how the research engine
turns an identifier into a cited memo, where the deterministic trust gates sit,
which external tools the sources draw on, how the cache + margin ledger close the
economic loop, and how monitors run the whole thing continuously.

> These diagrams are hand-drawn for clarity. For a diagram of your **actual,
> live** configuration — current network, prices, which sources are paid, the
> store backend, feature flags — run `uv run jim-map` or open
> [`GET /map`](#the-live-map) on a running seller. See [the live map](#the-live-map).

Companion reading: [ARCHITECTURE.md](ARCHITECTURE.md) (the deep dive) and
[BUILD_PLAN.md](BUILD_PLAN.md) (the phases).

---

## 1. The whole system, one picture

Everything from buyers → discovery → payment rails → seller → engine → trust
gates → sources → external tools → store → monitors → observability.

```mermaid
flowchart LR
  subgraph buyers["① Buyers / clients"]
    mcpA["MCP agents<br/>(Claude, IDEs)"]
    httpA["HTTP / agent buyers<br/>(x402 clients)"]
    human["Human UI<br/>(/ — pays under the hood)"]
  end

  subgraph disc["② Discovery (Phase 5)"]
    cat["GET /catalog"]
    wk["GET /.well-known/x402<br/>(manifest)"]
    bz["Bazaar index<br/>(auto on 1st settle)"]
    mcpS["MCP server<br/>jim-mcp"]
  end

  subgraph pay["③ x402 payment rails"]
    mw["x402 middleware<br/>402 → verify → settle"]
    fac["Facilitator<br/>(testnet / CDP)"]
    usdc["USDC<br/>EIP-3009"]
  end

  subgraph sell["④ Seller — paid routes"]
    rf["GET /research/fundamentals"]
    rt["GET /research/token"]
  end

  subgraph eng["⑤ Research engine (LangGraph)"]
    gather["gather"]
    debate["debate<br/>bull ∥ bear → judge"]
    synth["synthesize<br/>(LLM memo)"]
    jf["faithfulness judge"]
  end

  subgraph trust["⑥ Deterministic trust gates (no LLM)"]
    gate["sourcing gate"]
    budget["budget cap<br/>propose/dispose"]
    imp["impersonal guard"]
  end

  subgraph src["⑦ Sources"]
    fsrc["FundamentalsSource<br/>(free)"]
    gsrc["GraphSource<br/>(PAID · x402)"]
  end

  subgraph ext["⑧ External tools / upstreams"]
    edgar["SEC EDGAR<br/>(public domain)"]
    yahoo["Yahoo charts<br/>(price/technicals)"]
    graph["The Graph / mock<br/>(Uniswap v3)"]
  end

  subgraph store["⑨ Store + margin ledger"]
    cache["cache (data_purchases)<br/>buy once · resell many"]
    ledger["margin ledger<br/>(query_records)"]
    insights["insights<br/>(pgvector)"]
  end

  subgraph mon["⑩ Monitors (Phase 4)"]
    sched["scheduler"]
    crew["trigger crew<br/>+ materiality gate"]
    notify["notify<br/>console · webhook · feed"]
  end

  subgraph obs["⑪ Observability"]
    lf["Langfuse<br/>(best-effort)"]
  end

  mcpA --> mcpS
  httpA --> wk
  httpA --> cat
  human --> rf
  mcpS --> mw
  cat --> bz
  wk --> bz
  bz -. "found you" .-> httpA

  mw --> fac --> usdc
  mw --> rf
  mw --> rt
  rf --> gather
  rt --> gather

  gather --> debate --> synth --> gate
  gate -- "fail → retry" --> synth
  gate -- "pass" --> jf

  gather --> fsrc --> edgar
  fsrc --> yahoo
  gather --> gsrc
  gsrc --> budget -- "dispose → buy (x402)" --> graph
  gsrc --> cache

  gather --> store
  jf --> ledger
  synth --> insights

  sched --> crew --> gather
  crew --> notify --> imp
  synth --> imp

  jf --> lf
  gate --> lf

  classDef trustcls fill:#fce4ec,stroke:#c2185b,color:#880e4f;
  class gate,budget,imp trustcls;
  classDef paycls fill:#fff3e0,stroke:#ef6c00,color:#e65100;
  class mw,fac,usdc paycls;
```

**How to read it.** A buyer arrives at ①, finds jim via ② (or just calls a known
URL), and pays through ③. The middleware only lets the request reach a ④ route
*after settlement*. The route runs the ⑤ engine, which writes prose but cannot
ship a number the ⑥ sourcing gate rejects. Data comes from ⑦ sources, which draw
on ⑧ external tools; paid data passes the ⑥ budget cap and is cached in ⑨ so the
*next* sale of the same datum is nearly pure margin. ⑩ monitors re-run the engine
on a cadence and only speak when the materiality gate says there's news. ⑪ traces
everything, best-effort.

---

## 2. Sell-side payment (customer → jim)

The x402 V2 cycle for one paid call.

```mermaid
sequenceDiagram
  autonumber
  participant C as Buyer (x402 client)
  participant M as x402 middleware
  participant F as Facilitator
  participant H as Route handler
  participant E as Research engine

  C->>M: GET /research/fundamentals?ticker=AAPL
  M-->>C: 402 PAYMENT REQUIRED<br/>(base64 payment-required header:<br/>exact · network · USDC · amount · accepts[]<br/>+ Bazaar discovery extension)
  C->>C: sign EIP-3009 USDC authorization
  C->>M: retry with payment-signature
  M->>F: /verify
  F-->>M: ok
  M->>H: invoke handler (settlement pending)
  H->>E: run_research(AAPL)
  E-->>H: cited memo + sourcing verdict + economics
  M->>F: /settle
  F-->>M: receipt (tx hash)
  M-->>C: 200 OK + payment-response header<br/>{ memo, citations, sourcing, cost }
```

---

## 3. Buy-side (jim → upstream, nested inside one request)

While serving a `token` request, jim becomes an x402 **buyer** — the `cost_in`
half of the margin equation. The budget *disposes*; the cache makes repeats free.

```mermaid
sequenceDiagram
  autonumber
  participant E as Engine (gather)
  participant G as GraphSource
  participant Ca as Cache (store)
  participant B as Budget cap
  participant By as Buy client (x402)
  participant TG as The Graph (or mock)

  E->>G: gather("WETH")
  G->>Ca: cached purchase?
  alt cache hit
    Ca-->>G: payload (cost_in = $0)
    Note over G,Ca: repackaged sale — nearly pure margin
  else miss
    G->>B: propose(estimate)
    alt over ceiling
      B-->>G: denied → BudgetExceeded
    else approved
      B-->>G: ok
      G->>By: pay(subgraph query)
      By->>TG: 402 → sign → settle → 200
      TG-->>By: Uniswap-v3 JSON + tx hash
      By-->>G: payload + cost_in + tx
      G->>Ca: record_purchase (TTL)
    end
  end
  G-->>E: Snapshot + cost_in + cache_hit
```

---

## 4. Marketplace discovery (Phase 5)

Two paths by which another agent finds jim and pays it — pull (manifest / MCP)
and index (Bazaar, automatic on first settlement). See
[ADR-0003](adr/0003-bazaar-discovery.md).

```mermaid
sequenceDiagram
  autonumber
  participant A as Another agent
  participant J as jim seller
  participant Fx as Bazaar facilitator

  rect rgb(237,231,246)
    note over A,J: Pull discovery
    A->>J: GET /.well-known/x402
    J-->>A: manifest (network, asset, pay_to,<br/>products + schemas + prices, MCP endpoint)
    A->>J: GET /catalog  (per-product call shapes)
    J-->>A: listings + input/output JSON schemas
  end

  rect rgb(232,245,233)
    note over A,Fx: Index discovery (zero manual submission)
    A->>J: GET /research/token (no payment)
    J-->>A: 402 + Bazaar discovery extension
    A->>J: pay → settle
    J->>Fx: settlement carries the discovery extension
    Fx->>Fx: auto-catalog jim (first settle = first index)
    A->>Fx: search Bazaar → finds jim later
  end
```

---

## 5. The trust boundary — model proposes, code disposes

jim's defining invariant, visualized. Three deterministic gates wrap every place
the model has latitude.

```mermaid
flowchart TD
  m["LLM (synthesizer / bull / bear / NL parser)"]:::model
  subgraph gates["Deterministic gates — no model, reproducible"]
    sg["Sourcing gate<br/>every figure must match a cited fact"]:::gate
    bc["Budget cap<br/>hard per-query data ceiling"]:::gate
    ig["Impersonal guard<br/>no advice / 2nd person / price targets"]:::gate
    mg["Materiality gate<br/>severity floor + cooldown"]:::gate
  end
  ship["Ships to the customer"]:::ok
  reject["Rejected / blocked"]:::bad

  m -- "writes prose" --> sg
  sg -- "0 violations" --> ship
  sg -- "any unsourced number" --> reject
  m -- "wants to buy data" --> bc
  bc -- "within ceiling" --> ship
  bc -- "over ceiling" --> reject
  m -- "monitor update text" --> ig
  ig -- "general analysis" --> ship
  ig -- "personalized / advice" --> reject
  m -- "should we alert?" --> mg
  mg -- "material" --> ship
  mg -- "quiet" --> reject

  classDef model fill:#e1f5fe,stroke:#0277bd,color:#01579b;
  classDef gate fill:#fce4ec,stroke:#c2185b,color:#880e4f;
  classDef ok fill:#e8f5e9,stroke:#2e7d32,color:#1b5e20;
  classDef bad fill:#ffebee,stroke:#c62828,color:#b71c1c;
```

---

## 6. Tools & MCP — both directions

jim has **function-level tools** wired into a fixed graph (not model-chosen), and
is MCP-ready in both directions. See [ARCHITECTURE §9](ARCHITECTURE.md#9-tools-function-tools-and-mcp).

```mermaid
flowchart LR
  subgraph sellmcp["jim as an MCP SERVER (sell side, Phase 5)"]
    client1["MCP client<br/>(Claude Desktop / IDE)"] --> tool["research_fundamentals<br/>research_token (x402-gated)"]
    tool --> rr["run_research → same gate + budget"]
  end
  subgraph buymcp["jim as an MCP CLIENT (buy side, future)"]
    newsrc["A new Source"] --> mcptool["x402-gated MCP data tool"]
    newsrc --> proc["procure() → budget → cache"]
  end
  note["MCP is a transport for tools — it plugs into<br/>'where the call comes from', never into<br/>'what is allowed to ship'."]:::note
  tool -.-> note
  mcptool -.-> note
  classDef note fill:#fffde7,stroke:#f9a825,color:#f57f17;
```

---

## 7. Monitor lifecycle (Phase 4 recap)

A monitor turns a one-shot call into a standing one — deterministic detection,
LLM only when there's news.

```mermaid
flowchart TD
  start(["tick: monitor due"]) --> gather["gather → fresh Snapshot"]
  gather --> diff["diff vs baseline"]
  diff -->|first run| base["store baseline · status=baseline"]:::done
  diff --> crew["trigger crew (pure functions)"]
  crew --> matg{"materiality gate<br/>severity floor + cooldown"}
  matg -->|quiet| quiet["status=quiet<br/>$0 inference, no push"]:::done
  matg -->|material| write["synthesize update"]
  write --> sg["sourcing gate + impersonal guard"]
  sg -->|fail| fallback["gate-safe fallback memo"]
  sg -->|pass| push
  fallback --> push["push: console · HMAC webhook · feed"]:::ok
  push --> rec["roll baseline · reschedule · record economics"]:::done
  classDef done fill:#eceff1,stroke:#607d8b,color:#263238;
  classDef ok fill:#e8f5e9,stroke:#2e7d32,color:#1b5e20;
```

---

## 8. The build, phase by phase

```mermaid
flowchart LR
  p0["Phase 0<br/>payment rail"] --> p1["Phase 1<br/>cited EDGAR + sourcing gate"]
  p1 --> p2["Phase 2<br/>buy side + margin engine"]
  p2 --> p3["Phase 3<br/>debate + metrics + evals"]
  p3 --> p4["Phase 4<br/>continuous monitors"]
  p4 --> p5["Phase 5<br/>marketplace · discovery · MCP · mainnet"]
  classDef done fill:#e8f5e9,stroke:#2e7d32,color:#1b5e20;
  class p0,p1,p2,p3,p4,p5 done;
```

---

## The live map

The diagrams above are static. To see **your** running system — the actual
network, prices, paid sources, store backend, and feature flags — generate one:

```bash
uv run jim-map                       # Mermaid (paste into any renderer / GitHub)
uv run jim-map --format html -o map.html   # self-contained page (mermaid.js)
uv run jim-map --format json         # the raw node/edge graph

# Or, from a running seller:
#   GET /map        → the rendered page in your browser
#   GET /map.mmd    → raw Mermaid source
#   GET /map.json   → the structured graph
```

Because `jim-map` reads config, it changes when you do: flip `GRAPH_LIVE=true`
and the token upstream repoints from the mock to Base mainnet; set `DATABASE_URL`
and the store node switches from in-memory to Postgres+pgvector; turn off
`ENABLE_DEBATE` and the debate node disappears. The map is the system telling you
what it currently is — not what a diagram once claimed it was.

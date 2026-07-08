# Jim Agent — Command Reference

## First-time setup

```bash
uv sync --extra dev              # install deps
cp .env.example .env             # then fill in secrets (see Key env vars below)
uv run jim-wallet new            # generate wallet → paste keys into .env
docker compose up -d             # start postgres
uv run jim-initdb                # create tables
```

Testnet faucets (Base Sepolia):
- ETH  → https://www.alchemy.com/faucets/base-sepolia
- USDC → https://faucet.circle.com  (select Base Sepolia)

---

## Dev stack

```bash
./dev.sh                         # postgres + seller (http://localhost:4021)
./dev.sh --monitors              # + monitor scheduler
./dev.sh --mcp                   # + MCP server on :4022
./dev.sh --no-db                 # skip docker (already running)
```

---

## Seller

```bash
uv run jim-seller                # http://localhost:4021

# Useful endpoints
GET /healthz                     # liveness check
GET /catalog                     # product list + prices
GET /dashboard                   # economics dashboard (margin, cache, inference cost)
GET /admin                       # settlement audit (payments, buyer addresses, tx hashes)
GET /admin/audit                 # same as JSON
```

---

## Research (CLI — no seller required)

```bash
# Equities (EDGAR + Yahoo Finance)
uv run jim-research AAPL
uv run jim-research AAPL --mode agent        # terse, metric-dense
uv run jim-research AAPL --json              # machine-readable
uv run jim-research AAPL --no-cache          # bypass memo cache
uv run jim-research AAPL --high-stakes       # Sonnet judge tier

# On-chain tokens (Uniswap v3 via The Graph)
uv run jim-research WETH --product token
uv run jim-research AERO:base --product token
uv run jim-research ARB:arbitrum --product token

# Macro (Fed funds / CPI / Treasury — free public-domain)
uv run jim-research US --product macro
```

---

## Monitors (requires Postgres)

```bash
uv run jim-monitor add AAPL --watch price:5 rsi:70/30 filing --every 1d
uv run jim-monitor add NVDA --describe "ping me on big earnings moves"
uv run jim-monitor add WETH --product token --watch price:8

uv run jim-monitor list                 # all monitors + status
uv run jim-monitor run <id>             # run one now (delivers on trigger)
uv run jim-monitor run-all              # run all currently due monitors
uv run jim-monitor preview <id>         # dry-run, no delivery
uv run jim-monitor serve                # scheduler loop (or MONITOR_AUTOSTART=true)
uv run jim-monitor feed                 # recent material updates
```

---

## Marketplace & discovery

```bash
uv run jim-market catalog               # products, routes, prices, sources
uv run jim-market pricing               # tiers + discounts
uv run jim-market manifest              # /.well-known/x402 discovery JSON
uv run jim-market mainnet               # mainnet readiness preflight (read-only)

uv run jim-map                          # Mermaid system diagram (stdout)
uv run jim-map --format html -o map.html  # self-contained HTML page

uv sync --extra mcp && uv run jim-mcp   # MCP server on :4022
```

---

## Wallet

```bash
uv run jim-wallet new                   # generate new keypair
uv run jim-wallet show                  # current address + testnet balances
```

---

## Dashboards

```bash
uv run jim-dashboard                    # economics: margin, cache hits, inference cost
uv run jim-admin                        # settlement audit: payments, buyer addresses, tx
```

---

## Testing & eval

```bash
uv run pytest                           # full offline hermetic suite
uv run jim-eval --gate-only             # gate regression (no ANTHROPIC_API_KEY needed)
uv run jim-eval                         # full eval + debate lift
uv run jim-eval AAPL MSFT              # restrict to specific tickers
```

---

## Demo scripts

```bash
uv run python scripts/ping_demo.py               # pay for /ping, print receipt
uv run python scripts/research_demo.py AAPL      # buy a fundamentals memo
uv run python scripts/graph_probe.py WETH        # audit The Graph price before mainnet
uv run python scripts/discover_demo.py           # test discovery manifest
uv run python scripts/precompute.py              # warm token cache (WETH/WBTC/UNI)
```

---

## Key env vars

| Variable | Default | Notes |
|---|---|---|
| `NETWORK` | `eip155:84532` | Testnet. Switch to `eip155:8453` for Base mainnet |
| `EVM_ADDRESS` | — | Wallet that *receives* payments |
| `EVM_PRIVATE_KEY` | — | Wallet that *sends* payments; generate with `jim-wallet new` |
| `ANTHROPIC_API_KEY` | — | Required for synthesis + judge |
| `DATABASE_URL` | (Postgres string) | Unset → in-memory (no persistence) |
| `GRAPH_LIVE` | `false` | `true` = real mainnet USDC spend on The Graph |
| `ENABLE_JUDGE` | `true` | Faithfulness judge per claim |
| `ENABLE_DEBATE` | `true` | Bull/bear/judge adversarial review |
| `MONITOR_AUTOSTART` | `false` | `true` = embed scheduler inside seller process |
| `PER_QUERY_BUDGET_USD` | `0.10` | Hard ceiling per data query (also the x402 price cap) |
| `MEMO_CACHE_ENABLED` | `true` | Reuse identical memos at $0 inference cost |

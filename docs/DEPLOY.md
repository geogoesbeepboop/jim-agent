# jim — Deploying to feed real users (the Horizon 1 runbook)

This is the operator's guide to putting jim on the public internet, written to
be *understood*, not just executed. Every step you run yourself; jim never
deploys itself or moves your keys.

---

## 1. What a live jim actually is

One container + one Postgres + one wallet. That's the whole topology.

```
                         ┌──────────────────────────────────────────────┐
   agents (x402/MCP) ───►│  jim-seller (the Docker image, port 4021)    │
   humans (browser)  ───►│    paid:  /research/* · /ping · mock vendors │
   Bazaar indexer    ───►│    free:  / · /proof · /catalog · /pricing   │
                         │           /.well-known/x402 · agent-card     │
                         │           /admin · /dashboard · /map         │
                         │    + monitor scheduler (MONITOR_AUTOSTART)   │
                         └───────┬──────────────────┬───────────────────┘
                                 │                  │
                    DATABASE_URL │                  │ outbound
                                 ▼                  ▼
                     Postgres + pgvector    facilitator (CDP) · EDGAR ·
                     (cache · ledgers ·     Yahoo · gov macro · The Graph ·
                      receipts · trust)     peer agents (all over HTTPS/x402)
```

Three things it's easy to get wrong about this picture:

- **The wallet is an env var, not a file.** `EVM_PRIVATE_KEY` is both the
  buy-side key (jim paying The Graph / peers) and the default webhook signing
  secret. It lives in the platform's secret store. `EVM_ADDRESS` is where
  revenue lands. If the host is compromised, the key is the blast radius —
  which is why the funded balance should stay small (see §6) and why the
  custody upgrade (Coinbase Agentic Wallets, MPC + session caps) is the next
  step after go-live, not before it.
- **Payments don't touch your server's trust.** The facilitator (Coinbase CDP
  on mainnet) verifies and settles USDC on Base; jim never holds customer
  funds in flight. A facilitator outage degrades to "can't sell right now,"
  never to "money lost."
- **"Feeding real users" starts with one settlement.** Each paid route's 402
  carries the Bazaar discovery extension, so the **first successful mainnet
  settlement auto-indexes jim on the x402 Bazaar** — that's the launch event.
  From then on agents can find jim without being told the URL. Humans get the
  same server at `/` (free preview + pay-with-wallet).

## 2. The platform decision (yours to make)

Requirements: a long-running process (the scheduler and monitors rule out
serverless), Postgres **with pgvector**, secrets management, TLS + a stable
domain (discovery URLs and the agent card embed it).

| Option | Shape | Why / why not |
|---|---|---|
| **Fly.io app + Supabase Postgres** (recommended) | container on Fly, managed PG w/ pgvector on Supabase | You already use Supabase; its Postgres ships pgvector. Fly gives a always-on machine, secrets, TLS, and painless `fly deploy` from the Dockerfile. Two dashboards, but each is the best of its kind. |
| **Railway (all-in-one)** | container + PG plugin in one project | Simplest single pane; pgvector supported. Slightly less control over regions/scaling. Great if two dashboards annoys you. |
| **A $6 VPS + docker compose** | you run everything | Cheapest, most instructive, most ops on you (TLS, backups, upgrades). Fine for a rehearsal, not what I'd feed real users with. |

Ballpark cost for any of these at demo-to-early-traffic scale: **$10–25/month**
plus cents of gas/facilitator fees.

## 3. Environment checklist

Everything is env-driven (`src/jim/config.py`, mirrored in `.env.example`).
The load-bearing set for production:

| Var | Value | Notes |
|---|---|---|
| `NETWORK` | `eip155:8453` | Base mainnet (testnet rehearsal: `eip155:84532`) |
| `EVM_ADDRESS` / `EVM_PRIVATE_KEY` | your wallet | **platform secret store only** — never in the image, never in git |
| `CDP_API_KEY_ID` / `CDP_API_KEY_SECRET` | CDP keys | mainnet facilitator auth (testnet uses x402.org, no auth) |
| `DATABASE_URL` | `postgresql+asyncpg://…` | Supabase/Railway PG; run `uv run jim-initdb` once against it |
| `ANTHROPIC_API_KEY` | your key | synthesis/judge; gather + gate run without it |
| `PUBLIC_BASE_URL` | `https://your.domain` | baked into the manifest, agent card, and Bazaar listing — set it before the first settlement |
| `SERVICE_NAME` / `SERVICE_DESCRIPTION` | your branding | what the Bazaar and the card display |
| `MONITOR_AUTOSTART` | `true` | run the monitor scheduler in-process |
| `UI_SETTLE_VIA_X402` | `false` | keep Preview free; humans pay via their own wallet |
| `GRAPH_LIVE` | `true` when ready | flips the token product's buy leg to the real Graph gateway (real USDC, price-capped) |
| `PER_QUERY_BUDGET_USD` | e.g. `0.10` | jim's hard per-run data-spend ceiling |
| `PEER_SOURCES` | optional | keep empty at launch; add peers deliberately |
| `MONITOR_WEBHOOK_SECRET` | a fresh secret | don't let it default to the wallet key in prod |

## 4. The runbook

```bash
# 0. Rehearse everything below on Base Sepolia first (NETWORK=eip155:84532,
#    faucet USDC, x402.org facilitator). Same steps, zero real money.

# 1. Database (Supabase): create project → enable pgvector → copy the
#    connection string (asyncpg form) → create the tables:
DATABASE_URL=postgresql+asyncpg://... uv run jim-initdb

# 2. Build + deploy the container (Fly shown; Railway is `railway up`):
fly launch --no-deploy            # generates fly.toml; internal_port = 4021
fly secrets set EVM_PRIVATE_KEY=... EVM_ADDRESS=... CDP_API_KEY_ID=... \
  CDP_API_KEY_SECRET=... ANTHROPIC_API_KEY=... DATABASE_URL=... \
  MONITOR_WEBHOOK_SECRET=...
fly deploy                        # uses the Dockerfile; /health is the check

# 3. Domain + discovery identity:
fly certs add jim.yourdomain.com
#    then set PUBLIC_BASE_URL=https://jim.yourdomain.com and redeploy —
#    the manifest, agent card, and Bazaar resource URLs all derive from it.

# 4. The gate before real money — run the preflight, fix every ✗:
uv run jim-market mainnet         # read-only; checks network, facilitator,
                                  # prices vs fees, buy leg, balances (RPC)

# 5. Fund the wallet SMALL (gas ETH + a few USDC on Base) — it's the buy-side
#    float, not a treasury. Top up as margin proves itself on /dashboard.

# 6. Launch = the first paid call. From any machine with a funded key:
uv run python scripts/research_demo.py AAPL
#    → the settlement auto-indexes jim on the x402 Bazaar (watch /admin)
#    → /proof starts filling in with real rows
```

## 5. Day-2 operations

- **Watch:** `/proof` (public), `/admin` (settlements), `/dashboard` (margin +
  trust), platform logs. The facilitator and EDGAR are the external
  dependencies that matter; the resilience wrapper + degradation paths cover
  blips, but a facilitator fee/tier change is a business event — check CDP
  announcements.
- **Backups:** Supabase/Railway PG backups on; the store *is* the audit trail.
- **Key hygiene:** the wallet key can't rotate without changing `pay_to`
  everywhere (Bazaar listing included) — another reason to keep the balance
  small and prioritize the Agentic Wallets upgrade (docs/NORTH_STAR.md,
  Horizon 2).
- **Scaling:** one container serves the demo-to-early-revenue phase easily
  (cache hits are cheap; the LLM is the bottleneck). The Phase 8 items
  (ROADMAP) are the pressure-relief valves when traffic demands them.

## 6. What can go wrong (and what already guards it)

| Risk | Guard |
|---|---|
| Buyer charged for bad research | never-bill-rejected invariant (ADR-0008) — refusals cancel settlement |
| jim overpays for data | per-query budget + dynamic price cap (ADR-0007) |
| Peer feeds garbage | gate refuses; trust debits; floor stops future buys |
| Payment loops between agents | call-chain 409 refusal before the paywall |
| Wallet key stolen from host | small float + platform secrets; MPC custody next |
| Facilitator down | 402 issuance degrades gracefully; sells pause, nothing lost |
| Free-route abuse (DoS) | paid routes are self-rate-limiting; put the platform's proxy rate limit in front of the free surface |

# jim — Launch kit (the demo, the listings, the applications)

The operator-facing half of Horizon 1 ([NORTH_STAR.md](NORTH_STAR.md) items
3 and 5). Everything here is something *you* run or submit; the repo side is
already built.

---

## 1. The three-minute demo

One recorded terminal session (plus a browser tab on `/proof`) telling the
whole story: **discover → pay → verify → a peer goes bad → refusal, no bill,
trust decays, routing changes.** Rehearse on testnet; record on whichever
network you want to show (mainnet rows are more impressive; testnet is free).

### Setup (before recording)

```bash
# Terminal A — the seller, with the mock peer composed into fundamentals:
PEER_SOURCES='[{"name":"mock-sentiment",
                "url":"http://localhost:4021/mock-peer/research",
                "price_estimate_usd":0.01,
                "products":["fundamentals"]}]' uv run jim-seller

# Browser — open http://localhost:4021/proof  (auto-refreshes every 30s)
```

### The beats

| # | ~time | What you do | What the viewer sees |
|---|---|---|---|
| 1 | 0:00 | `curl -s localhost:4021/.well-known/x402 \| jq .resources[0]`, then `.well-known/agent-card.json \| jq .skills[0], .x402` | machine discovery: prices, schemas, how to pay — no signup, no API key |
| 2 | 0:30 | `uv run python scripts/peer_demo.py AAPL` | a real x402 settlement; jim *subcontracts the sentiment peer inside the run*; the memo cites peer facts; "✅ … the agent economy loop" |
| 3 | 1:10 | flip to the browser: `/proof` | the settlement row (tx link), gate pass-rate, the trust table with `peer:mock-sentiment` earning score |
| 4 | 1:30 | restart Terminal A with `MOCK_PEER_CORRUPT=true` added, run `scripts/peer_demo.py MSFT` **three times** | the peer now lies (unusable rows). Each run: memo still ships **without** peer facts, sourcing note says `peer:mock-sentiment: skipped`, trust debits |
| 5 | 2:20 | fourth run | the trust floor kicks in: *"below the trust floor … refusing to pay a source whose data we cannot verify"* — jim stops paying the bad peer **before** any settlement |
| 6 | 2:40 | `/proof` again + `uv run jim-identity attest AAPL \| tail -20` | trust score decayed on the public page; a signed, offline-verifiable receipt binding memo hash → gate verdict → settlement |
| 7 | 2:55 | closing card | "Identity says who an agent is. jim proves what it delivered. x402-native · gate-verified · never billed for rejected research." |

The one-take script: beats 1–3 are the happy path, 4–5 are the differentiator
(nobody else can show a paid subcontractor being caught and cut off
deterministically), 6 is the receipt, 7 is the thesis.

> Optional mainnet variant: run beat 2 against the deployed instance and show
> Basescan for the settlement tx — worth it for the Coinbase audience.

## 2. Listings (do these once the deployment is live)

| Where | What to submit | Notes |
|---|---|---|
| **x402 Bazaar** | nothing — the first mainnet settlement auto-indexes | confirm with the Bazaar search/API after launch; the listing content comes from the catalog + `SERVICE_NAME`/`SERVICE_DESCRIPTION`/tags |
| **Official MCP registry** (`registry.modelcontextprotocol.io`) | `jim-mcp` server entry | point at the streamable-http endpoint of the deployment; description: first paragraph of the agent card |
| **PulseMCP** | same entry | highest-traffic community registry; skip the other six (fragmented, low ROI) |
| **ERC-8004 Identity Registry** | `uv run jim-identity register` → follow the printed steps | operator executes the transaction; registry address must be verified against the EIP first |

## 3. Program applications (drafts to adapt)

**x402 Foundation grant / Coinbase builder programs** — the pitch paragraph:

> jim is a production x402 agent that is both buyer and seller in one request:
> it sells cited financial research over x402 and pays upstream sources —
> including *other agents* — over x402 inside the same run. Its deterministic
> sourcing gate makes it the missing trust layer of the agent economy:
> research that fails verification is refused **before settlement** (the buyer
> is never billed), every source's reputation is computed from verification
> outcomes rather than reviews, and every shipped memo carries a signed,
> offline-verifiable receipt. Live proof: [/proof URL]. We're applying to
> [harden the rail / build the verifiable-delivery extension / …].

**Base Batches / hackathons** — lead with the demo video and `/proof`; the
category is "agentic commerce infrastructure," the hook is the corrupt-peer
beat (trust decay on camera).

## 4. Outreach one-liners (when you're ready to send)

- **Coinbase / x402 team**: "jim is the two-sided x402 reference you don't
  have yet — agent buys from agent inside one settled request, with a
  deterministic answer to 'was what it delivered true?' Live at [/proof]."
- **Stripe (agentic commerce)**: "ACP solved agentic *checkout*. The unsolved
  half is *delivery* — proving what an agent service shipped was correct, and
  not billing when it wasn't. Here's a working implementation on the rail
  your team already pays over."
- **Ramp / Brex / Mercury**: "Your agents automate procurement; none of them
  can yet *hire* an outside specialist safely. jim is the verifiable
  subcontractor: cited diligence over x402, spend-capped, receipt included,
  and it refuses payment when its own verification fails."

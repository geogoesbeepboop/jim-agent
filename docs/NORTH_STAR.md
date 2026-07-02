# jim — North Star

*The approved strategy (July 2026): what jim is becoming, why now, and what we
refuse to build. The practical backlog stays in [ROADMAP.md](ROADMAP.md); the
enterprise scaling detail stays in [ENTERPRISE_VISION.md](ENTERPRISE_VISION.md).
This doc is the direction.*

---

## The thesis

Agentic commerce's trust stack has three layers. Two are crowded; one is empty:

| Layer | Question | Who's building it (mid-2026) | Status |
|---|---|---|---|
| **Identity** | who is this agent? | ERC-8004 (mainnet Jan 2026, 45k+ agents), Experian Agent Trust, Fime FACT | crowded |
| **Authorization** | may it spend? | AP2 mandates, Coinbase Agentic Wallets, AWS AgentCore policy controls | crowded |
| **Correctness** | was what it delivered *true*? | **nobody** — reputation registries store feedback, which is gameable | **open** |

An agent can pass every identity and KYA check and still fabricate a number.
jim's deterministic sourcing gate, outcome-based trust ledger, and
never-bill-rejected invariant are a working occupation of that empty layer.
The loudest unsolved problem in the space — dispute liability for
agent-initiated transactions (Justt, Chargebacks911, PYMNTS all name it) — is
exactly what "refuse before billing + machine-re-adjudicable verdicts" answers.

> **jim is research with a receipt** — the reference implementation of
> *verifiable delivery* for the agent economy. Identity registries prove who
> an agent is; jim proves what it delivered.

Two market facts sharpen the bet: Stripe's ACP is merchant-checkout-shaped
with no path for independent agent *services* (so we don't build for it),
while Stripe itself pays over x402/USDC-on-Base — the rail jim already lives
on is the one both camps actually use. And Ramp/Brex/Mercury have shipped
internal agents but none consumes external paid agent services: the
**verifiable subcontractor** lane is empty.

## Horizon 1 — Make it undeniable (now)

Proof beats features. Companies are impressed by a live, economically-alive
agent with radical transparency, not by architecture docs.

1. **Go live on Base mainnet** and stay live ([DEPLOY.md](DEPLOY.md)); the
   first settlement auto-indexes jim on the x402 Bazaar.
2. **The `/proof` page** — live settlements (on-chain tx links), gate
   pass-rate, the trust table, and the headline counter: *money refused
   because verification failed*. ✅ shipped.
3. **The three-minute demo** — discover → pay → verified memo → a peer goes
   bad → refusal, no bill, trust decays, routing changes
   ([LAUNCH.md](LAUNCH.md)).
4. **Onchain identity + signed receipts** — `jim-identity`: ERC-8004
   registration prepared (operator executes), and EIP-191-signed gate-verdict
   attestations any EVM stack can verify offline. ✅ shipped (guarded).
5. **Distribution**: x402 Bazaar first; PulseMCP + the official MCP registry;
   x402 Foundation grants / Base Batches applications ([LAUNCH.md](LAUNCH.md)).

## Horizon 2 — Productize the trust kernel (1–3 months)

Turn the gate from jim's internal organ into everyone else's infrastructure:
**signed receipts on every paid response** (EAS-anchorable), **`/verify` —
the gate as a paid service** (bring your own claims + sources), the **dispute
primitive** (challenge → deterministic re-adjudication → automatic refund),
**task-shaped products** (diligence packet over the A2A task lifecycle,
comparables, portfolio bundles), and the **custody upgrade** to Coinbase
Agentic Wallets (MPC + session caps).

## Horizon 3 — Scale + the network (3–6 months)

Prepaid balances / x402 reusable-access sessions for high-frequency buyers;
monitors as paid subscriptions; **real peers** (sentiment, filings-NLP) plus a
published peer wire-format mini-spec so others can sell *to* jim; multi-tenant
policy packs (the compliance control plane); and the open note —
*"Verifiable Delivery for Agentic Commerce"* — aimed at the x402 Foundation
and A2A communities. Optionality note: Tempo/MPP as a second settlement rail
only if a counterparty demands it; Base/x402-native is a feature, not a gap.

## Company-specific motions

- **Coinbase** — the reference-citizen track: mainnet Bazaar listing, Agentic
  Wallets integration, ERC-8004 + EAS, a grant application, and pitching
  verifiable delivery upstream to the x402 Foundation.
- **Stripe** — don't force ACP; publish "checkout is solved, delivery isn't"
  aimed at their gap; stay discoverable on the x402 rail Stripe itself uses.
- **Ramp / Brex / Mercury** — the verifiable-subcontractor pitch: a demo where
  a procurement agent hires jim over x402 for the cited due-diligence leg,
  under a spend policy, with a receipt.

## What we refuse (standing decisions)

Personalized advice (the impersonal invariant is the moat) · proprietary data
(ADR-0007 — the public-domain invariant *is* the economics) · ACP integration
now · our own registry/marketplace · any token · a Tempo build-out before a
counterparty requires it.

## The scoreboard (what "impressive" means, measurably)

Live uptime · mainnet settlements + unique buyer addresses · gate pass-rate ·
**$ refused-not-billed** · receipts issued/verified · peer facts composed ·
disputes auto-adjudicated · p50 latency · inference cost per memo. All of it
on `/proof` — the metrics are the pitch.

---

*Grounding: x402 V2 (Dec 2025) + x402 Foundation (Coinbase/Cloudflare, w/
Google, Visa, AWS, Anthropic) + Bazaar live; ERC-8004 mainnet (Jan 2026);
Coinbase Agentic Wallets (Feb 2026); Stripe ACP live w/ Instant Checkout,
Tempo mainnet (Mar 2026) + Machine Payments Protocol; AP2 → FIDO donation;
A2A at Linux Foundation, 150+ orgs; AWS AgentCore x402-native; Ramp agent
fleet (2026). Verified July 2026 — re-verify before external claims.*

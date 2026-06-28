# jim — Enterprise Vision

*How I'd scale jim at a fintech giant, what they'd want it to do, and where it
fits their strategy.*

This is the "think bigger" companion to the practical [ROADMAP.md](ROADMAP.md).
It answers four questions:

1. If jim were built inside **Stripe / VISA / Coinbase**, what would they want it
   to *also* do? (§1)
2. What architecture decisions change to serve a **magnitude more customers**? (§2)
3. What does **enterprise-grade robustness** require that a proof-of-concept skips? (§3)
4. How does jim **fit each enterprise's vision**? (§4)

> **The reusable kernel.** Strip away the financial-research surface and jim is
> three deterministic gates wrapped around an LLM and a payment rail:
>
> | Gate | jim today | The general primitive |
> |---|---|---|
> | **Sourcing gate** ([gate.py](../src/jim/research/gate.py)) | every figure must match a cited fact | *verifiable claims* — no output a human/regulator relies on without provenance |
> | **Propose/dispose budget** ([budget.py](../src/jim/research/budget.py)) | hard per-query data-spend ceiling | *bounded autonomous spend* — the model can want; only code can pay |
> | **Materiality + impersonal gates** ([monitors/](../src/jim/monitors/)) | alert only on real change; stay general | *policy control plane* — deterministic rules decide when/how an agent may act |
>
> These three are exactly what makes an AI agent *safe to put in front of money*.
> That is the asset a Stripe / VISA / Coinbase actually wants — not another
> research-memo vendor. Everything below is about industrializing that kernel.

---

## 1. What the giants would want it to do

All three already have an agentic-commerce initiative (Stripe's Agent Toolkit &
Agentic Commerce Protocol, Visa Intelligent Commerce / agent payment credentials,
Coinbase's x402 + AgentKit + Bazaar). jim's kernel is the missing *trust and
spend-control* layer underneath those. Here's the shape each would push it into.

### 1.1 Stripe — the spend-control + audit primitive for agentic commerce

| They'd want jim to… | Why it maps |
|---|---|
| **Act, not just research** — pay invoices, issue refunds, reconcile, assemble dispute evidence — within mandates | The propose/dispose `BudgetCap` is exactly "let an agent spend, bounded." Generalize from *data spend* to *any spend*. |
| **Underwrite** — pull a merchant's filings + signals into a *cited, auditable* risk memo for Capital / Radar | The sourcing gate is the audit trail underwriting needs: every risk factor traces to a source, defensible to a regulator. |
| **Chargeback evidence** — provenance-tracked narratives for disputes | "Every claim cites its document" is literally what a representment packet needs. |
| **Recurring agent mandates** — authorize an agent to spend on a schedule within limits | jim's monitors already are "scheduled, bounded, only-act-when-rule-fires." Same machinery, applied to payouts. |

**What Stripe changes:** settlement moves off x402-on-Base to *their* ledger +
card/ACH/stablecoin rails (Bridge), **idempotency keys everywhere** (their
signature primitive — see [ROADMAP Phase 6](ROADMAP.md)), **mandates** for
recurring authorization, and a spend-management console. jim's gate stays; the
rail underneath swaps.

### 1.2 VISA — the explainability + agent-credential layer on the network

| They'd want jim to… | Why it maps |
|---|---|
| **Carry a constrained agent credential** — a tokenized payment credential scoped to merchant categories, limits, time windows | jim's wallet + budget cap is the toy version; Visa wants *tokens with policy*, not raw keys. The policy lives where the budget cap already conceptually sits. |
| **Explain risk with provenance** — "this transaction is risky *because* [cited signals]" | Adverse-action / explainability is a regulatory requirement. The sourcing gate forces every risk factor to trace to a signal — explainability by construction. |
| **KYB / merchant onboarding research** — cited, reproducible diligence | Same engine, new product: identifier → cited memo, gate-verified. |
| **Operate at network scale** — deterministic, replayable decisions auditors can re-run | The whole point of the deterministic gate: same input → same verdict, always. Auditors love this. |

**What VISA changes:** custody → HSM + tokenization (no raw key ever), settlement
→ ISO 20022 / network rails, identity → strong **agent identity + registry** (who
operates this agent, is it sanctioned), and a hard requirement that *every*
model-touched decision be wrapped in a deterministic, explainable gate — which is
jim's strength, not a retrofit.

### 1.3 Coinbase — the reference citizen of the onchain agent economy

This is the most natural home: x402 *is* Coinbase's protocol, Base is the chain,
the Bazaar is their discovery rail, AgentKit is the SDK. jim is nearly a reference
app already.

| They'd want jim to… | Why it maps |
|---|---|
| **Be the AgentKit template** — the canonical "verifiable, self-funding agent" others fork | jim already buys *and* sells over x402, tracks margin, and discovers via Bazaar. Add CDP wallets and it's a showcase. |
| **Sell cited onchain analytics to other agents** | The `token` product is exactly this; scale the source set across chains/protocols. |
| **Anchor a marketplace of thousands of jim-like agents** trading USDC for verifiable services | jim's gate is the trust primitive that keeps such a market from drowning in hallucinated data — see [AGENT_INTEROP.md](AGENT_INTEROP.md). |

**What Coinbase changes:** wallet → **CDP MPC server wallets** with a policy engine
+ spend permissions / session keys (the architecture already names CDP as the
custody upgrade), facilitator → their production facilitator with real fees,
identity → onchain (Basenames + EAS attestations), and compliance for crypto
(Travel Rule, OFAC screening on every settle, MiCA, proof-of-reserves-style
auditability).

### 1.4 The common thread

> Every one of them already has the *commerce* and the *rails*. What they lack —
> and what jim is — is the **deterministic trust + bounded-spend kernel** that makes
> it safe to let an agent loose on money. The differentiator isn't the memo; it's
> that **the model proposes and code disposes**, reproducibly and auditably.

Capabilities all three would push jim to grow into, beyond US-equity memos:
global + multi-jurisdiction data (IFRS, non-US filings, languages); real-time
streaming sources (not just polling monitors); a **research → decision → action →
audit** closed loop (act on findings within mandates, never unbounded); and
**policy-configurable personalization** — flip the impersonal guard from a
hard-coded rule into a per-tenant policy so a licensed entity *can* personalize
and an unlicensed one provably can't.

---

## 2. The architecture that changes at scale

jim today is one beautiful, correct, single-process system: FastAPI + an
in-process LangGraph compiled at import, a single asyncio scheduler loop,
Postgres+pgvector, a local key, best-effort tracing. That is the *right* shape to
prove the rails. To serve orders of magnitude more customers, here's what moves —
and crucially, **what stays** (the gates).

| Concern | jim today | At magnitude | Why it has to change |
|---|---|---|---|
| **Compute** | single FastAPI process | stateless replicas behind a LB; split fast-path (cached/deterministic) from slow-path (LLM) into separate pools | a flood of cache hits shouldn't queue behind LLM latency; replicas need no shared in-process state |
| **Scheduler** | one asyncio due-loop ([scheduler.py](../src/jim/monitors/scheduler.py)) | partitioned work queue / Temporal: leases, exactly-once, backpressure | a single loop can't fan millions of monitor ticks; ADR-0002 already calls this a one-file swap |
| **LLM calls** | direct Anthropic per node | prompt caching + Batch API for monitors + Haiku→Sonnet→Opus cascade + semantic memo cache + load-shed to deterministic fallback | the model is the bottleneck *and* the cost center; the gate is cheap, the synthesis isn't |
| **Payments** | synchronous on-chain settle per call, with an unpaid price pre-flight | prepaid balances / payment channels, batched net settlement, cached price quotes; x402 only at the edges | you cannot settle every microtransaction on-chain synchronously at VISA volume |
| **Custody** | local `eth_account` key in `.env` | CDP MPC / HSM, per-purpose sub-accounts, custody-level spend policy, rotation, hot/warm/cold split | one key = one blast radius, no rotation, no policy — a non-starter |
| **Data** | one Postgres + pgvector, in-memory fallback | read replicas, tenant sharding, Redis hot cache over `data_purchases`, OLAP rollups for the dashboard | OLTP serving and margin analytics must not contend; vectors at billions need real indexing/quantization |
| **Tenancy** | one global `get_settings()` singleton | per-tenant config resolver: price, budget, model, gate tolerance, policy, entitlements; isolation + per-tenant quotas | every knob becomes per-customer; blast radius must be contained per tenant |
| **Sources** | direct EDGAR/Yahoo/Graph calls | circuit breakers, vendor redundancy behind the `Source` Protocol, freshness SLAs, an entitlement/licensing layer | external upstreams are SPOFs; Yahoo's ToS bars redistribution at scale |
| **Correctness** | cache write after settle (a gap) | idempotency keys + settlement reconciliation job | settle-then-fail-to-record loses money; needs distributed-systems rigor |
| **Observability** | best-effort Langfuse no-op | first-class OTel traces, SLOs, immutable compliance audit log, alerting | "is jim healthy / did we double-charge / why did the gate pass" must be answerable |
| **Abuse** | paid routes are DoS-resistant by construction | rate limits + WAF on *unpaid* routes (catalog, manifest, 402 pre-flight, /map, /monitors) and the pre-settlement work window | the free surface and the work-before-settle window are the attack surface |
| **Release** | `uv run` | containers + k8s + canary, with the **eval suite as the deploy gate** | jim already has a gate-regression merge gate; promote it to a canary that A/Bs model changes |

> **What does not change: the trust gates.** Sharding, queues, replicas, and MPC
> wallets are all *plumbing*. The sourcing gate, the budget cap, and the
> materiality/impersonal guards stay exactly where they are — between the model and
> the customer. Scaling jim means scaling everything *around* the gates without
> ever moving the model back across them.

A useful mental model is **CQRS for the engine**: the *write* path (LLM synthesis,
debate, judge) is expensive and rate-limited; the *read* path (cache hit →
deterministic repackage, gate re-verify) is cheap and infinitely scalable. Today
they share one graph; at scale they're two pools with two SLAs.

---

## 3. Enterprise-grade robustness

What a proof-of-concept skips and a regulated deployment requires. (The buildable
slice is [ROADMAP Phase 6](ROADMAP.md); this is the target state.)

**Correctness.** The gate is the most security-critical code in the system and is
currently regex-based and untested against adversarial inputs. Enterprise-grade
means: property-based fuzzing of every numeric-obfuscation vector, a tokenizer-
based extractor if regex proves bypassable, and a golden-output regression set in
the deploy gate. *If the gate has a bypass, the entire trust story is fiction* —
this is the single highest-leverage hardening in the whole program.

**Reliability.** Timeouts + bounded retries + circuit breakers on every external
dependency; graceful degradation by *failure* (jim already degrades well by
*absence*); dead-letter + retry for monitor deliveries; backpressure / load-shed
under LLM rate limits; idempotency on the sell side and reconciliation on both
legs so a settled-but-unrecorded purchase is detected and healed.

**Security.** Secrets in a vault/KMS/MPC, never `.env`; identifier validation to
kill SSRF/traversal in source fetches; **treat all upstream text as
prompt-injection-hostile** (the gate guards *numbers*, not narrative framing — add
an explicit input check so a malicious source can't steer the synthesizer);
webhook replay protection (timestamp + nonce on top of the existing HMAC);
dependency pinning + supply-chain scanning.

**Financial integrity.** A continuous reconciliation invariant —
`Σ settlements == Σ query_records.price_out`, buy-side `Σ purchases == on-chain
spend` — with alarms on drift; graceful budget-exhaustion (serve cached/partial,
don't 500); quote-vs-settle price-change handling on the buy leg.

**Quality / drift.** Extend the eval harness ([eval/](../src/jim/eval/)) from
"gate regression + debate lift" to: per-source quality scoring (gate pass-rate by
upstream), drift detection (does pass-rate fall when EDGAR changes tag
conventions?), and a **model-upgrade regression alarm** — when Anthropic ships a
new model, the eval suite must show non-regression before it's promoted (this is
where the `claude-api` model-migration discipline lives).

**Operational.** Health/readiness probes (jim has `/health`), runbooks, on-call,
incident response, config validation that fails fast at startup (the mainnet
preflight's "testnet facilitator on mainnet = hard fail" generalized to all
config).

---

## 4. How jim fits each enterprise's vision

The strategic narrative — why a giant funds this, not just what it does.

- **Stripe's vision is agentic commerce infrastructure.** Their bet is that agents
  will transact on behalf of businesses and people. The blocker to productionizing
  that is *control and accountability*: how do you let an agent spend without it
  going rogue, and prove afterward why it did what it did? jim is the **spend-control
  + audit primitive** that makes their agentic rails underwritable. jim's
  propose/dispose is the shape of a Stripe agent mandate; jim's gate is the shape of
  a Stripe audit log.

- **VISA's vision is trusted agent payments on the network.** They're issuing agent
  payment credentials and need every agent decision to be *explainable and
  replayable* to satisfy regulators and the issuing banks. jim is the
  **explainability + provenance layer**: a deterministic gate that turns "the AI
  decided" into "here is the cited chain of facts, re-runnable to the same verdict."
  That is what gets an agent credential approved for the network.

- **Coinbase's vision is the onchain agent economy.** Thousands of agents
  discovering and paying each other in USDC for services. That economy collapses if
  agents can't trust each other's data — hallucinations compound across hops. jim is
  the **verifiable citizen and the trust primitive**: an agent that only resells what
  its gate can verify, whose gate pass-rate *is* a reputation score. jim is both the
  reference AgentKit app and the pattern that keeps the Bazaar honest. See
  [AGENT_INTEROP.md](AGENT_INTEROP.md).

> **The one-sentence pitch.** Every AI agent that spends money or makes a claim a
> human or regulator will rely on needs verifiable claims, bounded spend, and an
> auditable trail. jim is the reference implementation of all three — and at a
> fintech giant it stops being a research product and becomes the *trust layer*
> that makes agentic finance shippable.

---

## See also

- [ROADMAP.md](ROADMAP.md) — the buildable on-ramp to everything here
- [AGENT_INTEROP.md](AGENT_INTEROP.md) — the agent-to-agent economy in depth
- [ARCHITECTURE.md](ARCHITECTURE.md) + [adr/](adr/) — why the current design is the way it is

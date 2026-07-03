# jim — Roadmap (the "keep iterating" track)

This is the **near-term, buildable** backlog: the concrete next phases that extend
[BUILD_PLAN.md](BUILD_PLAN.md) beyond Phase 5. It is the practical companion to
[ENTERPRISE_VISION.md](ENTERPRISE_VISION.md) (the 10-year "how I'd scale this at
Stripe / VISA / Coinbase" thinking) and [AGENT_INTEROP.md](AGENT_INTEROP.md) (the
agent-to-agent economy). Where a roadmap item is the tractable on-ramp to an
enterprise-scale idea, it links across.

> **House rule carried forward.** Every item must keep jim's defining invariant —
> *the model proposes, deterministic code disposes* — and ship **offline-first**
> (fully tested with no key / wallet / network / DB), leaving the one live exit
> run as the single unchecked box. See [jim-working-cadence] in the build notes.

---

## Track 0 — Earn the right (it's untested)

Phase 5 is *implemented* but the live legs are unproven and the crown-jewel gate
has never been adversarially stressed. **Do this before adding surface area** —
new features on an unverified core just multiply the unknowns.

- [ ] **Run every "live exit run."** Each phase in [BUILD_PLAN.md](BUILD_PLAN.md)
      ends with one unchecked box (a real Sepolia settlement, a real paid memo at
      100% coverage, a positive-margin dashboard, a debate-vs-single-pass lift, a
      monitor pushing a real update, a mainnet settle). Close them top to bottom;
      they are the end-to-end proof the offline tests intentionally stop short of.
- [x] **Fuzz the sourcing gate** ([gate.py](../src/jim/research/gate.py)). Done —
      and it was as leveraged as predicted: the probe found 8 bypass classes
      (sci notation, "5 billion" without grouping, bare integer runs, `5B`
      suffixes, underscores, spelled-out figures, €/£, `999999k`) and 2
      false-reject classes (ranges, accounting negatives — the likely root
      cause of the rejected first-mainnet COIN memo). The extractor now catches
      all of them; loss phrasing over negative facts matches by magnitude; a
      Hypothesis property suite ([test_gate_fuzz.py](../tests/test_gate_fuzz.py))
      pins both invariants over random values. See
      [ADR-0008](adr/0008-agent-economy-trust-callchain-billing.md).
- [ ] **Record-and-replay integration tests** for the live upstreams (EDGAR,
      Yahoo, The Graph). Cassette the real responses once, replay in CI — so a
      change to [edgar.py](../src/jim/research/edgar.py) parsing is caught without
      hitting SEC on every run, and an upstream schema drift is caught when you
      re-record.
- [ ] **Golden-memo regression set.** Freeze a handful of known-good cited memos;
      assert the engine still produces gate-passing output with the same fact set
      after refactors. Pairs with the existing `jim-eval --gate-only` merge gate.

---

## Phase 6 — Harden & prove (robustness)

Goal: jim survives the real world — flaky upstreams, partial failures, a hostile
caller, a key that must rotate.

- [x] **Resilience wrapper (free upstreams).** Done for EDGAR / Yahoo / macro —
      [net/resilience.py](../src/jim/net/resilience.py): one helper
      (`resilient_call`) with per-attempt timeout, bounded retries with
      exponential backoff + jitter, and a per-host circuit breaker with a
      half-open probe; transport failures retry, semantic errors don't; each
      source's existing degradation (drop the reading / EdgarError) is
      preserved. The facilitator's `get_supported()` also degrades to exact-EVM
      on our network instead of failing 402 issuance. *Still open:* wrapping
      Anthropic calls and [buyer/client.py](../src/jim/buyer/client.py)'s paid
      leg (retrying a *payment* needs idempotency first — see the next item).
- [ ] **Idempotency + settlement reconciliation.** The window between
      `buyer.pay()` settling on-chain and `store.record_purchase()` writing the
      cache is a money-losing failure gap. Add an idempotency key per sell-side
      call and a reconciliation job that proves `Σ settlements == Σ query_records`
      and buy-side `Σ purchases == on-chain spend`. (Foundational for the
      enterprise ledger — see [ENTERPRISE_VISION §3](ENTERPRISE_VISION.md).)
- [ ] **Custody upgrade: env key → CDP MPC / KMS.** The architecture already names
      this ([ARCHITECTURE §8](ARCHITECTURE.md#8--paymentsx402-v2-deep-dive)). A raw
      `eth_account` key in `.env` has no rotation, no policy, one blast radius. Slot
      a Coinbase CDP server wallet (or KMS-backed signer) behind the existing
      `address`/signer interface in [wallet/](../src/jim/wallet/) and move the spend
      ceiling down into a custody-level policy, not just the app's `BudgetCap`.
- [x] **Input hardening (identifiers).** Done —
      [identifiers.py](../src/jim/research/identifiers.py): allowlist-based
      `canonicalize(identifier, product)` runs at the top of `run_research`,
      before any settings/store/source work, so a hostile ticker can't reach
      URL construction (SSRF/path-traversal defense; per-source validation
      stays as depth). *Still open:* the source-**text**-as-untrusted injection
      check for the synthesizer prompt.
- [x] **Webhook replay protection.** Done — deliveries carry `X-Jim-Timestamp`
      + `X-Jim-Nonce`, and the HMAC binds both to the body
      (`sha256(secret, f"{ts}.{nonce}." + body)`); subscriber-side
      `verify_delivery()` rejects stale timestamps and replayed nonces
      ([notify.py](../src/jim/monitors/notify.py)). *Still open:* the
      dead-letter + retry path for failed pushes.
- [ ] **SLO surface.** Promote [obs/tracing.py](../src/jim/obs/tracing.py) from
      best-effort Langfuse to OpenTelemetry spans + a few real metrics (p99
      latency, gate pass-rate, settlement success rate, margin) so "is jim healthy"
      is answerable. Still no-op without config.

---

## Phase 7 — Compose: agent-to-agent sourcing

Goal: jim stops trying to know everything and starts **buying specialized signals
from other agents**, marking up the synthesis. Full treatment in
[AGENT_INTEROP.md](AGENT_INTEROP.md); the buildable slice:

- [x] **Source-as-agent.** Done — [peer.py](../src/jim/sources/peer.py):
      `PeerSource` buys a facts payload (bare or jim-shaped) from a peer over
      x402 through the same `procure()` → budget → cache path; `CompositeSource`
      merges peer facts into the product snapshot with renumbered citations +
      per-fact origins. Peers are config (`PEER_SOURCES`); a mock peer vendor
      closes the testnet loop. See [ADR-0008](adr/0008-agent-economy-trust-callchain-billing.md).
- [x] **The gate as composition-safety.** Done — every gated run attributes its
      outcome to the sources whose facts it used
      ([interop/trust.py](../src/jim/interop/trust.py)); the Laplace-smoothed
      pass-rate is the trust score (`source_trust_events` ledger, surfaced on
      the dashboard), and the buy path refuses peers below `PEER_TRUST_FLOOR`.
- [x] **Cross-agent spend safety.** Done — buys carry `X-Jim-Call-Chain`
      ([interop/callchain.py](../src/jim/interop/callchain.py)); the seller
      refuses loops and over-depth chains with 409 *before* the paywall, and
      the buyer never extends a chain past `CALL_CHAIN_MAX_DEPTH`. (A shared
      request-tree *budget* — beyond depth — stays open; today each hop's
      per-query `BudgetCap` bounds it.)
- [x] **Agent card.** Done — `GET /.well-known/agent-card.json`
      ([agentcard.py](../src/jim/marketplace/agentcard.py)) derives skills from
      the catalog, binds x402 payment details, states the trust/call-chain
      contract, and is linked from the `/.well-known/x402` manifest.

---

## Phase 8 — Scale the rails (on-ramp to enterprise)

Goal: the tractable subset of [ENTERPRISE_VISION §2](ENTERPRISE_VISION.md) — make
jim hold up past a single process and a single tenant.

- [ ] **Durable scheduler.** ADR-0002 already calls the asyncio loop a one-file
      swap. Move monitor ticks to a partitioned work queue / Temporal so millions
      of monitors get leases, exactly-once execution, and backpressure — see
      [ADR-0002](adr/0002-lightweight-asyncio-scheduler.md).
- [ ] **Model cost controls.** The LLM is the bottleneck and the cost center. Add
      prompt caching, route monitors through the Batch API, a Haiku→Sonnet→Opus
      cascade by difficulty, and a **semantic memo cache** (extend the pgvector
      `insights` table from hashed trigrams to a real embedding model — already a
      drop-in per [ARCHITECTURE §6.3](ARCHITECTURE.md#63-store-store)). Under model
      rate limits, shed load to the deterministic fallback instead of failing.
- [ ] **Prepaid balances / payment channels.** Per-call on-chain settlement is
      fine at demo scale and untenable at volume. Let a buyer deposit once and draw
      down (off-chain accounting, periodic on-chain netting) so the hot path isn't
      a synchronous settlement. Keep x402 at the edges.
- [ ] **Multi-tenant config.** Replace the single `get_settings()` `lru_cache`
      singleton with a tenant-scoped resolver: price, budget, model, gate
      tolerance, impersonal policy, and data entitlements all become per-tenant,
      with isolation and per-tenant rate limits.
- [ ] **Data layer for scale.** Read replicas + a Redis hot cache in front of
      `data_purchases`; move the dashboard's aggregations off the OLTP path to an
      OLAP/rollup table so margin analytics don't contend with serving.

---

## Phase 9 — Enterprise surface (where it meets the vision)

Goal: the things a Stripe/VISA/Coinbase deployment would *require* before this
touches a regulated workflow. Detailed in
[ENTERPRISE_VISION §1 & §4](ENTERPRISE_VISION.md).

- [ ] **Policy gates (the impersonal guard, generalized).** Today the impersonal
      guard is hard-coded for the publisher's-exclusion lane. Make the gate set
      **policy-configurable per tenant**: a licensed/registered tenant can opt into
      personalized advice; an unlicensed one can't. The deterministic-gate pattern
      becomes a compliance control plane.
- [ ] **Compliance pack.** OFAC/sanctions screening on every settlement
      counterparty, Travel-Rule metadata on crypto legs, an immutable audit log of
      every gate verdict + settlement + model output, and configurable data
      retention. The sourcing gate already produces a compliance-grade audit trail
      per claim — wire it into a retained, queryable log.
- [ ] **Human-in-the-loop review queue** for low-confidence or high-value outputs:
      anything the judge scores near the `judge_threshold`, or any *action* (not
      just research) above a value bound, parks for human approval — the same
      propose/dispose split, with a human as the disposer.
- [ ] **Licensed data adapters.** The provenance caveat in
      [ARCHITECTURE §5.2](ARCHITECTURE.md#52-market-enrichment-yahoopy) is explicit:
      Yahoo carries ToS. Add redistributable/licensed market-data + global-filings
      adapters behind the unchanged `Fact`/citation model so an enterprise can ship
      legally outside the EDGAR public-domain lane.
- [ ] **White-label / embed.** Per-tenant branding, prompts, gates, and pricing so
      one jim deployment serves many enterprise customers — the multi-tenant config
      from Phase 8 made customer-facing.

---

## How to read this against the vision docs

| If you want… | Read |
|---|---|
| What to build *next*, concretely | this file |
| What the fintech giants would demand + the scaling rethink | [ENTERPRISE_VISION.md](ENTERPRISE_VISION.md) |
| Whether/how jim should talk to other agents | [AGENT_INTEROP.md](AGENT_INTEROP.md) |
| Why the current design is the way it is | [ARCHITECTURE.md](ARCHITECTURE.md) + [adr/](adr/) |

The roadmap is deliberately ordered **prove → harden → compose → scale → enterprise**.
Each phase is shippable on its own and keeps the offline-first, model-proposes /
code-disposes spirit intact.

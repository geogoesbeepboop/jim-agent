# jim — Showcase

*The interview-facing brief: what's built, what it proves, the stories worth
telling, and the plan — ordered by what makes the project undeniable, not by
what's fun to build next. The buildable backlog stays in [ROADMAP.md](ROADMAP.md);
the demo script stays in [LAUNCH.md](LAUNCH.md); this doc is the narrative and
the priorities.*

---

## The 30-second pitch

> jim is a financial research agent that **sells its research over x402 and
> pays for its own data over x402** — including buying signals from *other
> agents* inside the same paid request. What makes it different is the trust
> model: a **deterministic sourcing gate** (no LLM) verifies that every figure
> in every memo traces to a cited primary source before anything ships, a
> **trust ledger** scores every data source by its verification outcomes
> rather than reviews, and research that fails verification is **refused
> before settlement — the buyer is never billed**. Identity registries prove
> who an agent is; jim proves what it delivered.

The one-sentence version: **research with a receipt** — a working occupation
of the empty "correctness" layer of agentic commerce
([NORTH_STAR.md](NORTH_STAR.md)).

---

## What's built today

Phases 0–5 complete, the Phase 7 agent-economy slice complete, Phase 6
hardening in progress. 37 of 43 build-plan boxes checked — the six open boxes
are **all** live exit runs (see the scorecard below).

### 1. Verifiable research (the crown jewel)

- **The sourcing gate** ([research/gate.py](../src/jim/research/gate.py)) — a
  deterministic, no-LLM check that every figure in a memo matches a cited
  `Fact`. Fuzz-hardened with Hypothesis
  ([tests/test_gate_fuzz.py](../tests/test_gate_fuzz.py)): an adversarial
  probe found **8 bypass classes and 2 false-reject classes**, all fixed and
  pinned by property tests ([ADR-0008](adr/0008-agent-economy-trust-callchain-billing.md)).
- **Injection-inert by proof, not hope** — 10 adversarial eval cases pin that
  instruction-shaped content in source text cannot move a gate verdict
  (fullwidth-digit evasion, zero-width-space smuggling, homoglyph citations,
  fake approval stamps), plus an end-to-end scenario where a synthesizer that
  "obeys" an injected instruction is rejected every retry and **bills $0**.
- **Defense in depth around the gate** — a completeness check (the gate's
  mirror: material facts the memo *omitted*), an LLM faithfulness judge with
  a per-claim checklist, and a fail-closed LangGraph pipeline
  ([research/engine.py](../src/jim/research/engine.py)):
  gather → memo-cache → synthesize → gate (with retry feedback) → judge.
- **Input hardening** — allowlist identifier canonicalization runs before any
  settings/store/source work, so a hostile ticker never reaches URL
  construction (SSRF/path-traversal defense).

### 2. A two-sided x402 economy

- **Sell side** ([seller/app.py](../src/jim/seller/app.py)) — a FastAPI x402
  paywall with settlement receipts and an audit trail
  ([seller/audit.py](../src/jim/seller/audit.py)).
- **Buy side** ([buyer/client.py](../src/jim/buyer/client.py)) — pays 402s
  upstream (The Graph, peer agents) inside the same request it's selling,
  and the margin engine books cost-in vs revenue-out per query.
- **Money never moves unverified** — a per-query `BudgetCap`, a dynamic-price
  guard that refuses over-budget x402 prices, and the billing invariant:
  **gate-rejected research is refused, never billed** (ADR-0008 — born from a
  real mainnet bug, see the war stories).

### 3. The agent economy (Phase 7)

- **Source-as-agent** ([sources/peer.py](../src/jim/sources/peer.py)) — jim
  buys cited fact payloads from peer agents through the same
  procure → budget → cache path as any upstream, merging peer facts into the
  snapshot with renumbered citations and per-fact origins.
- **The gate as a composition firewall** — every gated run attributes its
  outcome to the sources whose facts it used
  ([interop/trust.py](../src/jim/interop/trust.py)); the Laplace-smoothed
  pass-rate *is* the trust score, and the buy path refuses peers below the
  trust floor. Reputation computed from verification outcomes, not reviews —
  it can't be review-bombed or astroturfed.
- **Cross-agent spend safety** ([interop/callchain.py](../src/jim/interop/callchain.py)) —
  a propagated `X-Jim-Call-Chain` lets the seller refuse loops and over-depth
  request trees with a 409 **before the paywall**, and the buyer never
  extends a chain past max depth.
- **Delegable, not just callable** — an A2A agent card at
  `/.well-known/agent-card.json` derives skills from the catalog, binds x402
  payment details, and states the trust/call-chain contract; x402 Bazaar
  discovery and an MCP server round out the surfaces.

### 4. Monitors that only speak on material change

Scheduled diff-driven monitors ([monitors/](../src/jim/monitors/)) behind a
**deterministic materiality gate** (floors + cooldowns — ADR-0001), an
impersonal-output guard, NL monitor creation on the propose/dispose pattern
(the model proposes a spec; deterministic code clamps thresholds and drops
unknown kinds), and webhook delivery with HMAC binding timestamp + nonce +
body, with subscriber-side replay rejection.

### 5. An eval harness that already earned its keep

[ADR-0009](adr/0009-eval-harness-persisted-runs-tiered-suites.md): four
tiered suites (gate 48 cases / guards 40 / scenarios 10 / live), one uniform
`CaseResult` shape, persisted JSON runs with git sha + config snapshot,
baseline markers, and two-regime comparison (offline = zero tolerance, live =
configurable thresholds). A hand-rolled no-CDN dashboard (`jim-eval ui`)
serves trends from the same `compare_runs()` the CLI uses — CI and UI can
never disagree about what "regressed" means. The offline block: **98
deterministic cases, ~1s, $0**, wired into the merge gate.

### 6. Engineering discipline (the meta-capability)

- **265 hermetic tests** — the default suite needs no DB, wallet, network, or
  API key; `conftest.py` actively neutralizes a developer's real `.env`, and
  modifying that hermeticity to make a test pass is a named DO-NOT.
- **Offline-first as a rule** — every feature ships fully tested offline; the
  one live "exit run" per phase is the only unchecked box, ever.
- **9 ADRs** recording real decisions with real alternatives; architecture
  docs and a Mermaid system map (`jim-map`) maintained alongside code.
- **A commit-time health gate** (`.claude/gate.sh`: lint + fast tests) and an
  offline eval baseline compare as the merge gate.

---

## The war stories (what to actually tell)

These land because each one is a *found* problem with evidence in the repo —
not a feature list. Each follows the same arc: built the checker → checker
caught something real → fix became a permanent invariant.

1. **The fuzz that paid for itself.** The sourcing gate is the crown jewel,
   so before adding features I adversarially fuzzed it. The probe found 8 ways
   to sneak an uncited figure past it (scientific notation, "5 billion"
   spelled out, `5B` suffixes, digit underscores, €/£ currencies…) and — more
   interesting — 2 ways it *wrongly rejected honest memos* (numeric ranges,
   accounting-negative parentheses). One of those false-reject classes was
   the likely root cause of a real rejected mainnet memo. A Hypothesis
   property suite now pins both directions over random values.

2. **The bug that became an invariant.** jim's first mainnet settlement
   charged a buyer for a memo the gate had *rejected* — the paywall settled
   before the verdict. The fix wasn't a patch; it became a named, eval-asserted
   invariant: gate-rejected research is refused before settlement, never
   billed. There's an end-to-end scenario that plants a persistent
   hallucination and asserts **$0 at the ledger** ([ADR-0008](adr/0008-agent-economy-trust-callchain-billing.md)).

3. **The silent regression only a live eval could see.** The first persisted
   live-eval run showed every held-out memo `rejected` with ok-rate 0.00 —
   while the offline sourcing gate still passed 0.875. Root cause: the
   faithfulness judge's `max_tokens=900` truncated its per-claim JSON
   mid-array, failed to parse, and fail-closed every run, mislabeled as
   "unparseable output" (which read like a model-quality problem; it wasn't —
   the model was emitting valid JSON). This is exactly the "gate passes but
   the agent is broken" failure class a persisted live ok-rate exists to
   catch ([ADR-0009](adr/0009-eval-harness-persisted-runs-tiered-suites.md)).

4. **The eval dataset caught a bug while being written.** Growing the gate
   regression from 5 to 38 labeled memos immediately surfaced a real
   false-reject: the range-parser's scale-suffix class was case-insensitive
   and consumed the "t" of a following word — "2023-2024 the…" parsed as
   trillions. Labeled datasets are executable specs.

5. **Prompt injection, closed end-to-end.** jim synthesizes over untrusted
   third-party text (filings, peer memos). Rather than assert the gate is
   injection-proof, the eval *simulates the compromise*: a snapshot whose
   source text carries an injection and a synthesizer that obeys it — every
   retry rejected, $0 billed. The claim isn't "the model resists injection";
   it's "a fully-compromised model still can't move money or publish a lie."

The through-line to say out loud: **"the model proposes, deterministic code
disposes."** Every LLM output that touches money, alerts, or published
figures passes a deterministic gate first — and both gate directions
(false-accept *and* false-reject) are adversarially tested, because a gate
that wrongly rejects honest work is a revenue bug, not a safety feature.

---

## Honest scorecard

| Claim | Status | Evidence |
|---|---|---|
| Every figure verifiably cited | ✅ proven offline | gate + fuzz suite + 48-case eval |
| Rejected research never billed | ✅ proven offline | eval scenario asserts $0 at ledger |
| Injection cannot move money/figures | ✅ proven offline | 10 gate cases + e2e scenario |
| Loop/over-depth chains refused pre-payment | ✅ proven offline | callchain tests |
| Trust ledger cuts off bad peers | ✅ proven offline | composite-trust tests, demo beats 4–5 |
| Real x402 settlements (testnet) | ⬜ **unproven live** | BUILD_PLAN exit run |
| Paid memo at 100% coverage, live | ⬜ **unproven live** | BUILD_PLAN exit run |
| Live eval lift (debate vs single-pass) | ⬜ **unproven live** | BUILD_PLAN exit run |
| Monitor fleet pushing real updates | ⬜ **unproven live** | BUILD_PLAN exit run |
| Positive-margin dashboard, live | ⬜ **unproven live** | BUILD_PLAN exit run |
| Mainnet settlement post-billing-fix | ⬜ **unproven live** | BUILD_PLAN exit run |

This table *is* the priority argument: every impressive claim currently ends
with "…in tests." One live deployment converts the whole column.

---

## Next steps — ordered by leverage

### Tier 1 — Proof (do this before any new feature)

The north star already says it: **proof beats features.** New surface area on
an unverified core multiplies unknowns and impresses nobody.

1. **Close the six live exit runs, top to bottom.** Fund a Sepolia wallet;
   settle; buy a real memo at full coverage; run `jim-eval run --suite live`
   for the debate lift; run a small monitor fleet on Postgres; show the
   margin dashboard; finish with one mainnet settle through the fixed billing
   path. Each is a checkbox in [BUILD_PLAN.md](BUILD_PLAN.md) with the
   runbook in [DEPLOY.md](DEPLOY.md).
2. **Stand up a persistent public instance** (testnet is fine) with `/proof`
   reachable. "Is it running anywhere?" must have a URL for an answer. The
   proof page — live settlements, gate pass-rate, trust table, signed
   receipts — is the single strongest interview artifact this project can
   produce.
3. **Record the three-minute demo** ([LAUNCH.md §1](LAUNCH.md)) — the
   corrupt-peer arc (trust decays on camera, payment refused before
   settlement) is the beat no one else can show. A recorded demo also
   de-risks live-coding-interview network roulette: rehearse live, but carry
   the recording.

### Tier 2 — Best story per week of work

4. **Idempotency + settlement reconciliation** ([ROADMAP Phase 6](ROADMAP.md)).
   The window between `buyer.pay()` settling on-chain and the cache write is
   a money-losing gap. Idempotency keys per sell-side call plus a
   reconciliation job proving `Σ settlements == Σ query_records` and buy-side
   `Σ purchases == on-chain spend`. This is the classic
   exactly-once-money-over-at-least-once-compute problem — the best pure
   distributed-systems talking point in the backlog, and foundational for the
   enterprise ledger story.
5. **Record-and-replay cassettes** for EDGAR / Yahoo / The Graph, plus the
   **golden-memo regression set** (Track 0's remainder). Cheap, and they
   round out the testing story: hermetic unit tests → offline eval suites →
   replayed integration → one live exit run per phase. That ladder is itself
   an interview answer ("how do you test an agent?").

### Tier 3 — Enterprise signal

6. **Custody upgrade: env key → CDP MPC / KMS** behind the existing signer
   interface, with the spend ceiling pushed down to custody-level policy.
   Turns "it's a demo wallet" into a security-architecture answer.
7. **SLO surface** — promote tracing to OpenTelemetry + the four metrics that
   matter (p99 latency, gate pass-rate, settlement success, margin), still
   no-op without config. "How do you know it's healthy?" gets a real answer.
8. **Synthesizer-side untrusted-text check** — the eval now proves hostile
   source text can't move gated outcomes; the remaining item is the explicit
   engine-side treatment of source text as untrusted at the prompt boundary.

### Tier 4 — Pick exactly one scale story

Choose **one** Phase 8 item and finish it rather than sprawling: the
**semantic memo cache** (upgrade pgvector from hashed trigrams to real
embeddings — already a drop-in per ARCHITECTURE §6.3) is the highest
story-value pick, since it adds an ML-systems dimension (cache-hit economics:
every semantic hit is inference cost saved, measurable in the margin
dashboard). The durable-scheduler swap (ADR-0002) is the alternative if the
interview loop skews infra.

### Deliberately deferred

Multi-tenant config, prepaid balances/payment channels, policy-configurable
gates, compliance pack, white-label ([ROADMAP Phases 8–9](ROADMAP.md)) — all
real, none of them change an interview outcome until the live column above is
green. Knowing *why* they're deferred is itself the talking point: sequencing
under uncertainty is the skill being interviewed.

---

## Tough questions, honest answers

**"Is it live?"** — Testnet-proven end to end, with one mainnet settlement
that *found a billing bug* (now a named invariant with an eval asserting $0
for rejected runs). Closing the per-phase live exit runs is the current
track, deliberately sequenced before new features. *(Upgrade this answer to
"yes — here's /proof" by shipping Tier 1.)*

**"Isn't the gate just regex? What about semantic hallucinations?"** — The
gate polices *figures* deterministically, and that scope boundary is explicit
and tested (two eval cases pin that instruction-shaped prose alone is not the
gate's job). Semantic quality is layered on top: the completeness check
catches material omissions, the LLM judge runs a per-claim faithfulness
checklist, and the whole pipeline fails closed. The design bet is that the
*deterministic* core handles the failure mode that touches money — fabricated
numbers — and the probabilistic layers handle nuance, never the reverse.

**"Why x402 and not Stripe's agentic commerce stack?"** — ACP is
merchant-checkout-shaped with no path for independent agent *services*;
meanwhile Stripe itself pays over x402/USDC-on-Base. jim lives on the rail
both camps actually use, and its niche — verifiable *delivery* — is the layer
neither identity registries nor authorization mandates cover
([NORTH_STAR.md](NORTH_STAR.md)).

**"What breaks at 1000×?"** — Three named bottlenecks with designed answers:
per-call on-chain settlement → prepaid balances with periodic netting; the
asyncio monitor scheduler → a partitioned work queue (ADR-0002 calls it a
one-file swap); LLM cost → caching, Batch API routing, and a model cascade by
difficulty. None built yet — deliberately, see "deferred."

**"What would you do differently?"** — Cassette-based integration tests from
day one (upstream parsing changes currently ride on unit tests + live runs),
and custody-grade key management before the first mainnet settle rather than
after. Both are in the plan because they were felt, not theorized.

**"How do you know the LLM parts actually work?"** — Four eval tiers, cheapest
first: 98 deterministic offline cases (~1s, $0) as the merge gate, then a live
suite over held-out tickers measuring rubric score, faithfulness, latency,
tokens, and dollars — persisted per-run with git sha and config snapshot, with
thresholded regression verdicts against a named baseline. The live suite
caught a real silent regression in its first outing (war story #3).

---

## Pointers

| Need | Doc |
|---|---|
| The 3-minute demo script + listings + outreach | [LAUNCH.md](LAUNCH.md) |
| Go-live runbook | [DEPLOY.md](DEPLOY.md) |
| Strategy / market thesis | [NORTH_STAR.md](NORTH_STAR.md) |
| Buildable backlog (superset of this plan) | [ROADMAP.md](ROADMAP.md) |
| How it works, end to end | [ARCHITECTURE.md](ARCHITECTURE.md) + [SYSTEM_MAP.md](SYSTEM_MAP.md) |
| Why it's built this way | [adr/](adr/) |

# ADR-0008 — The agent economy: peer sourcing, verification-based trust, call-chain safety — and never billing rejected research

**Status:** Accepted

## Context

The first mainnet settlement exposed the gap between jim's trust story and its
billing mechanics: a customer (us, self-paying through the UI) paid real USDC
for a COIN fundamentals memo whose footer read *"paid · x402 … status
rejected"*. The payment settled; the research had failed the sourcing gate.
Two root causes, one incident:

1. **The gate false-rejected honest phrasing.** COIN-style filings produce
   negative quarters and guidance ranges; "a loss of \$1.2 billion" and
   "\$1.2–1.4 billion" both read as value mismatches to the old extractor. The
   run burned its retries and finished `rejected`.
2. **The seller charged anyway.** Handlers returned 200 with
   `status="rejected"`, and the x402 middleware settles any 2xx.

At the same time, [ROADMAP Phase 7](../ROADMAP.md) and
[AGENT_INTEROP](../AGENT_INTEROP.md) had already argued the *next* growth stage
is composition: jim buying specialized signals from peer agents and reselling
verified synthesis. Composition raises exactly the risks the incident
previewed — unverifiable claims, runaway spend, misplaced billing — so the fix
and the feature are one design.

## Decision

> Apply *model proposes, deterministic code disposes* at three new seams: what
> a memo may claim (a fuzz-hardened gate), what a buyer may be charged (never
> for rejected research), and how agents compose (verified peers, outcome-based
> trust, bounded call graphs).

### 1. Fuzz-hardened sourcing gate (Track 0 closed)

[gate.py](../../src/jim/research/gate.py) now extracts scientific notation
(`3.9e11`), word scales without grouping (`5 billion`), bare integer runs,
uppercase suffixes (`5B`), underscore grouping, spelled-out figures (`five
billion`, `twenty-five percent`), euro/pound symbols, and ranges
(`$1.2–1.4 billion` → both endpoints checked). Accounting negatives and loss
phrasing over negative facts match by magnitude, so true statements stop
false-rejecting. A Hypothesis suite
([test_gate_fuzz.py](../../tests/test_gate_fuzz.py)) pins both invariants —
*no fabricated figure passes; no formatter-rendered truth fails* — over random
values, not just examples.

### 2. The billing invariant: rejected research is never billed

The x402 payment middleware only settles 2xx responses; a verified payment on
an error response is **cancelled**. So the seller now *refuses* rejected runs:
HTTP handlers return 502 + structured diagnostics
([`_deliver_or_refuse`](../../src/jim/seller/app.py)), the MCP tool raises, and
even the free UI preview declines to render unverified output. The engine books
rejected runs at \$0 revenue so the margin ledger shows the true loss instead
of phantom income. The buyer keeps their money; the memo footer can no longer
read "paid … rejected".

### 3. Source-as-agent ([peer.py](../../src/jim/sources/peer.py))

A `PeerSource` buys cited facts from a peer agent over x402 through the **same**
`procure()` → budget → cache path as The Graph; `CompositeSource` merges peer
facts into a product's snapshot with renumbered citations and per-fact
`origins`. The sourcing gate then verifies peer figures exactly like EDGAR
figures — **the gate is the composition firewall**: jim can buy from agents it
does not trust and still only ship what it can verify. Peers are configuration
(`PEER_SOURCES` JSON), not code; a failing peer degrades to a sourcing note,
never a failed run. A mock peer vendor (`/mock-peer/research`) closes the
testnet loop end-to-end, mirroring `mock_graph`.

### 4. Trust = the gate pass-rate ([interop/trust.py](../../src/jim/interop/trust.py))

Every gated run attributes its outcome to the sources whose facts it used
(deterministic rule: a pass credits all contributors; a failure debits only
sources whose facts appear in a violation's citations — the synthesizer's own
uncited hallucinations blame no source). Events append to a
`source_trust_events` ledger; the score is the Laplace-smoothed pass-rate.
The buy path *routes* on it: a peer below `PEER_TRUST_FLOOR` (after
`PEER_TRUST_MIN_EVENTS` observations) is refused before any payment. Reputation
by verification, not by reviews — computed from outcomes jim observed itself.

### 5. Call-chain spend safety ([interop/callchain.py](../../src/jim/interop/callchain.py))

Every buy carries `X-Jim-Call-Chain` — the ordered list of paying agents with
jim's identity appended. Sell-side, the outermost middleware refuses (409,
pre-paywall, no money moves) any chain already containing jim's address (a
payment loop) or at `CALL_CHAIN_MAX_DEPTH`. Buy-side, jim never extends a chain
past the ceiling. This bounds the two cross-agent failure modes a per-query
budget cannot see: cycles and runaway depth. A peer that strips the header
escapes *its* obligations, not ours — the guard bounds jim's own participation,
which is the part jim controls.

## Consequences

- **Positive.** The mainnet incident class is closed twice over (fewer false
  rejects; rejects no longer billed). jim can now compose paid peer signals
  with bounded spend and an auditable, outcome-based reputation per source —
  the AGENT_INTEROP "general contractor" verdict is implemented, not just
  argued. All of it is offline-tested; no new live dependencies.
- **Negative / accepted.** The gate got more paranoid: prose like "the 401K
  plan" or idiomatic "a million reasons" now reads as a figure and will
  false-reject (the synthesizer retries with feedback; the fallback renderers
  never emit such forms). Trust events add one store write per contributing
  source per run. The call-chain header is cooperative — it protects against
  loops jim participates in, not against adversarial peers who forge chains
  (source identity/attestation stays on the roadmap).
- **Deferred.** Qualitative-claim verification across hops, refund/clawback
  attestations, OFAC screening of counterparties
  ([ENTERPRISE_VISION §3](../ENTERPRISE_VISION.md)), and A2A task delegation
  beyond the published agent card.

## See also

- [AGENT_INTEROP.md](../AGENT_INTEROP.md) — the design argument this implements
- [ROADMAP.md](../ROADMAP.md) — Track 0 + Phase 6/7 items this closes
- [ADR-0007](0007-data-source-economics-multichain-macro.md) — the price guard
  this composes with on every peer buy

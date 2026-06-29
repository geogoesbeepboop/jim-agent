# ADR-0005 — Settlement audit log + admin view, and a real browser-wallet checkout

**Status:** Accepted

## Context

Two gaps remained in the payment story after Phase 5 (see
[ADR-0004](0004-mainnet-cutover-and-ui-self-pay.md)):

1. **No on-chain audit trail.** `query_records` captures the *economics* of each
   run (price_out − data_cost − inference = margin), but nothing recorded *who
   paid us and on which transaction*. For a service that takes real USDC, "show
   me settled revenue, the buyer addresses, and the settlement tx for each sale"
   is table stakes — and it must reflect what **actually settled**, not what we
   intended to charge.
2. **Humans couldn't pay with their own wallet.** ADR-0004 shipped a UI that
   proved the rail by having jim **buy its own endpoint** (net-zero self-pay) or
   fall back to a labelled free preview. Honest, but it explicitly deferred the
   real thing: *"production swaps jim's wallet for a browser wallet … with the
   identical settlement path."* This ADR pays that down.

The structural constraint that shapes (1): the x402 payment middleware **settles
after the protected handler returns**, writing the receipt into the
`PAYMENT-RESPONSE` response header. So `run_research` (which runs *inside* the
handler) never sees the tx hash. The only correct capture point is on the way
back out, outside the payment middleware.

## Decision

> A thin **audit middleware** records one append-only settlement receipt per
> payment from the response header; a separate **admin dashboard** reads it; and
> the storefront offers a **real browser-wallet checkout** by wiring x402's
> bundled paywall — no hand-rolled crypto.

- **Audit log** ([audit.py](../../src/jim/seller/audit.py)). `PaymentAuditMiddleware`
  is mounted *outermost* (added last, so it wraps the payment middleware) and, on
  the response, decodes the base64 `PAYMENT-RESPONSE` header → `payer`,
  `transaction`, `network`, `amount`. It writes a `PaymentReceipt`
  (buyer + tx + settled USDC + which query) and **never raises into the response
  path** — auditing is best-effort observability and must not break a paid
  delivery (a DB outage logs and is swallowed; the buyer still gets their report).
  The decode/classify half is pure and header-only, so it unit-tests offline.
- **Two ledgers, on purpose.** `payment_receipts` (settlement/revenue: who, how
  much, which tx) is deliberately separate from `query_records` (economics:
  margin). They answer different questions and are populated at different layers
  (middleware vs. engine); fusing them would couple settlement capture to the
  engine and lose receipts for `/ping` and the mock-graph vendor.
- **Admin dashboard** ([admin.py](../../src/jim/admin.py), `jim-admin`,
  `GET /admin`, `GET /admin/audit`) — settled revenue, unique buyers, per-product
  breakdown, and the per-tx trail with Basescan links. Read-only, and distinct
  from `/dashboard` (the margin view) by design.
- **Browser-wallet checkout.** The seller wires x402's bundled `paywall_provider`
  (`create_paywall().with_network(evm_paywall)`), so a browser that hits a paid
  route unpaid is served x402's audited paywall — MetaMask / Coinbase Wallet /
  WalletConnect → real EIP-3009 signing → on-chain settlement. The gate is
  `_is_web_browser` (HTML `Accept` **and** a `Mozilla` UA), so **agents still get
  the machine-readable 402** — the paywall is purely additive for humans. The
  cleaner storefront keeps its free **Preview** (the ADR-0004 direct path) and
  adds a **Pay with wallet** action that opens the paid route to trigger the
  paywall. Choosing the bundled paywall over a hand-rolled in-page signer trades
  a little UX integration (the wallet step leaves our page) for a tested,
  multi-wallet implementation and zero browser-crypto maintenance.

## Consequences

**Positive**
- Every settled payment is now provable on-chain from jim's own records, captured
  authoritatively from the settlement receipt rather than inferred from intent.
- Humans pay with their own wallet over the *same* settlement path agents use; the
  net-zero self-pay demo is no longer the only human option.
- The audit layer is decoupled — it observes `/ping`, `/research/*`, and the
  vendor uniformly, and adding a paid route needs no audit wiring.

**Negative / trade-offs**
- The audit log trusts the facilitator's `PAYMENT-RESPONSE`; a facilitator that
  omits or malforms it yields no receipt (we fail open on observability, never on
  delivery). It is not an independent chain indexer — it records what the
  facilitator reported settled.
- The bundled paywall is a large static asset and renders on its own page, so the
  wallet flow leaves the custom storefront (acceptable: Preview stays in-page).
- `payment_receipts` is append-only and uncapped; a high-traffic deployment will
  want retention/rollup (out of scope here).
- New table → existing deployments must re-run `jim-initdb` (idempotent
  `create_all`) before the admin view populates.

## ELI5 / what I learned

Before, jim's books showed *how much money it should have made per job*, but not
*who actually paid and with which receipt*. The catch: the payment clears only
**after** the work is handed back, and the receipt is stapled to the very last
envelope going out the door — so the worker inside never sees it. The fix is a
clerk standing at the exit who reads that stapled receipt and writes it in a
ledger; if the clerk trips, the customer still walks out with their report. And
for the shop's website, instead of jim paying its own till to prove the card
reader works, a real person can now tap their own wallet — same till, same wire,
just their money. The lesson: *capture the receipt where the receipt actually is
(the way out), keep the audit clerk unable to block the door, and for real
payments reuse the wallet flow that already exists instead of rebuilding the
crypto yourself.*

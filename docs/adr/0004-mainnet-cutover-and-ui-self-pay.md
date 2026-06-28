# ADR-0004 — Guarded mainnet cutover (preflight, no auto-spend) + server-side UI self-pay

**Status:** Accepted

## Context

Phase 5's last goal is "cut over to Base mainnet with real USDC; measure real
facilitator fees + minimums," and "ship a thin human UI that pays via x402 under
the hood." Both touch the one genuinely irreversible thing jim does — moving real
money — so they need deliberate guardrails, not just a config flip.

Two specific risks:

1. **Cutover by surprise.** Flipping `NETWORK=eip155:8453` while the facilitator
   is still the free testnet one (which cannot settle on mainnet), or with a
   pay-to address unset, or with prices below the facilitator's minimum, would
   fail real settlements — discovered the expensive way, in production.
2. **The "pays under the hood" UI.** A browser visitor doesn't hold USDC, and we
   won't ask them to. But the demo must still exercise the *real* x402 path, not a
   fake checkout, or it proves nothing.

## Decision

> A read-only mainnet **preflight** that reports and never spends, plus a UI that
> proves the rail by having jim **buy its own endpoint** when funded — degrading
> to a labelled preview otherwise.

- **Preflight** ([mainnet.py](../../src/jim/marketplace/mainnet.py),
  `jim-market mainnet`, `GET /mainnet/readiness`): a deterministic checklist with
  `ok`/`info`/`warn`/`fail` rungs. A testnet-facilitator-on-mainnet or a missing
  pay-to address is a hard **`fail`** (blocks a clean cutover); still being on
  testnet, or no gas, is a **`warn`**. If `MAINNET_RPC_URL` is set it additionally
  *reads* ETH (gas) and USDC balances over a read-only JSON-RPC call — but it
  never signs, sends, or settles anything. `ready` ⇔ no `fail`.
- **Facilitator economics** are operator-provided inputs (`FACILITATOR_MIN_USDC`,
  `FACILITATOR_FEE_BPS`) the preflight checks our prices against, surfacing
  products that would price below the settleable minimum — answering "measure real
  facilitator fees + minimums" without guessing them.
- **UI self-pay** ([ui.py](../../src/jim/marketplace/ui.py)): `POST /ui/checkout`
  settles a real x402 payment by routing through the existing buy client to jim's
  *own* paid endpoint — only when a wallet is funded **and** `UI_SETTLE_VIA_X402`
  is on. Otherwise it runs the engine directly and returns a result clearly
  labelled `paid=false`, `settled_via="direct"` (a preview). Either way it's just
  another caller of `run_research`, so the **same sourcing gate** decides what
  ships.
- **The buy leg was already mainnet-capable** (Phase 2's `GRAPH_LIVE` buys real
  USDC on Base mainnet), so Phase 5 is really only the *sell-leg* cutover plus
  these guardrails — the legs stay independent (different network/wallet per side,
  per [ARCHITECTURE §8](../ARCHITECTURE.md#8-payments--x402-v2-deep-dive)).

## Consequences

**Positive**
- The irreversible step gets a dry-run: an operator sees every blocker before
  flipping the network, and the preflight is safe to run anytime (it can't spend).
- The UI demonstrates a genuine on-chain settlement with no visitor wallet, while
  staying honest about when it's a preview vs. a paid call.
- No new trust surface: the UI and MCP and HTTP all funnel through one
  `run_research` and one gate.

**Negative / trade-offs**
- Facilitator min/fee are operator-supplied, not discovered from the facilitator,
  so a stale value gives a stale check.
- UI self-pay means jim pays *itself* (net-zero economics) — it proves the rail,
  not real third-party revenue; production swaps jim's wallet for a browser wallet
  (x402 extension / WalletConnect) with the identical settlement path.
- The balance probe trusts whatever `MAINNET_RPC_URL` returns and is best-effort
  (a flaky RPC degrades to a `warn`, never a crash).

## ELI5 / what I learned

Going to mainnet means real dollars, and that's the one move I can't undo. So
before the switch I built a checklist that *only looks* — it tells me "your
receiving address is set, but your money-checker is still the testnet one that
can't actually pay you" — and it never touches a cent. For the website, a normal
person doesn't have a crypto wallet, but I still wanted a *real* payment to happen
so the demo isn't a lie: so jim quietly pays its own shop counter with its own
wallet, and if there's no money loaded it just shows a clearly-labelled free
preview instead. The lesson: *for the one irreversible action, build a dry-run
that can't fire the gun, and when you demo the real thing, make it real but
honest about when it isn't.*

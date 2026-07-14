# ADR-0010 — Dual-mode Claude auth: subscription for the dev-loop, API key pinned for production

**Status:** Accepted

## Context

jim's engine calls Claude through the raw `anthropic` SDK
(`AsyncAnthropic(...).messages.create`) at three engine sites (synthesize,
judge, debate) plus two monitor sites, each reading `ANTHROPIC_API_KEY`. Every
live run — including the eval `live` suite, which drives the same engine —
spends metered API credits. The ask: run evals / local research often without
burning credits, by leveraging a Claude **subscription** (`claude login`).

Two hard constraints shaped the design:

1. **The raw SDK cannot use subscription auth.** Anthropic's public Messages API
   rejects Claude-subscription OAuth tokens (`"OAuth authentication is currently
not supported"`). The only sanctioned mechanical path is the **Claude Agent
   SDK** (`claude-agent-sdk`), which spawns the `claude` CLI as a subprocess and
   inherits its `claude login` session / `CLAUDE_CODE_OAUTH_TOKEN`.
2. **ToS forbids subscription behind a service sold to others.** Anthropic
   sanctions subscription auth for _your own individual use_ only, and actively
   enforces this (2026 third-party-harness bans). jim's seller path backs paid
   x402 output to third parties — subscription there is the prohibited case, at
   account-ban risk. It also can't scale: subscription rate limits are a rolling
   session window sized for one interactive user, not concurrent buyer traffic.

So subscription auth is legitimate for exactly the stated goal — _your own_
evals and local `jim-research` — and illegitimate for anything customer-facing.

## Decision

A single client factory (`jim.llm`) with two backends behind one coroutine
(`complete(model, system, user, max_tokens) -> LLMResponse`):

- **`api_key`** — the raw `anthropic` SDK. Unchanged semantics (ephemeral
  prompt-cache block, real token usage). The production default and the only
  path for paid, third-party-facing output.
- **`subscription`** — the Claude Agent SDK. Spawns the `claude` CLI using your
  login; **strips `ANTHROPIC_API_KEY` from the subprocess env** so it can't
  silently shadow the subscription token. Best-effort token usage from
  `ResultMessage.usage`. An **optional extra** (`uv sync --extra subscription`),
  not a core dependency — the default path and offline suite need none of it.

Mode is `Settings.llm_auth_mode` (`api_key` default | `subscription` | `auto`);
`auto` prefers subscription when a credential is detectable, else api_key.
`jim-eval run` and `jim-research` take `--auth-mode` to opt in per-invocation.

**The seller and monitor entrypoints call `pin_api_key_mode()` at startup** — a
deterministic, process-level pin that overrides config, env, `--auth-mode`, and
even an explicit mode argument. This is the guard that keeps subscription off the
paid path regardless of misconfiguration, in the spirit of "the model proposes,
deterministic code disposes." `build_app()` pins too (defense-in-depth for any
ASGI deployment); tests reset the pin per-test.

## Consequences

- **Evals/local research can run on your subscription** (`jim-eval run --suite
live --auth-mode subscription`, `jim-research AAPL --auth-mode subscription`)
  without spending API credits — the stated goal.
- **The seller/monitor paths are unchanged and provably API-key-only.** No paid
  output can be routed through a subscription credential.
- **Cost metrics under subscription are notional.** There is no per-token charge
  on a subscription, so `inference_cost_usd` reflects the API-equivalent (0 when
  usage isn't reported). Eval run docs record `llm_auth_mode` + `has_subscription`
  so cost/regression comparisons stay mode-aware.
- **Subprocess cost.** The Agent SDK spawns the `claude` CLI per call (~seconds
  of overhead) — fine for a dev-loop, another reason it stays off the latency-
  sensitive seller path.
- **macOS detection gap.** `claude login` on macOS stores creds in the Keychain,
  which `auto` can't cheaply probe; on macOS use `--auth-mode subscription` or
  export a `CLAUDE_CODE_OAUTH_TOKEN` (from `claude setup-token`).
- **Point-in-time.** Anthropic's enforcement posture is evolving; re-check before
  relying on subscription auth long-term. It is never used for anything jim sells.

## Alternatives considered

- **Route the raw SDK at a subscription OAuth token.** Rejected: the public API
  rejects it; header-replication hacks are unstable and actively closed off.
- **Subscription everywhere (incl. seller).** Rejected: ToS violation, account-
  ban risk, and a rate-limit ceiling incompatible with concurrent buyers.
- **API-key-only eval-cost optimization** (cheaper judge, wider caching). Useful
  but doesn't use the subscription; orthogonal, can still be done later.

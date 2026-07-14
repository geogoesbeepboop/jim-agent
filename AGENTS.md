# jim — agent instructions

## What this is

An impersonal, fully-cited financial research service that **sells over x402 and
pays for its own data over x402** — every published number must trace to a
public-domain primary source (SEC EDGAR, The Graph, gov macro data, peer agents).

## Run / verify

```bash
uv sync --extra dev            # install (Python 3.12+, uv is the canonical runner)
uv run pytest                  # full offline suite — hermetic by design: no DB,
                               #   wallet, network, or API key (conftest.py neutralizes .env)
uv run ruff check .            # lint (line-length 100, py312)
uv run jim-seller              # serve on :4021 (/ping, /research/*, /dashboard, /admin)
uv run jim-research AAPL       # CLI research (needs ANTHROPIC_API_KEY for live synthesis)
uv run jim-eval --gate-only    # offline: planted hallucinations must be blocked
docker compose up -d           # local Postgres + pgvector (only for live/DB runs)
```

Health gate: `.claude/gate.sh` (lint + fast tests, <120s) — runs automatically on
`git commit` via the global guard-commit hook; `git commit --no-verify` skips it deliberately.

## Architecture map (files that matter)

- `src/jim/config.py` — env-driven settings; network defaults to Base Sepolia (`eip155:84532`)
- `src/jim/llm.py` — dual-mode Claude client factory: `api_key` (production, raw SDK) vs
  `subscription` (dev-loop, Claude Agent SDK / `claude login`); seller/monitors pin api_key (ADR-0010)
- `src/jim/research/gate.py` — **the sourcing gate**: deterministic, no-LLM check that every
  figure in a memo matches a cited `Fact`; fuzz-hardened (`tests/test_gate_fuzz.py`)
- `src/jim/research/engine.py` — LangGraph pipeline: gather → memo-cache → synthesize →
  gate (retry) → judge; fails closed
- `src/jim/seller/app.py` + `seller/audit.py` — FastAPI paywall (x402) + settlement receipts
- `src/jim/buyer/client.py` — x402 buy client (pays 402s, reports cost + tx)
- `src/jim/sources/` — EDGAR + macro (free), The Graph (paid), `peer.py` (buy from peer agents)
- `src/jim/interop/` — Phase 7: call-chain loop/depth refusal, per-source trust ledger
- `src/jim/monitors/` — scheduled diff-driven monitors behind a deterministic materiality gate
- `src/jim/marketplace/` — catalog, pricing tiers, x402 Bazaar discovery, MCP server, UI
- `src/jim/store/` — Postgres+pgvector cache/ledger (in-memory fallback when `DATABASE_URL` unset)
- `docs/ARCHITECTURE.md` — the deep dive; `docs/SYSTEM_MAP.md` / `uv run jim-map` — Mermaid map

## Invariants (never bypass)

- **The model proposes, deterministic code disposes.** Every LLM output that touches
  money, alerts, or published figures passes a deterministic gate first.
- **The sourcing gate is never weakened or skipped.** No memo ships with an uncited or
  mismatched figure; gate-rejected research is **refused, never billed** (ADR-0008).
- **No payment before verification.** Call-chain loop/depth checks run before money moves;
  the dynamic-price cap guard refuses over-budget x402 prices; per-query budget is a ceiling.
- **Offline-first.** Every feature ships fully tested with no key/wallet/network/DB; the one
  live "exit run" per phase is the only unchecked box.
- **Impersonal.** Research and monitor output stays general — never personalized advice.
- **Paid output is API-key-authed.** The seller + monitors pin `api_key` auth at startup
  (`jim.llm.pin_api_key_mode`); Claude-subscription auth is dev-loop only (evals, local
  `jim-research`) and must never back third-party-facing output — Anthropic ToS (ADR-0010).

## DO NOT

- Do not commit `.env`, private keys, or wallet material (a real `.env` exists locally).
- Do not point tests or demos at Base mainnet (`eip155:8453`) — mainnet runs are deliberate,
  operator-driven cutovers (`jim-market mainnet` preflight first).
- Do not add network/DB/API-key requirements to the default `pytest` suite.
- Do not modify `tests/conftest.py` hermeticity overrides to make a test pass.
- Do not spend real funds: no live settlements, faucet drains, or paid Graph queries in tests.

## Status & roadmap

Current phase: **Phase 7 — the agent economy** (+ Track 0 / Phase 6 hardening);
Phases 0–5 done. Backlog: `docs/ROADMAP.md`; phased plan: `docs/BUILD_PLAN.md`;
decisions: `docs/adr/`.

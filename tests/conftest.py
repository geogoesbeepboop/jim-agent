"""Test-suite guardrails: keep the offline suite hermetic.

The suite is designed to run with **no** Postgres, API key, or wallet (see the
README). But a developer's local ``.env`` often sets ``DATABASE_URL`` (a live
Postgres) and ``ANTHROPIC_API_KEY`` — and pydantic-settings reads ``.env``. Left
alone, the store-touching tests would then hit that real database (accumulating
state across runs → spurious failures like ``billable_queries`` drift) and the
research tests could spend real credits.

Neutralising these at import time — before any ``jim`` module constructs its
cached ``Settings`` — forces the in-memory store and the no-key code paths, so
``uv run pytest`` is reproducible on any machine. Real integration runs against
Postgres set ``DATABASE_URL`` explicitly outside the suite.
"""

from __future__ import annotations

import os

import pytest

# Env vars win over .env in pydantic-settings, so an empty value here overrides a
# developer's .env. Empty DATABASE_URL is falsy → get_store() picks MemoryStore.
os.environ["DATABASE_URL"] = ""
# Empty (not popped) — pydantic-settings prefers a real os.environ value over
# .env, but an *absent* key still falls through to .env's real value. An empty
# string is a present-but-falsy override, so it actually neutralizes them.
os.environ["ANTHROPIC_API_KEY"] = ""
# A developer's `claude login` / subscription token must not leak into the offline
# suite, and the auth mode is pinned to the production default. (Strengthens the
# hermeticity the rest of this file establishes.)
os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = ""
os.environ["LLM_AUTH_MODE"] = "api_key"
os.environ["CDP_API_KEY_ID"] = ""
os.environ["CDP_API_KEY_SECRET"] = ""
# A developer's configured peer agents (Phase 7) must not leak into the suite —
# tests construct their own PeerSpec/PeerSource explicitly.
os.environ["PEER_SOURCES"] = ""
# Pin the network to the documented offline default (Base Sepolia, config.py's
# BASE_SEPOLIA). A .env set to Base mainnet (eip155:8453) breaks the paywall/
# discovery/call-chain tests — the testnet facilitator rejects mainnet routes.
os.environ["NETWORK"] = "eip155:84532"
# .env's true would make the UI checkout tests need a funded wallet instead of
# the offline direct-research path; false is the documented offline default.
os.environ["UI_SETTLE_VIA_X402"] = "false"


@pytest.fixture(autouse=True)
def _reset_llm_process_state():
    """Keep the process-level auth pin/override from leaking between tests.

    ``build_app`` and the seller/monitor entrypoints pin api_key mode for the whole
    process; a test exercising them must not silently pin later factory tests. Reset
    both globals before every test so ordering never matters.
    """
    import jim.llm as llm

    llm._PINNED_API_KEY = False
    llm._MODE_OVERRIDE = None
    yield
    llm._PINNED_API_KEY = False
    llm._MODE_OVERRIDE = None

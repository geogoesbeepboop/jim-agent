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

# Env vars win over .env in pydantic-settings, so an empty value here overrides a
# developer's .env. Empty DATABASE_URL is falsy → get_store() picks MemoryStore.
os.environ["DATABASE_URL"] = ""
os.environ.pop("ANTHROPIC_API_KEY", None)

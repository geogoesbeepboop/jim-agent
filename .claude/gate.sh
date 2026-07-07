#!/usr/bin/env bash
# Health gate — runs on every `git commit` via the global guard-commit hook.
# Fast (<120s target; ~15s in practice): lint + the full offline test suite.
# Skip deliberately with `git commit --no-verify`.
set -euo pipefail
cd "$(dirname "$0")/.."

# Runner: prefer the project venv's binaries directly — `uv run` takes the global
# uv cache lock (~/.cache/uv/.lock), so a concurrent uv in any worktree can stall
# the gate indefinitely (observed 2026-07-07: 9s -> 2247s in the nightly digest).
# Fall back to uv only for fresh clones with no .venv yet.
if [ -x .venv/bin/python ]; then
  run() { ".venv/bin/$1" "${@:2}"; }
elif command -v uv >/dev/null 2>&1; then
  run() { uv run --no-sync "$@"; }
else
  echo "gate: neither .venv nor uv found — run 'uv sync --extra dev' first" >&2
  exit 1
fi

echo "gate: ruff check"
run ruff check .

# Full offline suite (~10s, 338 tests) — no DB/network/API-key/wallet needed by
# design, and the hypothesis fuzz tests are derandomized + bounded, so nothing
# is excluded for speed. -p no:cacheprovider keeps the gate from writing
# .pytest_cache state during a commit.
echo "gate: pytest (offline suite)"
run pytest -q -p no:cacheprovider

echo "gate: OK"

#!/usr/bin/env bash
# Offline eval suite — picked up by the nightly digest convention (like gate.sh
# is by guard-commit). Runs the deterministic suites only (gate + guards +
# scenarios: no key, no DB, no network, no spend; ~2s) and persists the run
# under ./eval_runs (gitignored) so `jim-eval ui` can plot trends. Any failing
# offline case exits 1; with a baseline set (`jim-eval baseline set <id>`),
# --compare-baseline also exits 1 on regression vs that baseline.
set -euo pipefail
cd "$(dirname "$0")/.."

# Runner: uv is the repo's canonical runner (README: `uv run jim-eval`). Fall
# back to the project venv for shells where uv isn't on PATH (e.g. nightly/hook
# environments that don't source ~/.zprofile). --no-sync: must not mutate .venv.
if command -v uv >/dev/null 2>&1; then
  run() { uv run --no-sync "$@"; }
elif [ -x .venv/bin/python ]; then
  run() { ".venv/bin/$1" "${@:2}"; }
else
  echo "evals: neither uv nor .venv found — run 'uv sync --extra dev' first" >&2
  exit 1
fi

# jim-eval runs without tests/conftest.py, so neutralize the same .env leakage
# the test suite does (see tests/conftest.py): empty values override .env in
# pydantic-settings, forcing the in-memory store and the no-key/testnet paths.
# The offline suites need none of these — this guarantees zero credentials,
# zero cost, and reproducible results whatever the local .env says.
export DATABASE_URL=""
export ANTHROPIC_API_KEY=""
export CDP_API_KEY_ID=""
export CDP_API_KEY_SECRET=""
export PEER_SOURCES=""
export NETWORK="eip155:84532"
export UI_SETTLE_VIA_X402="false"

echo "evals: jim-eval (offline suites)"
run jim-eval run --suite offline --compare-baseline --label nightly

echo "evals: OK"

#!/usr/bin/env bash
# Start the jim dev stack.
# Usage: ./dev.sh [--monitors] [--mcp] [--no-db]
#   --monitors   also start jim-monitor serve (scheduler loop)
#   --mcp        also start jim-mcp on :4022 (run `uv sync --extra mcp` first)
#   --no-db      skip docker compose (use when Postgres is already running)

set -euo pipefail

MONITORS=false
MCP=false
DB=true

for arg in "$@"; do
  case $arg in
    --monitors) MONITORS=true ;;
    --mcp)      MCP=true ;;
    --no-db)    DB=false ;;
    -h|--help)
      sed -n '2,6p' "$0" | sed 's/^# //'
      exit 0
      ;;
    *)
      echo "Unknown flag: $arg  (try --help)" >&2
      exit 1
      ;;
  esac
done

PIDS=()
cleanup() {
  echo ""
  echo "Stopping jim stack..."
  for pid in "${PIDS[@]:-}"; do
    kill "$pid" 2>/dev/null || true
  done
  wait "${PIDS[@]:-}" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# ── Postgres ─────────────────────────────────────────────────────────────────
if $DB; then
  echo "▶ Starting Postgres..."
  docker compose up -d
  printf "  Waiting for db to be ready"
  until docker compose exec -T db pg_isready -U jim -d jim -q 2>/dev/null; do
    printf "."
    sleep 1
  done
  echo " ready"
fi

# ── Seller ───────────────────────────────────────────────────────────────────
echo "▶ Starting jim-seller → http://localhost:4021"
uv run jim-seller &
PIDS+=($!)

# ── Optional: monitor scheduler ──────────────────────────────────────────────
if $MONITORS; then
  echo "▶ Starting jim-monitor serve"
  uv run jim-monitor serve &
  PIDS+=($!)
fi

# ── Optional: MCP server ─────────────────────────────────────────────────────
if $MCP; then
  if ! uv run python -c "import mcp" 2>/dev/null; then
    echo "  MCP extras not installed — run: uv sync --extra mcp" >&2
    exit 1
  fi
  echo "▶ Starting jim-mcp → :4022"
  uv run jim-mcp &
  PIDS+=($!)
fi

echo ""
echo "Stack running. Press Ctrl+C to stop all processes."
wait

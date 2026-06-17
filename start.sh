#!/usr/bin/env bash
# =============================================================================
# TCG Software — WSL / Linux launcher
# =============================================================================
# Starts the FastAPI backend (uvicorn via `python -m tcg.core`) and the Vite
# frontend dev server, then waits on the backend /health probe. Ctrl-C stops
# both. Requires `uv` (Python env) and `npm` (frontend) on PATH.
#
# Env (read from ./.env by the app at runtime — never committed):
#   DWH_*        read-only market-data role (tcg_read)
#   APP_DB_*     read-write app-data role (tcg_app_rw)
#   TCG_CORS_ORIGINS (optional; defaults to the Vite dev origin)
# =============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

HOST="${TCG_HOST:-127.0.0.1}"
PORT="${TCG_PORT:-8000}"
LOG_LEVEL="${TCG_LOG_LEVEL:-info}"

if ! command -v uv >/dev/null 2>&1; then
  echo "ERROR: 'uv' not found on PATH. Install it: https://docs.astral.sh/uv/" >&2
  exit 1
fi
if ! command -v npm >/dev/null 2>&1; then
  echo "ERROR: 'npm' not found on PATH. Install Node.js (>=18)." >&2
  exit 1
fi

echo "[tcg] syncing Python dependencies (uv sync)…"
uv sync

echo "[tcg] installing frontend dependencies (npm install)…"
(cd frontend && npm install)

# --- Process management: start both, kill both on exit ----------------------
BACK_PID=""
FRONT_PID=""
cleanup() {
  echo
  echo "[tcg] shutting down…"
  [ -n "$FRONT_PID" ] && kill "$FRONT_PID" 2>/dev/null || true
  [ -n "$BACK_PID" ] && kill "$BACK_PID" 2>/dev/null || true
  wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

echo "[tcg] starting backend on http://${HOST}:${PORT} …"
uv run python -m tcg.core --host "$HOST" --port "$PORT" --log-level "$LOG_LEVEL" &
BACK_PID=$!

# Wait for the backend readiness probe before launching the UI.
echo "[tcg] waiting for backend /health …"
for _ in $(seq 1 60); do
  if curl -fsS "http://${HOST}:${PORT}/health" >/dev/null 2>&1; then
    echo "[tcg] backend ready."
    break
  fi
  # Bail early if the backend process died.
  if ! kill -0 "$BACK_PID" 2>/dev/null; then
    echo "ERROR: backend exited during startup — check the logs above." >&2
    exit 1
  fi
  sleep 1
done

echo "[tcg] starting Vite dev server (frontend)…"
(cd frontend && npm run dev) &
FRONT_PID=$!

echo "[tcg] backend pid=$BACK_PID, frontend pid=$FRONT_PID. Ctrl-C to stop."
# Wait on either process; cleanup() handles the rest.
wait -n "$BACK_PID" "$FRONT_PID"

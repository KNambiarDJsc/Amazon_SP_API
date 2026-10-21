#!/usr/bin/env bash
# Boots the real backend for end-to-end tests: PostgreSQL + Redis (supplied by the caller), the
# Dramatiq worker and the API, running the offline Amazon mock. Stays in the foreground so
# Playwright can manage it; stopping it stops both processes.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
PY="${E2E_PYTHON:-$ROOT/.venv/bin/python}"
: "${E2E_DATABASE_URL:?set E2E_DATABASE_URL, e.g. postgresql+psycopg://postgres@localhost:5433/eligibility_e2e}"
: "${E2E_REDIS_URL:?set E2E_REDIS_URL, e.g. redis://localhost:6380/5}"
PORT="${E2E_API_PORT:-8788}"

export DATABASE_URL="$E2E_DATABASE_URL" REDIS_URL="$E2E_REDIS_URL"
export AMAZON_MODE=mock PYTHONPATH="$ROOT/backend"
export EXPORT_DIR="$ROOT/frontend/e2e/.state/exports"
# 12-check jobs must take the background-export path, 9-check jobs the direct one.
export EXPORT_SYNC_MAX_CHECKS=11
export RATE_SEARCH_CATALOG_PER_SEC=1000 RATE_SEARCH_CATALOG_BURST=1000
export RATE_RESTRICTIONS_PER_SEC=1000 RATE_RESTRICTIONS_BURST=1000

cd "$ROOT"
"$PY" scripts/e2e_prepare.py

"$PY" -m dramatiq app.workers.main --processes 1 --threads 4 >/dev/null 2>&1 &
WORKER=$!
"$PY" -m uvicorn app.api.main:app --host 127.0.0.1 --port "$PORT" --log-level warning &
API=$!

trap 'kill "$WORKER" "$API" 2>/dev/null || true; wait 2>/dev/null || true' EXIT
trap 'exit 0' TERM INT
wait -n "$WORKER" "$API"

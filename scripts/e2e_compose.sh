#!/usr/bin/env bash
# Acceptance run: the dashboard's Playwright suite against the real docker compose deployment
# (Caddy -> API -> worker -> PostgreSQL/Redis), on the offline Amazon mock.
#
#   scripts/e2e_compose.sh            # start (or reuse) the stack, seed two sellers, run the suite
#   scripts/e2e_compose.sh --down     # same, then stop the stack and delete its volumes
#
# Optional: ENV_FILE=path (default .env.e2e, created with random secrets if missing), HTTP_PORT.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

ENV_FILE="${ENV_FILE:-.env.e2e}"
export HTTP_PORT="${HTTP_PORT:-8080}"
if [ ! -f "$ENV_FILE" ]; then
  {
    echo "POSTGRES_PASSWORD=$(head -c 24 /dev/urandom | base64 | tr -dc 'A-Za-z0-9')"
    echo "REDIS_PASSWORD=$(head -c 24 /dev/urandom | base64 | tr -dc 'A-Za-z0-9')"
    echo "AMAZON_MODE=mock"
  } > "$ENV_FILE"
fi
compose() { docker compose --env-file "$ENV_FILE" -f docker-compose.yml -f docker-compose.e2e.yml "$@"; }

compose up -d --wait

mkdir -p frontend/e2e/.state
seed() { # prints the API key of a newly created seller
  compose exec -T api python -m app.cli create-seller --name "$1" | grep -o 'spk_[A-Za-z0-9_-]*'
}
KEY_A="$(seed 'E2E Seller A')"
KEY_B="$(seed 'E2E Seller B')"
printf '{"a":"%s","b":"%s"}' "$KEY_A" "$KEY_B" > frontend/e2e/.state/keys.json

DOWN=0
[ "${1:-}" = "--down" ] && { DOWN=1; shift; }

status=0
(cd frontend && E2E_BASE_URL="http://127.0.0.1:${HTTP_PORT}" npx playwright test "$@") || status=$?

[ "$DOWN" = 1 ] && compose down -v
exit "$status"

# Shared by the ops scripts. Source it; do not run it.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

# COMPOSE_FILES: extra compose files, e.g. "-f docker-compose.yml -f docker-compose.prod.yml"
# ENV_FILE: the env file the stack was started with (default .env)
ENV_FILE="${ENV_FILE:-.env}"
COMPOSE_FILES="${COMPOSE_FILES:--f docker-compose.yml}"
# shellcheck disable=SC2086
compose() { docker compose --env-file "$ENV_FILE" $COMPOSE_FILES "$@"; }

BACKUP_DIR="${BACKUP_DIR:-$ROOT/backups}"
DB_NAME="${DB_NAME:-eligibility}"
DB_USER="${DB_USER:-eligibility}"

die() { echo "error: $*" >&2; exit 1; }

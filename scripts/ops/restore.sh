#!/usr/bin/env bash
# Restore a backup made by backup.sh.
#
#   scripts/ops/restore.sh backups/eligibility-20261021T220000Z.dump --into eligibility_check
#       Restores into a SEPARATE scratch database and prints its row counts. Touches nothing live.
#       Use it to prove a backup is restorable.
#
#   scripts/ops/restore.sh backups/eligibility-20261021T220000Z.dump --replace-live
#       Stops the API, workers and web, REPLACES the live database with the backup, migrates it to
#       the running code's schema, and starts everything again. Destructive.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

dump="${1:-}"
mode="${2:-}"
[ -f "$dump" ] || die "usage: restore.sh <dump file> (--into <scratch db name> | --replace-live)"

psql_admin() { compose exec -T postgres psql -U "$DB_USER" -d postgres -v ON_ERROR_STOP=1 "$@"; }

restore_into() { # <database name>
  local db="$1"
  psql_admin -c "DROP DATABASE IF EXISTS \"$db\"" -c "CREATE DATABASE \"$db\"" >/dev/null
  compose exec -T postgres pg_restore -U "$DB_USER" -d "$db" --no-owner --exit-on-error <"$dump"
}

row_counts() { # <database name>: "table count" for every table, sorted. One query, no loop.
  compose exec -T postgres psql -U "$DB_USER" -d "$1" -At -F ' ' -v ON_ERROR_STOP=1 -c "
    SELECT table_name,
           (xpath('/row/c/text()', query_to_xml(
             format('SELECT count(*) AS c FROM %I.%I', table_schema, table_name), false, true, '')))[1]::text
    FROM information_schema.tables
    WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
    ORDER BY 1"
}

case "$mode" in
  --into)
    scratch="${3:-}"
    [ -n "$scratch" ] || die "--into needs a database name"
    [ "$scratch" != "$DB_NAME" ] || die "--into must not be the live database; use --replace-live"
    restore_into "$scratch"
    echo "restored into scratch database '$scratch'. Row counts:"
    row_counts "$scratch"
    echo "drop it when done:  docker compose exec postgres psql -U $DB_USER -d postgres -c 'DROP DATABASE \"$scratch\"'"
    ;;
  --replace-live)
    echo "This will REPLACE the live database '$DB_NAME' with $dump."
    read -r -p "Type the database name to confirm: " answer
    [ "$answer" = "$DB_NAME" ] || die "not confirmed"
    compose stop web api worker
    psql_admin -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '$DB_NAME' AND pid <> pg_backend_pid()" >/dev/null
    restore_into "$DB_NAME"
    # The dump may come from an older release: bring its schema up to the running code's.
    compose run --rm migrate
    compose up -d --wait
    echo "restored. Jobs that were mid-flight resume on their own (see docs/OPERATIONS.md)."
    ;;
  *)
    die "usage: restore.sh <dump file> (--into <scratch db name> | --replace-live)"
    ;;
esac

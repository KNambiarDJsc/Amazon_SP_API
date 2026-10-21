#!/usr/bin/env bash
# Logical backup of the PostgreSQL database (the source of truth for jobs, checks, evidence and
# audit history) into backups/eligibility-<UTC timestamp>.dump, verified and pruned.
#
#   scripts/ops/backup.sh
#   RETENTION=30 BACKUP_DIR=/mnt/backups scripts/ops/backup.sh
#
# What this does NOT cover (see docs/OPERATIONS.md): the CREDENTIAL_ENCRYPTION_KEYS in your env
# file, without which stored Amazon refresh tokens cannot be decrypted. Keep those separately.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

RETENTION="${RETENTION:-14}" # number of dumps to keep
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
final="$BACKUP_DIR/eligibility-$stamp.dump"
partial="$final.partial"

mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"
trap 'rm -f "$partial"' EXIT

# Custom format: compressed, and restorable selectively and in parallel.
compose exec -T postgres pg_dump -U "$DB_USER" -d "$DB_NAME" --format=custom --no-owner >"$partial"

# A dump that cannot be listed is not a backup.
if ! compose exec -T postgres pg_restore --list <"$partial" >/dev/null; then
  die "the dump failed verification and was discarded"
fi
tables="$(compose exec -T postgres pg_restore --list <"$partial" | grep -c ' TABLE DATA ' || true)"
[ "$tables" -gt 0 ] || die "the dump contains no table data; refusing to keep it"

chmod 600 "$partial"
mv "$partial" "$final"
echo "backup written: $final ($(du -h "$final" | cut -f1), $tables tables with data)"

# Retention: newest N survive.
ls -1t "$BACKUP_DIR"/eligibility-*.dump 2>/dev/null | tail -n +"$((RETENTION + 1))" | while read -r old; do
  rm -f -- "$old" && echo "pruned: $old"
done

#!/bin/sh
# Restores the live database from one backup (CHAT-54). Runs on the server in the deploy folder. Destructive: every
# change after that backup is lost, so it asks for the backup's file name typed again unless KN_YES=1.
# Usage: restore.sh backups/kisaniti-<UTC>.dump
# The services that write (API, assessor, website, ingest) are stopped first, the database is dropped and recreated,
# the dump is restored, and the release that was running is started again. Recorded in backups/backup.log.
set -eu
cd "${KN_DIR:-$HOME/kisaniti}"
F="${1:-}"
fail() { echo "FAIL: $*" >&2; exit 1; }
[ -f "$F" ] || fail "no such backup: '$F'"
grep -q " $F OK " backups/backup.log 2>/dev/null || fail "$F did not pass its restore test (see backups/backup.log)"
if [ "${KN_YES:-}" != 1 ]; then
  printf 'This replaces the live database with %s. Type the file name again to go on: ' "$F"; read -r again
  [ "$again" = "$F" ] || fail "names differ; nothing changed"
fi
WRITERS="$(docker compose ps --services --status running | grep -v -x -e db -e caddy || true)"
# shellcheck disable=SC2086
[ -z "$WRITERS" ] || docker compose stop $WRITERS >/dev/null
docker compose exec -T db sh -c 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d postgres -c "DROP DATABASE \"$POSTGRES_DB\" WITH (FORCE)" -c "CREATE DATABASE \"$POSTGRES_DB\""' >/dev/null \
  || fail "could not recreate the database; the writers are stopped, restore by hand"
docker compose exec -T db sh -c 'pg_restore --exit-on-error --no-owner -U "$POSTGRES_USER" -d "$POSTGRES_DB"' < "$F" \
  || fail "pg_restore failed; the writers are stopped, the database is incomplete"
# shellcheck disable=SC2086
[ -z "$WRITERS" ] || docker compose up -d --wait --wait-timeout 300 $WRITERS >/dev/null || fail "services did not come back after the restore"
echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $F RESTORED into the live database" >> backups/backup.log
echo "restored $F"

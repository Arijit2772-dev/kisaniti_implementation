#!/bin/sh
# Nightly database backup with a restore test (CHAT-54). Runs on the server in the deploy folder; release.sh installs
# the cron line. Usage: backup.sh       KN_KEEP (default 14) newest backups are kept.
# 1. pg_dump (custom format) to backups/kisaniti-<UTC>.dump, readable only by the owner.
# 2. Restore test: the dump is restored into a throwaway PostgreSQL with no network. It must restore without errors,
#    have the same tables and migration revision as the live database, and every table's row count must lie between
#    the live counts taken just before and just after the dump (writes may happen during it).
# 3. Only a backup that passed is kept; a failed one is renamed *.dump.FAILED and the script exits non-zero.
# Every run is appended to backups/backup.log as "<UTC time> <file> OK|FAILED <detail>".
set -eu
cd "${KN_DIR:-$HOME/kisaniti}"
KEEP="${KN_KEEP:-14}"
mkdir -p backups; chmod 700 backups
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
F="backups/kisaniti-$STAMP.dump"
LOG=backups/backup.log
SCRATCH="kn-restore-test-$STAMP"
fail() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $F FAILED $*" >> "$LOG"; [ -f "$F" ] && mv "$F" "$F.FAILED"; echo "FAIL: $*" >&2; exit 1; }
cleanup() { docker rm -f "$SCRATCH" >/dev/null 2>&1 || true; }
trap cleanup EXIT
q() { docker compose exec -T db sh -c "psql -v ON_ERROR_STOP=1 -U \"\$POSTGRES_USER\" -d \"\$POSTGRES_DB\" -tAc \"$1\""; }
# one line per table: name|exact row count (a function, so one query counts every table)
COUNTS="select string_agg(t || '|' || (xpath('/row/c/text()', query_to_xml('select count(*) as c from public.' || quote_ident(t), false, true, '')))[1]::text, chr(10) order by t) from unnest(array(select tablename::text from pg_tables where schemaname='public')) t"

q "$COUNTS" > "$F.before" || fail "could not count the live tables"
( umask 077; docker compose exec -T db sh -c 'pg_dump -Fc -U "$POSTGRES_USER" -d "$POSTGRES_DB"' > "$F" ) || fail "pg_dump failed"
q "$COUNTS" > "$F.after" || fail "could not count the live tables"
LIVE_REV="$(q 'select version_num from alembic_version')"
[ -s "$F" ] || fail "the dump is empty"

docker run -d --name "$SCRATCH" --network none -e POSTGRES_PASSWORD=restore-test-only -e POSTGRES_DB=r postgres:17.6 >/dev/null \
  || fail "could not start the restore-test database"
# over TCP: the image's temporary first-start server listens only on its socket, so this waits for the final server
for i in $(seq 1 90); do docker exec "$SCRATCH" pg_isready -h 127.0.0.1 -U postgres -d r >/dev/null 2>&1 && break; sleep 1; done
# roles live outside the dump (the FPO isolation role kisaniti_app); make the live cluster's roles, without login
for role in $(q "select rolname from pg_roles where rolname !~ '^pg_' and rolname <> current_user"); do
  docker exec "$SCRATCH" psql -v ON_ERROR_STOP=1 -U postgres -d r -qc "CREATE ROLE \"$role\" NOLOGIN" >/dev/null || fail "could not make role $role"
done
docker exec -i "$SCRATCH" pg_restore --exit-on-error --no-owner -U postgres -d r < "$F" \
  || fail "the dump does not restore cleanly"
rq() { docker exec "$SCRATCH" psql -v ON_ERROR_STOP=1 -U postgres -d r -tAc "$1"; }
rq "$COUNTS" > "$F.restored" || fail "could not count the restored tables"
[ "$(rq 'select version_num from alembic_version')" = "$LIVE_REV" ] || fail "restored migration revision differs from live ($LIVE_REV)"

DETAIL="$(python3 - "$F.before" "$F.after" "$F.restored" 2>&1 <<'PY'
import sys
def load(p):
    return {l.split("|")[0]: int(l.split("|")[1]) for l in open(p).read().split() if "|" in l}
b, a, r = (load(p) for p in sys.argv[1:4])
if not (set(b) == set(a) == set(r)) or not r:
    sys.exit(f"table sets differ: {sorted(set(b) ^ set(r))}")
bad = [t for t in r if not min(b[t], a[t]) <= r[t] <= max(b[t], a[t])]
if bad:
    sys.exit("row counts out of range: " + ", ".join(f"{t} live {b[t]}..{a[t]} restored {r[t]}" for t in bad))
print(f"{len(r)} tables, {sum(r.values())} rows")
PY
)" || fail "$DETAIL"
rm -f "$F.before" "$F.after" "$F.restored"
echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $F OK $DETAIL, revision $LIVE_REV, $(wc -c < "$F" | tr -d ' ') bytes" >> "$LOG"
# keep the newest $KEEP passed backups
ls -1 backups/kisaniti-*.dump 2>/dev/null | sort -r | tail -n "+$((KEEP + 1))" | while read -r old; do rm -f "$old"; done
echo "backup OK: $F ($DETAIL)"

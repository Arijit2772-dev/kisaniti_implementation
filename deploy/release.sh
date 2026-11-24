#!/bin/sh
# Starts one exact commit's images on the server (CHAT-54). Run by deploy/deploy.sh over SSH; it can also be run by
# hand in the deploy folder. Usage: release.sh <40-character commit>
# Every attempt is appended to deploys.log as "<UTC time> <commit> OK|FAILED|REFUSED <reason>".
# Rollback is a release of an older commit. It is refused when the database already has a migration that the older
# commit does not know: then the only safe way back is restoring the backup taken before that migration.
# First run: with no .env, KN_DOMAIN=<domain> creates one with a new random database password (never printed).
set -eu
cd "${KN_DIR:-$HOME/kisaniti}"
SHA="${1:-}"
LOG=deploys.log
fail() { echo "FAIL: $*" >&2; exit 1; }
record() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $SHA $*" >> "$LOG"; }
echo "$SHA" | grep -Eq '^[0-9a-f]{40}$' || fail "give the full 40-character commit hash, got '$SHA'"

if [ ! -f .env ]; then
  [ -n "${KN_DOMAIN:-}" ] || fail "no .env here; set KN_DOMAIN=<domain> once to create it"
  ( umask 077
    { echo "POSTGRES_USER=kisaniti"
      echo "POSTGRES_PASSWORD=$(od -An -N24 -tx1 /dev/urandom | tr -d ' \n')"
      echo "POSTGRES_DB=kisaniti"
      echo "KISANITI_MODE=DEMO"
      echo "KN_REGISTRY=${KN_REGISTRY:-ghcr.io/arijit2772-dev}"
      echo "KN_DOMAIN=$KN_DOMAIN"
      echo "KN_APP_HOST=${KN_APP_HOST:-app.$KN_DOMAIN}"
      echo "KN_MQTT_HOST=${KN_MQTT_HOST:-mqtt.$KN_DOMAIN}"
      echo "KN_TAG="
    } > .env )
  echo "created .env for $KN_DOMAIN"
fi
val() { sed -n "s/^$1=//p" .env | tail -1; }
APP="$(val KN_APP_HOST)"
export KN_TAG="$SHA"   # the shell value wins over .env; .env is updated only after the release is healthy

if [ "${KN_PULL:-1}" = 1 ]; then
  docker compose pull -q || { record FAILED pull; fail "could not pull the images for $SHA (did CI publish them?)"; }
fi

# Migration guard: the target commit must know the migration the database is at
REV="$(docker compose exec -T db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc "select version_num from alembic_version"' 2>/dev/null || true)"
if [ -n "$REV" ] && ! docker compose run --rm --no-deps -T backend alembic show "$REV" >/dev/null 2>&1; then
  record REFUSED "database at migration $REV, unknown to this commit"
  fail "the database is at migration $REV, which commit $SHA does not know. Restore the backup from before that migration instead."
fi

if ! docker compose up -d --wait --wait-timeout 300 --remove-orphans; then
  docker compose ps -a
  record FAILED "services not healthy"
  fail "the services did not become healthy; roll back with deploy/deploy.sh --previous"
fi

# Through Caddy exactly as a visitor arrives, and the API's own readiness (database reachable, migrations applied)
CA=""; [ -n "${KN_CHECK_CA:-}" ] && CA="--cacert $KN_CHECK_CA"
ok=0
for i in $(seq 1 30); do
  # shellcheck disable=SC2086
  curl -fsS $CA --resolve "$APP:443:127.0.0.1" "https://$APP/en/status" 2>/dev/null | grep -q "System status" && { ok=1; break; }
  sleep 2
done
[ "$ok" = 1 ] || { record FAILED "website not reachable over HTTPS"; fail "https://$APP/en/status did not answer"; }
docker compose exec -T backend python -c "import json,sys,urllib.request as u;sys.exit(json.load(u.urlopen('http://127.0.0.1:8000/health/ready'))['status']!='READY')" \
  || { record FAILED "API not READY"; fail "the API is not READY"; }
# the API that answers must be this exact commit's build (CI writes the commit into the image)
RUNNING="$(docker compose exec -T backend python -c "import json,urllib.request as u;print(json.load(u.urlopen('http://127.0.0.1:8000/api/v1/meta'))['build_commit'])")"
[ "$RUNNING" = "$SHA" ] || { record FAILED "running build $RUNNING"; fail "the API reports build $RUNNING, not $SHA"; }

if grep -q '^KN_TAG=' .env; then sed -i.bak "s/^KN_TAG=.*/KN_TAG=$SHA/" .env && rm -f .env.bak; else echo "KN_TAG=$SHA" >> .env; fi
# nightly backup at 22:00 UTC (03:30 in India), installed once; KN_CRON=0 skips it (local checks)
if [ "${KN_CRON:-1}" = 1 ]; then
  ( crontab -l 2>/dev/null | grep -v '# kisaniti-backup$' || true
    echo "0 22 * * * cd $PWD && sh backup.sh >> backup_cron.out 2>&1 # kisaniti-backup" ) | crontab -
fi
record OK
echo "released $SHA at https://$APP"

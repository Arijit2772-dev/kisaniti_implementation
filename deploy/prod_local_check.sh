#!/bin/sh
# P3-1 check: the production stack exactly as the server runs it, on this machine with fictional data.
# Builds the three images locally, releases them with deploy/release.sh into a temporary folder laid out like the
# server's, with host names under .localhost (Caddy then uses its own local CA instead of Let's Encrypt).
# Needs ports 80, 443 and 8883 free. Prints PASS or FAIL per check; removes the test stack and its data at the end.
set -eu
HERE="$(cd "$(dirname "$0")/.." && pwd)"; cd "$HERE"
T="$(mktemp -d)"
SHA="$(git rev-parse HEAD)"
export KN_DIR="$T" COMPOSE_PROJECT_NAME=kn-prodtest KN_PULL=0 KN_REGISTRY=kn-local KN_CRON=0
export KN_DOMAIN=localhost KN_APP_HOST=app.localhost KN_MQTT_HOST=mqtt.localhost
export KN_INGEST_CA=/run/kn/tls/local-root.crt   # the worker trusts Caddy's local CA here; public roots on the server
fail() { echo "FAIL: $*" >&2; exit 1; }
pass() { echo "PASS: $*"; }
dc() { (cd "$T" && docker compose "$@"); }
cleanup() { dc down -v >/dev/null 2>&1 || true; rm -rf "$T"; }
trap cleanup EXIT
for port in 80 443 8883; do
  if nc -z 127.0.0.1 "$port" 2>/dev/null; then fail "port $port is in use (stop the local stack: docker compose --profile hardware down)"; fi
done

docker build -q --build-arg KN_BUILD_COMMIT="$SHA" -t "kn-local/kisaniti-backend:$SHA" -f backend/Dockerfile . >/dev/null
docker build -q --build-arg KN_BUILD_COMMIT="$SHA" -t "kn-local/kisaniti-frontend:$SHA" frontend >/dev/null
docker build -q --build-arg KN_BUILD_COMMIT="$SHA" -t "kn-local/kisaniti-ingest:$SHA" -f backend/ingestion/Dockerfile . >/dev/null
pass "images built for $SHA"
cp deploy/compose.prod.yaml "$T/compose.yaml"
cp deploy/Caddyfile deploy/release.sh deploy/backup.sh deploy/restore.sh deploy/broker_user.sh deploy/broker_cert.sh "$T/"
mkdir -p "$T/broker" && cp deploy/broker/mosquitto.conf deploy/broker/brokercert.sh backend/ingestion/broker/acl "$T/broker/"

# Caddy makes its local CA on first start; the release's own HTTPS check needs it, so the first run trusts nothing
# and is expected to fail only at that check. Take the CA out, then release again.
sh "$T/release.sh" "$SHA" > "$T/first.out" 2>&1 || true
[ "$(stat -c %a "$T/.env" 2>/dev/null || stat -f %Lp "$T/.env")" = 600 ] || fail ".env is not private (mode 600)"
grep -q '^POSTGRES_PASSWORD=[0-9a-f]\{48\}$' "$T/.env" || fail ".env has no random database password"
pass ".env created with a random database password, readable only by its owner"
dc cp caddy:/data/caddy/pki/authorities/local/root.crt "$T/root.crt" >/dev/null
export KN_CHECK_CA="$T/root.crt"
sh "$T/release.sh" "$SHA" > "$T/release.out" 2>&1 || { cat "$T/first.out" "$T/release.out"; fail "release failed"; }
grep -q "^KN_TAG=$SHA$" "$T/.env" || fail "KN_TAG not recorded in .env"
tail -1 "$T/deploys.log" | grep -q " $SHA OK$" || fail "deploys.log has no OK line"
pass "release.sh started every service healthy, checked HTTPS and API readiness, recorded the commit"

PORTS="$(dc ps --format json | python3 -c '
import json,sys
pub=set()
for line in sys.stdin:
    if line.strip():
        c=json.loads(line)
        pub |= {c["Service"] + ":" + str(p["PublishedPort"]) for p in c.get("Publishers") or [] if p.get("PublishedPort")}
print(" ".join(sorted(pub)))')"
[ "$PORTS" = "broker:8883 caddy:443 caddy:80" ] || fail "published ports should be only caddy 80, 443 and broker 8883, got: $PORTS"
pass "only Caddy (80, 443) and the broker (8883) are reachable from outside; database, API, website and worker are not"

C="curl -fsS --cacert $T/root.crt"
$C "https://app.localhost/en/status" | grep -q "System status" || fail "website over HTTPS"
$C "https://app.localhost/pa/status" | grep -q "ਸਿਸਟਮ ਦੀ ਸਥਿਤੀ" || fail "Punjabi page over HTTPS"
[ "$(curl -sS -o /dev/null -w '%{http_code} %{redirect_url}' http://app.localhost/en/status)" = "308 https://app.localhost/en/status" ] \
  || fail "plain HTTP does not redirect to HTTPS"
[ "$(curl -sS --cacert "$T/root.crt" -o /dev/null -w '%{http_code} %{redirect_url}' https://localhost/x)" = "308 https://app.localhost/x" ] \
  || fail "the bare domain does not redirect to the app"
$C -D - -o /dev/null "https://app.localhost/en/status" | grep -qi '^strict-transport-security: max-age=31536000' || fail "no HSTS header"
$C "https://mqtt.localhost/" | grep -q "MQTT over TLS" || fail "the broker host name has no certificate"
$C "https://app.localhost/en/sign-in" | grep -q "data-build-commit=\"$SHA\"" || fail "the footer does not show build $SHA"
$C "https://app.localhost/api/v1/meta" | grep -q "\"build_commit\":\"$SHA\"" || fail "/api/v1/meta does not report build $SHA"
RID="$($C -D - -o /dev/null "https://app.localhost/api/v1/meta" | sed -n 's/^x-request-id: *\([0-9a-f]*\).*/\1/ip' | tr -d '\r')"
[ -n "$RID" ] && dc logs backend | grep "\"request_id\": \"$RID\"" | grep -q '"path": "/api/v1/meta"' || fail "no JSON log line for request $RID"
pass "footer and /api/v1/meta show build $SHA; a browser request's ID appears in the API's JSON log"
pass "HTTPS for app, mqtt and bare domain; HTTP and the bare domain redirect; HSTS set; Punjabi page served"

U="prod.check.$(date +%s)"
TEMP="$(dc exec -T backend python -m kisaniti_api.access.cli create-fpo --name "Prod check (fictional)" --admin-username "$U" --synthetic \
  | python3 -c "import json,sys;print(json.load(sys.stdin)['temporary_password'])")"
$C -D "$T/h" -c "$T/jar" -o "$T/login.json" -H 'Origin: https://app.localhost' -H 'Content-Type: application/json' \
  -d "{\"username\":\"$U\",\"password\":\"$TEMP\"}" https://app.localhost/api/v1/auth/login || fail "sign-in through Caddy"
grep -i '^set-cookie:' "$T/h" | grep -qi 'secure' || fail "the session cookie is not Secure"
CSRF="$(python3 -c "import json,sys;print(json.load(open(sys.argv[1]))['csrf_token'])" "$T/login.json")"
# the API's own rule (access/v1): a signed-in change from a website other than KISANITI_ALLOWED_ORIGINS is refused
CODE="$(curl -sS --cacert "$T/root.crt" -o /dev/null -w '%{http_code}' -b "$T/jar" -H "X-CSRF-Token: $CSRF" -H 'Origin: https://evil.example' \
  -H 'Content-Type: application/json' -d "{\"current_password\":\"$TEMP\",\"new_password\":\"prod check passphrase long\"}" \
  https://app.localhost/api/v1/auth/change-password)"
[ "$CODE" = 403 ] || fail "a signed-in change from another origin got HTTP $CODE, not 403"
pass "sign-in over HTTPS gives a Secure cookie; a signed-in change from another origin is refused (403)"
NEWPW="prod check passphrase long enough"
$C -o /dev/null -b "$T/jar" -H "X-CSRF-Token: $CSRF" -H 'Origin: https://app.localhost' -H 'Content-Type: application/json' \
  -d "{\"current_password\":\"$TEMP\",\"new_password\":\"$NEWPW\"}" https://app.localhost/api/v1/auth/change-password || fail "password change"
$C -b "$T/jar" https://app.localhost/en/models > "$T/models.html" || fail "forecast models page"
[ "$(grep -o 'data-status="NOT_PROMOTED"' "$T/models.html" | wc -l | tr -d ' ')" = 42 ] || fail "the models page does not list 42 not-promoted models"
grep -q "Naive (last price)" "$T/models.html" || fail "the models page does not name the model in use"
pass "forecast models page over HTTPS: 42 comparisons, none promoted, naive in use"

# P3-2 storage broker: TLS with the certificate Caddy obtained for mqtt.<domain>, server-only users, the worker stores
[ "$(stat -c %a "$T/broker" 2>/dev/null || stat -f %Lp "$T/broker")" = 700 ] || fail "the broker folder is not private"
grep -q '^ingestion:\$7\$' "$T/broker/auth/passwd" || fail "the broker has no ingestion user"
pass "release.sh made the broker's ingestion user (hash only in broker/auth/passwd; broker/ is private)"
echo | openssl s_client -connect 127.0.0.1:8883 -servername mqtt.localhost -CAfile "$T/root.crt" -verify_return_error \
  -verify_hostname mqtt.localhost 2>/dev/null | grep -q "Verify return code: 0" || fail "broker TLS on 8883 is not Caddy's certificate for mqtt.localhost"
pass "the broker serves Caddy's certificate for mqtt.localhost on 8883 (verified chain and host name)"
dc logs ingest | grep -q "subscribing kisaniti/v1/devices/+/telemetry" || fail "kn-ingest is not subscribed"
pass "kn-ingest connected to the broker by its public name over TLS and subscribed"
DEV="kn-prod-$(date +%s | tail -c 6)"
$C -o /dev/null -b "$T/jar" -H "X-CSRF-Token: $CSRF" -H 'Origin: https://app.localhost' -H 'Content-Type: application/json' \
  -d "{\"device_id\":\"$DEV\",\"source_origin\":\"SIMULATOR\"}" https://app.localhost/api/v1/devices || fail "device registration"
DPW="$(od -An -N12 -tx1 /dev/urandom | tr -d ' \n')"
H="$(mktemp -d)"
docker run --rm -u "$(id -u):$(id -g)" -v "$H:/a" eclipse-mosquitto:2.0.20 mosquitto_passwd -b -c /a/p "$DEV" "$DPW" >/dev/null
sh "$T/broker_user.sh" < "$H/p" > "$T/user.out" 2>&1 || { cat "$T/user.out"; fail "broker_user.sh"; }
rm -rf "$H"
if echo "evil:plain" | sh "$T/broker_user.sh" > /dev/null 2>&1; then fail "broker_user.sh accepted a line that is not a password hash"; fi
if echo 'ingestion:$7$101$AAAA$BBBB' | sh "$T/broker_user.sh" > /dev/null 2>&1; then fail "broker_user.sh replaced the ingestion user"; fi
pass "broker_user.sh adds a device from a password hash on stdin; plain text and the service user are refused"
SIM() { dc run --rm --no-deps -T --entrypoint python ingest -m kisaniti_ingestion.simulator --scenario stable_dry --seed 3 \
  --device-id "$DEV" --host mqtt.localhost --port 8883 --ca /run/kn/tls/local-root.crt --user "$DEV" --ack-timeout 10 "$@"; }
if SIM --steps 1 --password wrong-password > "$T/simbad.out" 2>&1; then fail "the broker accepted a wrong device password"; fi
SIM --steps 4 --password "$DPW" > "$T/sim.out" 2>&1 || { cat "$T/sim.out"; dc logs --tail 20 broker ingest; fail "software node over the public broker"; }
N="$(dc exec -T db sh -c "psql -U \"\$POSTGRES_USER\" -d \"\$POSTGRES_DB\" -tAc \"select count(*) from sensor_readings where device_id='$DEV'\"")"
[ "$N" = 4 ] || { cat "$T/sim.out"; fail "expected 4 stored readings for $DEV, found $N"; }
pass "a software node with its own broker user published 4 readings over TLS; all 4 stored and acknowledged; a wrong password is refused"
R1="$(sh "$T/broker_cert.sh")"; echo "$R1" | grep -q "unchanged" || fail "broker_cert.sh after a release: $R1"
dc exec -T brokercert sh -c 'echo 99 > /tls/serial'
R2="$(sh "$T/broker_cert.sh")"; echo "$R2" | grep -q "broker restarted" || fail "broker_cert.sh after a renewal: $R2"
ok=0; for i in $(seq 1 30); do [ "$(dc logs ingest | grep -c 'subscribing kisaniti')" -ge 3 ] && { ok=1; break; }; sleep 2; done
[ "$ok" = 1 ] || fail "kn-ingest did not resubscribe after the broker restart"
pass "broker_cert.sh leaves the broker alone while the certificate is unchanged, restarts it after a renewal; kn-ingest reconnects"

(export COMPOSE_FILE="$T/compose.yaml" CURL_CA_BUNDLE="$T/root.crt"; SMOKE_SEED_MARKET=1 scripts/smoke_access.sh https://app.localhost) \
  > "$T/smoke.out" 2>&1 || { cat "$T/smoke.out"; fail "smoke_access.sh through Caddy"; }
pass "smoke_access.sh (login, two-FPO isolation, lots, stock ledger, prices) passes through Caddy over HTTPS"

# P3-3 backups: dump, restore test in a throwaway database, retention, and one real restore of the live database
export KN_KEEP=2
VOLS="$(docker volume ls -q | wc -l | tr -d ' ')"
for n in 1 2 3; do sh "$T/backup.sh" > "$T/backup$n.out" 2>&1 || { cat "$T/backup$n.out"; fail "backup run $n"; }; sleep 1; done
[ "$(grep -c ' OK ' "$T/backups/backup.log")" = 3 ] || fail "backup.log should have 3 OK runs"
[ "$(ls "$T"/backups/kisaniti-*.dump | wc -l | tr -d ' ')" = 2 ] || fail "retention should keep exactly 2 backups"
[ "$(stat -c %a "$T/backups" 2>/dev/null || stat -f %Lp "$T/backups")" = 700 ] || fail "the backups folder is not private"
B="$(cd "$T" && ls backups/kisaniti-*.dump | sort | tail -1)"
[ "$(stat -c %a "$T/$B" 2>/dev/null || stat -f %Lp "$T/$B")" = 600 ] || fail "the backup file is not private"
[ "$(docker volume ls -q | wc -l | tr -d ' ')" = "$VOLS" ] || fail "backups left Docker volumes behind (the restore-test database must be removed with its data)"
pass "3 backups each passed a restore test (same tables, revision, row counts); 2 newest kept; files private; no volumes left behind"
U2="after.backup.$(date +%s)"
dc exec -T backend python -m kisaniti_api.access.cli create-fpo --name "After backup (fictional)" --admin-username "$U2" --synthetic >/dev/null
DBC() { dc exec -T db sh -c "psql -U \"\$POSTGRES_USER\" -d \"\$POSTGRES_DB\" -tAc \"$1\""; }
[ "$(DBC "select count(*) from users where username='$U2'")" = 1 ] || fail "the FPO made after the backup is missing"
if (cd "$T" && KN_YES=1 sh restore.sh backups/not-a-backup.dump) > /dev/null 2>&1; then fail "restore accepted a file that is not a passed backup"; fi
(cd "$T" && KN_YES=1 sh restore.sh "$B") > "$T/restore.out" 2>&1 || { cat "$T/restore.out"; fail "restore"; }
[ "$(DBC "select count(*) from users where username='$U2'")" = 0 ] || fail "after the restore, the later FPO is still there"
[ "$(DBC "select count(*) from users where username='$U'")" = 1 ] || fail "after the restore, the earlier user is missing"
$C -o /dev/null -H 'Content-Type: application/json' -d "{\"username\":\"$U\",\"password\":\"$NEWPW\"}" https://app.localhost/api/v1/auth/login \
  || fail "sign-in after the restore"
pass "restore.sh brought the live database back to the backup: later FPO gone, earlier user signs in over HTTPS"

# Rollback guard: a database at a migration the target commit does not know must be refused, and nothing changed
DBQ() { dc exec -T db sh -c "psql -U \"\$POSTGRES_USER\" -d \"\$POSTGRES_DB\" -tAc \"$1\""; }
REV="$(DBQ 'select version_num from alembic_version')"
DBQ "update alembic_version set version_num='ffff0000aaaa'" >/dev/null
if sh "$T/release.sh" "$SHA" > "$T/refused.out" 2>&1; then fail "release ran on a database from a newer schema"; fi
grep -q "Restore the backup" "$T/refused.out" || { cat "$T/refused.out"; fail "refusal message"; }
tail -1 "$T/deploys.log" | grep -q " REFUSED " || fail "refusal not recorded"
DBQ "update alembic_version set version_num='$REV'" >/dev/null
sh "$T/release.sh" "$SHA" > "$T/again.out" 2>&1 || { cat "$T/again.out"; fail "release after restoring the revision"; }
pass "a release onto an unknown newer migration is REFUSED and recorded; with the migration known it releases again"

(KN_DOMAIN='' sh "$T/release.sh" "$SHA") > "$T/nodomain.out" 2>&1 || { cat "$T/nodomain.out"; fail "a later release with KN_DOMAIN empty (as deploy.sh sends it) failed (BUG-011)"; }
pass "a later release with KN_DOMAIN empty uses the domain from .env (BUG-011)"
if sh "$T/release.sh" not-a-commit > /dev/null 2>&1; then fail "a short or invalid commit was accepted"; fi
pass "an invalid commit hash is refused"
cat "$T/deploys.log"
echo "ALL PASS"

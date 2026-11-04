#!/bin/sh
# HW-S1 check, on fictional data: the software node reaches the website over MQTT and TLS through the compose
# "hardware" profile, and the normal stack is not changed. Prints PASS or FAIL per check; any FAIL stops it.
# Usage: scripts/hw_stack_check.sh     Run from anywhere; starts the stack if needed and leaves it running.
# It adds a fictional FPO pair and one simulated device to the database, and removes its broker user at the end.
set -eu
HERE="$(cd "$(dirname "$0")/.." && pwd)"; cd "$HERE"
SITE="${SITE:-http://localhost:3000}"
BROKER="$HERE/backend/ingestion/broker"
J="$(mktemp -d)"
SUF="$(date +%s)$$"; DEV="kn-stack-$SUF"; PW="stack check passphrase $SUF"; DEV_PW="$(openssl rand -hex 16)"
IMG=eclipse-mosquitto:2.0.20
compose() { docker compose --profile hardware "$@"; }
passwd_tool() { docker run --rm -u "$(id -u):$(id -g)" -v "$BROKER":/mosquitto/config "$IMG" mosquitto_passwd "$@" > "$J/passwd.out" 2>&1 || { cat "$J/passwd.out" >&2; return 1; }; }
cleanup() {
  passwd_tool -D /mosquitto/config/passwd "$DEV" >/dev/null 2>&1 || true
  chmod 644 "$BROKER/passwd" 2>/dev/null || true
  compose kill -s HUP broker >/dev/null 2>&1 || true
  rm -rf "$J"
}
trap cleanup EXIT
fail() { echo "FAIL: $*" >&2; exit 1; }
pass() { echo "PASS: $*"; }
field() { python3 -c "import json,sys;print(json.load(sys.stdin)$1)"; }

[ "$(docker compose config --services | sort | tr '\n' ' ')" = "assessor backend db frontend " ] \
  || fail "the normal stack's services changed: $(docker compose config --services | tr '\n' ' ')"
[ "$(compose config --services | sort | tr '\n' ' ')" = "assessor backend broker db frontend ingest " ] \
  || fail "the hardware profile's services: $(compose config --services | tr '\n' ' ')"
pass "normal stack has its 4 services; the hardware profile adds only broker and ingest"

"$BROKER/setup_stack.sh" localhost > "$J/setup.out" 2>&1 || { cat "$J/setup.out"; fail "broker setup failed"; }
passwd_tool -b /mosquitto/config/passwd "$DEV" "$DEV_PW"
chmod 644 "$BROKER/passwd"
compose up --build -d --wait --wait-timeout 300 >/dev/null 2>&1 || { compose ps -a; compose logs broker ingest | tail -40; fail "the stack did not start"; }
compose kill -s HUP broker >/dev/null 2>&1   # a broker already running must read the new user
sleep 1
for i in $(seq 1 20); do compose logs ingest | grep -q 'subscribing kisaniti/v1/devices' && break; sleep 1; done
compose logs ingest | grep -q 'subscribing kisaniti/v1/devices' || { compose logs ingest | tail -20; fail "kn-ingest did not connect and subscribe"; }
pass "broker is up and kn-ingest connected over TLS as its own broker user"

python3 - "$BROKER/certs/ca.crt" <<'PY' || fail "port 8883 on the host does not give TLS with this CA"
import socket, ssl, sys
ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)  # as the worker's client does: the certificate and host name must verify
ctx.load_verify_locations(cafile=sys.argv[1])
with socket.create_connection(("localhost", 8883), timeout=5) as raw, ctx.wrap_socket(raw, server_hostname="localhost") as s:
    print("PASS: host port 8883 gives", s.version(), "and the certificate verifies against the CA")
PY

mkfpo() {  # label: creates a fictional FPO, signs in and changes the temporary password; leaves a cookie jar and CSRF token
  set -- "$1" "$(docker compose exec -T backend python -m kisaniti_api.access.cli create-fpo --name "Stack check $1 (fictional) $SUF" --admin-username "stack.$1.$SUF" --synthetic)"
  TEMP="$(printf '%s' "$2" | field "['temporary_password']")"
  curl -fsS -c "$J/$1.jar" -H 'Content-Type: application/json' -d "{\"username\":\"stack.$1.$SUF\",\"password\":\"$TEMP\"}" "$SITE/api/v1/auth/login" | field "['csrf_token']" > "$J/$1.csrf"
  curl -fsS -o /dev/null -b "$J/$1.jar" -H "X-CSRF-Token: $(cat "$J/$1.csrf")" -H 'Content-Type: application/json' \
    -d "{\"current_password\":\"$TEMP\",\"new_password\":\"$PW\"}" "$SITE/api/v1/auth/change-password"
}
mkfpo a; mkfpo b
api() { l="$1"; shift; curl -fsS -b "$J/$l.jar" -H "X-CSRF-Token: $(cat "$J/$l.csrf")" -H 'Content-Type: application/json' "$@"; }
START="$(python3 -c 'from datetime import datetime,timedelta,timezone;print((datetime.now(timezone.utc)-timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S.000Z"))')"
api a -o /dev/null -d "{\"device_id\":\"$DEV\",\"source_origin\":\"SIMULATOR\"}" "$SITE/api/v1/devices"
api a -o /dev/null -d "{\"placement\":\"IN_GRAIN\",\"crop\":\"PADDY\",\"assigned_at\":\"$START\"}" "$SITE/api/v1/devices/$DEV/assignments"
pass "device $DEV registered and assigned through the website"

SIM="python -m kisaniti_ingestion.simulator --scenario humid_rise --device-id $DEV --host broker --port 8883 --ca /run/kn/ca.crt --user $DEV"
if compose exec -T ingest $SIM --steps 1 --ack-timeout 2 --password wrong-password > "$J/bad.out" 2>&1; then
  cat "$J/bad.out"; fail "the broker accepted a wrong password"
fi
pass "the broker refuses a wrong password"

compose exec -T ingest $SIM --steps 12 --seed 1 --ack-timeout 10 --password "$DEV_PW" > "$J/sim.out" || { cat "$J/sim.out"; compose logs ingest | tail -20; fail "the software node's run failed"; }
ACKS="$(tail -1 "$J/sim.out" | field "['acks']")"
[ "$ACKS" = "{'ACCEPTED': 12}" ] || fail "expected 12 accepted acks, got $ACKS"
pass "software node: 12 readings over MQTT and TLS, 12 acknowledged after storing"

api a "$SITE/api/v1/devices/$DEV/readings?limit=1000" > "$J/readings.json"
python3 - "$J/readings.json" <<'PY' || fail "the website does not show the readings as expected"
import json, sys
rows = json.load(open(sys.argv[1]))
origins = {r["origin"] for r in rows}
if len(rows) != 12 or origins != {"SYNTHETIC"}:
    sys.exit(f"got {len(rows)} readings with origins {origins}")
print("PASS: the website returns all 12 readings, labelled SYNTHETIC")
PY
CODE="$(curl -sS -o /dev/null -w '%{http_code}' -b "$J/b.jar" "$SITE/api/v1/devices/$DEV/readings")"
[ "$CODE" = 404 ] || fail "another FPO asked for this device's readings and got HTTP $CODE, not 404"
pass "another FPO gets 404 for this device's readings"

compose logs ingest broker > "$J/logs.txt" 2>&1
DB_PW="$(compose exec -T ingest sh -c 'echo "${KN_STORE#*://*:}" | cut -d@ -f1')"
for secret in "$(cat "$BROKER/ingestion.password")" "$DEV_PW" "$DB_PW"; do
  [ -n "$secret" ] || fail "a secret to look for was empty"
  if grep -qF "$secret" "$J/logs.txt"; then fail "a password appears in the broker or worker logs"; fi
done
pass "no password appears in the broker or worker logs"
echo "ALL PASS"

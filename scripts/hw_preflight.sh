#!/bin/sh
# HW-S4 preflight: before powering a storage node, check everything it depends on, one PASS or FAIL per line.
# Unlike the stack check it changes nothing. Every check runs even after a FAIL; the exit code is 1 if any failed.
# Usage: scripts/hw_preflight.sh --device-id kn-node-001 [--host 192.168.43.10] [--secrets firmware/include/secrets.h]
#                                [--max-age-min 10] [--software-node]
#   --host           the address the node uses (default: KN_MQTT_HOST from the secrets file)
#   --max-age-min    how old the newest stored reading may be (default 10 minutes)
#   --software-node  the node runs on this machine (simulator): localhost is allowed and no secrets file is needed
set -u
HERE="$(cd "$(dirname "$0")/.." && pwd)"; cd "$HERE"
BROKER="$HERE/backend/ingestion/broker"
DEV=""; HOST=""; SECRETS="$HERE/firmware/include/secrets.h"; MAXAGE=10; SOFT=0
while [ $# -gt 0 ]; do
  case "$1" in
    --device-id) DEV="$2"; shift 2 ;;
    --host) HOST="$2"; shift 2 ;;
    --secrets) SECRETS="$2"; shift 2 ;;
    --max-age-min) MAXAGE="$2"; shift 2 ;;
    --software-node) SOFT=1; shift ;;
    *) echo "unknown argument $1" >&2; exit 2 ;;
  esac
done
[ -n "$DEV" ] || { echo "give --device-id" >&2; exit 2; }
echo "$MAXAGE" | grep -Eq '^[0-9]+$' || { echo "--max-age-min must be whole minutes" >&2; exit 2; }
FAILS=0
pass() { echo "PASS: $*"; }
fail() { echo "FAIL: $*"; FAILS=$((FAILS + 1)); }
compose() { docker compose --profile hardware "$@"; }
secret_define() { sed -n "s/^#define $1 \"\(.*\)\"$/\1/p" "$SECRETS" 2>/dev/null | head -1; }

# 1. stack: the four services the node's readings pass through
for svc in db backend broker ingest; do
  state="$(compose ps --format '{{.State}} {{.Health}}' "$svc" 2>/dev/null | head -1)"
  case "$state" in
    "running healthy"|"running ") pass "service $svc is running${state#running}" ;;
    *) fail "service $svc is not running and healthy (state: '${state:-absent}'); start: docker compose --profile hardware up -d --wait" ;;
  esac
done

# 2. secrets file the node was flashed with: host, device and CA must match this machine's broker
if [ -f "$SECRETS" ]; then
  S_HOST="$(secret_define KN_MQTT_HOST)"; S_DEV="$(secret_define KN_DEVICE_ID)"
  [ -n "$HOST" ] || HOST="$S_HOST"
  [ "$S_DEV" = "$DEV" ] && pass "secrets file is for $DEV" || fail "secrets file is for device '$S_DEV', not $DEV; provision this device"
  [ "$S_HOST" = "$HOST" ] && pass "secrets file uses host $HOST" || fail "secrets file uses host '$S_HOST', checking $HOST; provision again"
  python3 - "$SECRETS" "$BROKER/certs/ca.crt" <<'PY' && pass "secrets file holds this broker's CA" || fail "the CA in the secrets file is not this broker's ca.crt; provision again and reflash"
import re, sys
flashed = "".join(re.findall(r'"([^"]*)\\n"', open(sys.argv[1]).read()))
mine = "".join(l.strip() for l in open(sys.argv[2]) if l.strip())
sys.exit(0 if flashed.replace("\\n", "") == mine else 1)
PY
elif [ "$SOFT" = 1 ]; then
  pass "software node: no secrets file needed"
else
  fail "no secrets file at $SECRETS; run scripts/provision_node.py"
fi
[ -n "$HOST" ] || { HOST=localhost; fail "no --host and no secrets file; checking localhost only"; }

# 3. the address must belong to this machine now (a hotspot can hand out a new IP), and not be loopback for a real node
python3 - "$HOST" "$SOFT" <<'PY'
import socket, subprocess, sys
host, soft = sys.argv[1], sys.argv[2] == "1"
try:
    wanted = {ai[4][0] for ai in socket.getaddrinfo(host, None, socket.AF_INET)}
except OSError:
    print(f"FAIL: host {host} does not resolve"); sys.exit(1)
out = subprocess.run(["ifconfig"], capture_output=True, text=True).stdout if sys.platform == "darwin" else \
      subprocess.run(["ip", "-4", "-o", "addr"], capture_output=True, text=True).stdout
mine = {w.split("/")[0] for line in out.splitlines() for i, w in enumerate(line.split()) if i and line.split()[i - 1] == "inet"}
loop = all(a.startswith("127.") for a in wanted)
if loop and not soft:
    print(f"FAIL: {host} is this machine's loopback; a real node cannot reach it. Use the LAN address: {sorted(mine - {'127.0.0.1'})}")
    sys.exit(1)
if not wanted & mine:
    print(f"FAIL: {host} ({sorted(wanted)}) is not an address of this machine now ({sorted(mine)}); "
          "rerun backend/ingestion/broker/setup_stack.sh with the new address, then provision and reflash")
    sys.exit(1)
print(f"PASS: {host} is an address of this machine ({sorted(wanted & mine)[0]})")
PY
[ $? = 0 ] || FAILS=$((FAILS + 1))

# 4. TLS on port 8883 exactly as the node checks it: this CA, this host name, not expired soon
python3 - "$BROKER/certs/ca.crt" "$HOST" <<'PY'
import socket, ssl, sys, time
ca, host = sys.argv[1], sys.argv[2]
ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT); ctx.load_verify_locations(cafile=ca)
try:
    with socket.create_connection(("127.0.0.1", 8883), timeout=5) as raw, ctx.wrap_socket(raw, server_hostname=host) as s:
        days = (ssl.cert_time_to_seconds(s.getpeercert()["notAfter"]) - time.time()) / 86400
except (OSError, ssl.SSLError) as e:
    print(f"FAIL: TLS on 8883 for host {host}: {e}"); sys.exit(1)
if days < 14:
    print(f"FAIL: the broker certificate expires in {days:.0f} days; rerun setup_stack.sh"); sys.exit(1)
print(f"PASS: broker gives TLS on 8883, certificate valid for {host}, {days:.0f} days left")
PY
[ $? = 0 ] || FAILS=$((FAILS + 1))

# 5. broker user for the device
grep -q "^$DEV:" "$BROKER/passwd" 2>/dev/null && pass "broker user $DEV exists" || fail "no broker user $DEV; run scripts/provision_node.py"

# 6. worker connected and not restarting
if compose logs ingest 2>/dev/null | grep -q 'subscribing kisaniti/v1/devices'; then pass "kn-ingest connected and subscribed"; else fail "kn-ingest has not subscribed; see docker compose --profile hardware logs ingest"; fi
CID="$(compose ps -q ingest 2>/dev/null)"
RC="$( [ -n "$CID" ] && docker inspect -f '{{.RestartCount}}' "$CID" 2>/dev/null || echo '?')"
[ "$RC" = 0 ] && pass "kn-ingest has not restarted" || fail "kn-ingest restart count is $RC; see its logs"

# 7 and 8. registration on the website and the newest stored reading (read as the database owner, across FPOs)
q() { compose exec -T db sh -c "psql -U \"\$POSTGRES_USER\" -d \"\$POSTGRES_DB\" -tA -F'|' -c \"$1\"" 2>/dev/null; }
REG="$(q "select d.active, count(a.id) from devices d left join device_assignments a using (device_id, fpo_id) where d.device_id='$DEV' group by d.active")"
case "$REG" in
  "t|0") fail "device $DEV is registered but not assigned to a placement; assign it on the storage page" ;;
  t\|*) pass "device $DEV is registered, active and assigned" ;;
  "f|"*) fail "device $DEV is registered but inactive" ;;
  *) fail "device $DEV is not registered on the website; register it on the storage page" ;;
esac
NEW="$(q "select round(extract(epoch from now() - max(received_at)) / 60)::int, (array_agg(ingest_status order by received_at desc))[1], count(*) from sensor_readings where device_id='$DEV'")"
AGE="${NEW%%|*}"; REST="${NEW#*|}"; ST="${REST%%|*}"; N="${REST#*|}"
if [ -z "$AGE" ] || [ "${N:-0}" = 0 ]; then fail "no reading stored yet for $DEV (power the node and wait one interval)"
elif [ "$AGE" -le "$MAXAGE" ]; then pass "newest reading for $DEV is $AGE min old ($ST), $N stored"
else fail "newest reading for $DEV is $AGE min old, more than $MAXAGE; is the node powered and on Wi-Fi?"; fi

if [ "$FAILS" = 0 ]; then echo "PREFLIGHT PASS"; else echo "PREFLIGHT FAIL: $FAILS check(s)"; exit 1; fi

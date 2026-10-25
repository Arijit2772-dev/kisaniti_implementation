#!/bin/sh
# Live check (H-1, H-2, H-3 together): real Mosquitto (TLS + ACL, Docker) <-> software node and conformance harness
# <-> kn-ingest writing to a migrated PostgreSQL. All sensor data is SYNTHETIC. Nothing is left running.
#
# Needs: Docker; a PostgreSQL that has had `alembic upgrade head` (backend/); psycopg importable by the ingestion
# Python. Defaults suit the local test container:
#   KN_STORE=postgresql://kisaniti:test-only@localhost:5434/kisaniti_test
#   PY=backend/ingestion/.venv/bin/python    EXTRA_PYTHONPATH=<folder holding psycopg>
set -e
HERE="$(cd "$(dirname "$0")/.." && pwd)"
PY="${PY:-$HERE/.venv/bin/python}"
KN_STORE="${KN_STORE:-postgresql://kisaniti:test-only@localhost:5434/kisaniti_test}"
export PYTHONPATH="$HERE/src:$HERE/../../core/src${EXTRA_PYTHONPATH:+:$EXTRA_PYTHONPATH}"
WORK="$(mktemp -d)"
pw() { openssl rand -hex 12; }
PW_ING="$(pw)"; PW_CONF="$(pw)"; PW_SIM1="$(pw)"; PW_SIM2="$(pw)"; PW_C1="$(pw)"
cp "$HERE/broker/mosquitto.conf" "$HERE/broker/acl" "$WORK/"
# the conformance harness plays the backend for the publisher test, so it gets the ingestion user's rights
printf '\nuser conformance\ntopic read kisaniti/v1/devices/+/telemetry\ntopic write kisaniti/v1/devices/+/ack\n' >> "$WORK/acl"
cp "$HERE/broker/gen_certs.sh" "$WORK/" && "$WORK/gen_certs.sh" localhost >/dev/null 2>&1
touch "$WORK/passwd"
IMG=eclipse-mosquitto:2.0.20
for u in "ingestion:$PW_ING" "conformance:$PW_CONF" "kn-sim-001:$PW_SIM1" "kn-sim-002:$PW_SIM2" "kn-conf-001:$PW_C1"; do
  docker run --rm -v "$WORK":/mosquitto/config $IMG mosquitto_passwd -b /mosquitto/config/passwd "${u%%:*}" "${u#*:}" 2>/dev/null
done
chmod 644 "$WORK/passwd" "$WORK/acl"
CID=$(docker run -d --rm -p 18883:8883 -v "$WORK":/mosquitto/config $IMG)
WPID=""
trap 'docker stop $CID >/dev/null 2>&1; [ -n "$WPID" ] && kill $WPID 2>/dev/null; rm -rf "$WORK"' EXIT
sleep 2
CA="$WORK/certs/ca.crt"
FAILED=0

echo "== A. publisher conformance: the harness is the backend, the software node is the device under test"
"$PY" -m kisaniti_ingestion.conformance publisher --host localhost --port 18883 --ca "$CA" --user conformance \
  --password "$PW_CONF" --device-id kn-sim-002 --origin SIMULATOR --seconds 25 --withhold 2 > "$WORK/pub.out" 2>&1 &
CPID=$!
sleep 3
"$PY" -m kisaniti_ingestion.simulator --scenario sensor_fault --steps 12 --seed 5 --device-id kn-sim-002 \
  --host localhost --port 18883 --ca "$CA" --user kn-sim-002 --password "$PW_SIM2" --ack-timeout 2 || FAILED=1
wait $CPID || FAILED=1
cat "$WORK/pub.out"

echo "== B. start kn-ingest on PostgreSQL"
printf '[{"device_id":"kn-sim-001","source_origin":"SIMULATOR"},{"device_id":"kn-conf-001","source_origin":"SIMULATOR"}]' > "$WORK/devices.json"
KN_MQTT_HOST=localhost KN_MQTT_PORT=18883 KN_MQTT_TLS_CA="$CA" KN_MQTT_USER=ingestion KN_MQTT_PASSWORD="$PW_ING" \
  KN_DEVICES="$WORK/devices.json" KN_STORE="$KN_STORE" "$PY" -m kisaniti_ingestion.worker > "$WORK/ingest.log" 2>&1 &
WPID=$!
sleep 3
BEFORE="$("$PY" -c "from kisaniti_ingestion.store import open_store; print(open_store('$KN_STORE').counts())")"
echo "store before: $BEFORE"

echo "== C. receiver conformance over MQTT: the harness is the device, kn-ingest + PostgreSQL the backend"
"$PY" -m kisaniti_ingestion.conformance receiver --host localhost --port 18883 --ca "$CA" --user kn-conf-001 \
  --password "$PW_C1" --device-id kn-conf-001 --origin SIMULATOR || FAILED=1

echo "== D. software node, scenario humid_rise (48 samples), acknowledged by kn-ingest"
"$PY" -m kisaniti_ingestion.simulator --scenario humid_rise --steps 48 --seed 1 --device-id kn-sim-001 \
  --host localhost --port 18883 --ca "$CA" --user kn-sim-001 --password "$PW_SIM1" --ack-timeout 5 || FAILED=1

echo "== E. what PostgreSQL holds"
"$PY" - "$KN_STORE" <<'PY' || FAILED=1
import sys
from kisaniti_ingestion.store import open_store
s = open_store(sys.argv[1])
rows = s.readings_for("kn-sim-001")
seqs = sorted(r["seq"] for r in rows)
print("counts:", s.counts())
print("kn-sim-001 readings:", len(rows), "seq 0..47 complete:", seqs == list(range(48)))
print("origins:", {(r["provenance"]["origin"], r["provenance"]["source_kind"]) for r in rows})
ok = len(rows) == 48 and seqs == list(range(48)) and all(r["provenance"]["origin"] == "SYNTHETIC" for r in rows)
sys.exit(0 if ok else 1)
PY
echo "== worker log (tail)"; tail -5 "$WORK/ingest.log"
[ "$FAILED" = 0 ] && echo "RESULT: PASS" || { echo "RESULT: FAIL"; exit 1; }

#!/bin/sh
# Demonstration of the storage path on the running compose stack, with fictional data only:
#   register a simulated device through the website -> assign it to grain 30 hours ago -> feed 30 hours of simulator
#   readings through the real ingestion code -> run the assessment job -> read the result through the website.
# Usage: scripts/demo_storage.sh [username] [password]     (defaults: the fictional demo.patiala account)
set -eu
USER_NAME="${1:-demo.patiala}"; PASS="${2:-patiala demo passphrase 2026}"
SITE="${SITE:-http://localhost:3000}"
HERE="$(cd "$(dirname "$0")/.." && pwd)"
J="$(mktemp)"; trap 'rm -f "$J"' EXIT
DEV="kn-demo-$(date +%s)"
CSRF="$(curl -fsS -c "$J" -H 'Content-Type: application/json' -d "{\"username\":\"$USER_NAME\",\"password\":\"$PASS\"}" "$SITE/api/v1/auth/login" | python3 -c 'import json,sys;print(json.load(sys.stdin)["csrf_token"])')"
api() { curl -fsS -b "$J" -H "X-CSRF-Token: $CSRF" -H 'Content-Type: application/json' "$@"; }
START="$(python3 -c 'from datetime import datetime,timedelta,timezone;print((datetime.now(timezone.utc)-timedelta(hours=30)).strftime("%Y-%m-%dT%H:%M:%S.000Z"))')"
echo "1) register $DEV (SIMULATOR) and assign it IN_GRAIN / PADDY since $START"
api -d "{\"device_id\":\"$DEV\",\"source_origin\":\"SIMULATOR\"}" "$SITE/api/v1/devices" >/dev/null
api -d "{\"placement\":\"IN_GRAIN\",\"crop\":\"PADDY\",\"assigned_at\":\"$START\"}" "$SITE/api/v1/devices/$DEV/assignments" >/dev/null
echo "2) feed 30 hours of humid_rise readings through the real ingestion code (registry from the database)"
cd "$HERE/backend" && DEV="$DEV" PYTHONPATH="src:ingestion/src:../core/src" ./.venv/bin/python - <<'PY'
import os
from datetime import UTC, datetime, timedelta
from kisaniti_ingestion.registry import DbRegistry, postgres_fetcher
from kisaniti_ingestion.service import handle
from kisaniti_ingestion.simulator import SCENARIOS, InProcessLink, SimDevice, run_simulation
from kisaniti_ingestion.store import open_store
dsn = os.environ.get("KN_DSN", "postgresql://kisaniti:kisaniti-local-only@localhost:5433/kisaniti")
store, reg = open_store(dsn), DbRegistry(postgres_fetcher(dsn), ttl_s=0, miss_s=0)
now = datetime.now(UTC).replace(microsecond=0)
dev = SimDevice(os.environ["DEV"], InProcessLink(lambda t, p, at: handle(t, p, at, reg, store).ack, lambda: now))
run_simulation(dev, SCENARIOS["humid_rise"], 7, "d3a0b007", now - timedelta(hours=30), 360)
print(f"   sent 360 readings, unacknowledged: {len(dev.buffer)}")
raise SystemExit(0 if not dev.buffer else "FAIL: readings were not acknowledged (is the device registered and the boot id 8 hex characters?)")
PY
cd "$HERE"
echo "3) run the assessment job once (the compose service 'assessor' does this every hour)"
docker compose exec -T backend python -m kisaniti_api.devices.assess_job --once
echo "4) latest storage assessment, read through the website"
curl -fsS -b "$J" "$SITE/api/v1/devices/$DEV/assessment" | python3 -c '
import json,sys; d=json.load(sys.stdin)
print("   category      :", d["category"], d["unknown_reasons"] or "")
print("   RH median     :", d["rh_median_pct"], "% (aw", d["aw_median"], ") temperature", d["temperature_median_c"], "C")
print("   samples       :", d["samples_used"], "of", d["samples_expected"], "expected in 24 h")
print("   hysteresis    :", d["hysteresis"])
print("   origin        :", d["provenance"]["origin"], "|", d["label"])'
echo "   history: $(curl -fsS -b "$J" "$SITE/api/v1/devices/$DEV/assessments?limit=1000" | python3 -c 'import json,sys;print(len(json.load(sys.stdin)))') hourly assessments stored"

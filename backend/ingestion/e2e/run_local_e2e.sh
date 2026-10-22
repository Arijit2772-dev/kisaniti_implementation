#!/bin/sh
# Live check of the backend path: Mosquitto (TLS, ACL) in Docker -> kn-ingest -> SQLite -> ack -> device.
# Needs Docker. Uses throwaway certs/passwords in a temp folder; nothing is left running.
set -e
UV="$(command -v uv || echo "$HOME/Library/Python/3.13/bin/uv")"
HERE="$(cd "$(dirname "$0")/.." && pwd)"
# explicit import paths: uv's editable .pth files can be skipped on macOS when flagged hidden
export PYTHONPATH="$HERE/src:$HERE/../../core/src"
WORK="$(mktemp -d)"
PW_DEV="$(openssl rand -hex 12)"; PW_ING="$(openssl rand -hex 12)"; PW_OTHER="$(openssl rand -hex 12)"
cp "$HERE/broker/mosquitto.conf" "$HERE/broker/acl" "$WORK/"
cp "$HERE/broker/gen_certs.sh" "$WORK/" && "$WORK/gen_certs.sh" localhost >/dev/null 2>&1
touch "$WORK/passwd"
IMG=eclipse-mosquitto:2.0.20
for u in "kn-node-001:$PW_DEV" "ingestion:$PW_ING" "kn-node-002:$PW_OTHER"; do
  docker run --rm -v "$WORK":/mosquitto/config $IMG mosquitto_passwd -b /mosquitto/config/passwd "${u%%:*}" "${u#*:}" 2>/dev/null
done
chmod 644 "$WORK/passwd" "$WORK/acl"
CID=$(docker run -d --rm -p 18883:8883 -v "$WORK":/mosquitto/config $IMG)
trap 'docker stop $CID >/dev/null 2>&1; kill $WPID 2>/dev/null; rm -rf "$WORK"' EXIT
sleep 2
printf '[{"device_id":"kn-node-001","source_origin":"DEVICE"},{"device_id":"kn-node-002","source_origin":"DEVICE"}]' > "$WORK/devices.json"
cd "$HERE"
KN_MQTT_HOST=localhost KN_MQTT_PORT=18883 KN_MQTT_TLS_CA="$WORK/certs/ca.crt" KN_MQTT_USER=ingestion \
  KN_MQTT_PASSWORD="$PW_ING" KN_DEVICES="$WORK/devices.json" KN_STORE="$WORK/readings.sqlite3" \
  "$UV" run --quiet kn-ingest > "$WORK/ingest.log" 2>&1 &
WPID=$!
sleep 3
NOW="$(date -u +%Y-%m-%dT%H:%M:%S.000Z)"
P="$UV run --quiet python e2e/device_probe.py --host localhost --port 18883 --ca $WORK/certs/ca.crt"
echo "1) device publishes over TLS, gets ACCEPTED ack:"
$P --user kn-node-001 --password "$PW_DEV" --seq 0 --boot-id 0badc0de --measured-at "$NOW"
echo "2) same identity again (QoS 1 redelivery), gets DUPLICATE ack, stored once:"
$P --user kn-node-001 --password "$PW_DEV" --seq 0 --boot-id 0badc0de --measured-at "$NOW"
echo "3) wrong password is refused by the broker:"
if $P --user kn-node-001 --password wrong --seq 1 --timeout 4 >/dev/null 2>&1; then echo "UNEXPECTED: accepted"; exit 1; else echo "refused (ok)"; fi
echo "4) a device cannot publish as another device (ACL): kn-node-002 writing kn-node-001's topic gets no ack:"
"$UV" run --quiet python - "$WORK/certs/ca.crt" "$PW_OTHER" <<'PY'
import json, ssl, sys, time
import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion
c = mqtt.Client(CallbackAPIVersion.VERSION2, client_id="kn-node-002-x")
c.username_pw_set("kn-node-002", sys.argv[2]); c.tls_set(ca_certs=sys.argv[1], tls_version=ssl.PROTOCOL_TLS_CLIENT)
c.connect("localhost", 18883); c.loop_start()
c.publish("kisaniti/v1/devices/kn-node-001/telemetry", json.dumps({"spoof": True}), qos=1).wait_for_publish(5)
time.sleep(2); c.loop_stop(); print("published attempt sent")
PY
sleep 1
echo "store contents:"
"$UV" run --quiet python -c "
from kisaniti_ingestion.store import open_store; s=open_store('$WORK/readings.sqlite3'); print(s.counts())
d=s.readings_for('kn-node-001'); print([ (x['reading_id'], x['ingest_status'], x['provenance']['origin']) for x in d])"
echo "worker log:"; cat "$WORK/ingest.log"

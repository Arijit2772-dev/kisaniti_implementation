"""Act as a registered device: publish one telemetry message over TLS and wait for the backend's ack.

Usage: uv run python e2e/device_probe.py --host localhost --port 8883 --ca broker/certs/ca.crt \
       --user kn-node-001 --password ... [--seq 1]
Exit code 0 only if an ACCEPTED/DUPLICATE ack for this exact boot_id/seq arrives within the timeout.
"""

from __future__ import annotations

import argparse
import json
import secrets
import ssl
import sys
import threading
from datetime import UTC, datetime

import paho.mqtt.client as mqtt
from kisaniti_core.timeutil import format_instant
from paho.mqtt.enums import CallbackAPIVersion


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--port", type=int, default=8883)
    ap.add_argument("--ca")
    ap.add_argument("--user", default="kn-node-001")
    ap.add_argument("--password")
    ap.add_argument("--seq", type=int, default=0)
    ap.add_argument("--boot-id", default=secrets.token_hex(4))
    ap.add_argument("--timeout", type=float, default=15)
    ap.add_argument("--measured-at", help="instant to send (default: now)")
    a = ap.parse_args()
    dev = a.user
    doc = {
        "schema": "kisaniti.device_telemetry.v1",
        "device_id": dev,
        "boot_id": a.boot_id,
        "seq": a.seq,
        "measured_at": a.measured_at or format_instant(datetime.now(UTC)),
        "time_quality": "NTP_SYNCED",
        "uptime_ms": 1000,
        "temperature_c": 27.4,
        "humidity_pct": 63.1,
        "read_status": "OK",
        "sensor_model": "SHT30",
        "firmware_version": "e2e-probe",
        "source_origin": "DEVICE",
        "sample_interval_s": 300,
        "resent": False,
        "dropped_since_boot": 0,
    }
    got = threading.Event()
    result: dict[str, object] = {}
    c = mqtt.Client(CallbackAPIVersion.VERSION2, client_id=f"{dev}-{a.boot_id}")
    if a.password:
        c.username_pw_set(a.user, a.password)
    if a.ca:
        c.tls_set(ca_certs=a.ca, tls_version=ssl.PROTOCOL_TLS_CLIENT)

    def on_connect(cl: mqtt.Client, *_: object) -> None:
        cl.subscribe(f"kisaniti/v1/devices/{dev}/ack", qos=1)

    def on_subscribe(cl: mqtt.Client, *_: object) -> None:
        cl.publish(f"kisaniti/v1/devices/{dev}/telemetry", json.dumps(doc), qos=1)

    def on_message(_cl: mqtt.Client, _u: object, msg: mqtt.MQTTMessage) -> None:
        ack = json.loads(msg.payload)
        if ack.get("boot_id") == a.boot_id and ack.get("seq") == a.seq:
            result.update(ack)
            got.set()

    c.on_connect, c.on_subscribe, c.on_message = on_connect, on_subscribe, on_message
    c.connect(a.host, a.port)
    c.loop_start()
    ok = got.wait(a.timeout)
    c.loop_stop()
    print(json.dumps({"sent": f"{dev}:{a.boot_id}:{a.seq}", "ack": result if ok else None}))
    return 0 if ok and result.get("status") in ("ACCEPTED", "DUPLICATE") else 1


if __name__ == "__main__":
    sys.exit(main())

# L3b ingestion — live broker check, 22 Oct 2026

Command: `backend/ingestion/e2e/run_local_e2e.sh` (eclipse-mosquitto 2.0.20 in Docker, TLS on 8883 with a throw-away CA, per-device passwords and the ACL in `broker/acl`; worker `kn-ingest`; SQLite store). The device side is `e2e/device_probe.py` publishing a DeviceTelemetryV1 message, **not** the ESP32 firmware.

Result: **PASS** (4/4)

```text
1) device publishes over TLS, gets ACCEPTED ack:
{"sent": "kn-node-001:0badc0de:0", "ack": {"schema": "kisaniti.device_ack.v1", "boot_id": "0badc0de", "seq": 0, "status": "ACCEPTED"}}
2) same identity again (QoS 1 redelivery), gets DUPLICATE ack, stored once:
{"sent": "kn-node-001:0badc0de:0", "ack": {"schema": "kisaniti.device_ack.v1", "boot_id": "0badc0de", "seq": 0, "status": "DUPLICATE"}}
3) wrong password is refused by the broker:
refused (ok)
4) a device cannot publish as another device (ACL): kn-node-002 writing kn-node-001's topic gets no ack:
published attempt sent
store contents:
{'sensor_readings': 1, 'sensor_reading_conflicts': 0, 'sensor_rejected_messages': 0}
[('kn-node-001:0badc0de:0', 'ACCEPTED', 'REAL')]
worker log:
2026-10-22 21:24:12,400 INFO devices registered: kn-node-001, kn-node-002; store: <tmp>
2026-10-22 21:24:12,401 INFO connected to localhost:18883 (Success); subscribing kisaniti/v1/devices/+/telemetry
2026-10-22 21:24:15,124 INFO kisaniti/v1/devices/kn-node-001/telemetry 414 -> NEW
2026-10-22 21:24:15,220 INFO kisaniti/v1/devices/kn-node-001/telemetry 414 -> DUPLICATE
```

Not covered here: the real ESP32 (not bought yet); the PostgreSQL store (same SQL, not yet run against the product database); broker-side persistence across restarts.

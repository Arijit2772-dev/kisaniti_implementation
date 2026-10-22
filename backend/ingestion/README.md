# MQTT ingestion for storage nodes (Layer 3b)

Validates each `DeviceTelemetryV1` message, stores it once as a `SensorReadingV1`, and only then publishes the
acknowledgement the firmware waits for. Contract: `kisaniti/docs/contracts/L3b_SENSOR_STORAGE_CONTRACT_v1.md`
(schemas in `contracts/sensor/v1/`). The storage rule itself is `kisaniti_core.storage` (STORAGE-DEMO-0.1).

| Module | Job |
|---|---|
| `validate.py` | topic ↔ device check, JSON Schema, registry, future-time check, channel validity, provenance from the **registry** (DEVICE → REAL; twin/simulator → SYNTHETIC) |
| `store.py` | `INSERT … ON CONFLICT DO NOTHING` on `reading_id`; same content → DUPLICATE, different content → kept first copy + `sensor_reading_conflicts`; unparseable messages → `sensor_rejected_messages`. Commits before returning (SQLite `synchronous=FULL`; the same SQL runs on PostgreSQL) |
| `service.py` | decides the ack: ACCEPTED / DUPLICATE / QUARANTINED; never acks for an unregistered device or a topic that names another device |
| `worker.py` (`kn-ingest`) | paho-mqtt 2.1 client, QoS 1 subscription to `kisaniti/v1/devices/+/telemetry` |
| `assess_cli.py` (`kn-assess`) | prints the current `StorageAssessmentV1` for one device from the store |

## Run

```bash
cd backend/ingestion
uv sync
uv run pytest                      # 16 tests
./e2e/run_local_e2e.sh             # live: Mosquitto (TLS + ACL) in Docker -> worker -> SQLite -> ack
```

Real use with the node (laptop on the same hotspot):

```bash
broker/gen_certs.sh 192.168.43.10      # the laptop's IP on the hotspot; put certs/ca.crt into firmware secrets.h
broker/add_user.sh ingestion           # then one user per device: broker/add_user.sh kn-node-001
docker run -d --name kn-broker -p 8883:8883 -v "$PWD/broker":/mosquitto/config eclipse-mosquitto:2.0.20
cp devices.example.json devices.json   # register device IDs and their source (DEVICE or WOKWI_TWIN)
KN_MQTT_HOST=localhost KN_MQTT_PORT=8883 KN_MQTT_TLS_CA=broker/certs/ca.crt KN_MQTT_USER=ingestion \
  KN_MQTT_PASSWORD=... KN_DEVICES=devices.json KN_STORE=readings.sqlite3 uv run kn-ingest
```

Wokwi twin (public test broker, no TLS, no personal data): register `kn-twin-001` as `WOKWI_TWIN` and run with
`KN_MQTT_HOST=test.mosquitto.org KN_MQTT_PORT=1883` and no CA/user. Its readings are stored as SYNTHETIC.

**macOS note:** uv's editable `.pth` files can get the `hidden` flag, which Python 3.13 skips. `pyproject.toml`
therefore sets explicit `pythonpath`/`mypy_path`; for other commands export
`PYTHONPATH=src:../../core/src`.

**Not done yet (product integration):** a PostgreSQL migration in the main API, persisting the hysteresis state
between scheduled assessments, and device/lot screens.

Evidence: `docs/reports/2026-10-22_L3b_ingestion_e2e.md`.

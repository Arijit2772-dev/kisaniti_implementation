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
uv run pytest                      # 45 tests
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

## Software node, conformance harness and PostgreSQL (H-1 to H-3, 26 Oct 2026)

| Command | What it does |
|---|---|
| `kn-sim --scenario humid_rise --steps 48 --host … --user … --password …` | Software storage node: publishes synthetic `DeviceTelemetryV1` (`source_origin=SIMULATOR`) with the firmware's buffer, ack and resend behaviour. `kn-sim --list` shows the scenarios (`stable_dry`, `humid_rise`, `sensor_fault`, `late_ntp`, `never_synced`) |
| `kn-conformance selftest` | No broker: the simulator and the in-process backend check each other (13 receiver cases, every scenario as a publisher). Runs in CI |
| `kn-conformance receiver --device-id … --origin …` | Acts as a device against a live broker and backend; checks acks against contract L3B v1 |
| `kn-conformance publisher --device-id … --origin … --seconds 120 --withhold 2` | Acts as the backend for a device under test (real node, Wokwi twin, simulator): records what it publishes, withholds the first acks, checks the messages against the contract |
| `./e2e/run_sim_conformance_e2e.sh` | Live: Mosquitto (TLS + ACL) in Docker, `kn-ingest` on PostgreSQL, all of the above |

Any new device or simulator is "compatible" when `kn-conformance publisher` reports no FAIL for it. Only rules written in
the contract are checked (send order is not specified there, so it is not enforced).

**PostgreSQL.** The tables are created by the API's Alembic migration `0002_sensor_readings` (`backend/`); `kn-ingest`
only writes to them and refuses to start on an unmigrated database. Use
`KN_STORE=postgresql://user:pass@host:5432/db` (psycopg: `uv sync --extra postgres`). Tests: `backend/tests/test_sensor_tables.py`.

**Device registry (H-4a, 26 Oct 2026).** `KN_REGISTRY=db` (with a PostgreSQL `KN_STORE`) makes `kn-ingest` read the devices registered in the website from the `devices` table (cached 30 s; an unknown device triggers at most one lookup per 5 s); `devices.json` stays for stand-alone use. Contract `devices/v1`; a deactivated device is no longer acknowledged. Live check: `REGISTRY=db PG_CONTAINER=<name> ./e2e/run_sim_conformance_e2e.sh`.

**Not done yet:** per-device broker credentials (H-4b), scheduled assessments with
stored hysteresis (H-5), device and lot screens (H-6), tenant-scoped HTTP read endpoints (they need H-4; the tables have
no `fpo_id` by design), reconnect handling in `kn-ingest` when PostgreSQL goes away (a failed write sends no ack, so the
device resends, but the worker does not retry its own connection), real-node acceptance (needs the kit).

Evidence: `docs/reports/2026-10-22_L3b_ingestion_e2e.md`.

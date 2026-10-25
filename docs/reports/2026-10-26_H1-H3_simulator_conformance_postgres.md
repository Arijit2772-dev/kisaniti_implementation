# Completion report — H-1, H-2, H-3 (conformance harness, software node, PostgreSQL persistence)

26 October 2026 • Layer L3b, contract `W01-L3B` (frozen). Plan: `kisaniti/docs/FINAL_DELIVERY_PLAN.md` §4.3. All sensor data in this report is SYNTHETIC; no accuracy or benefit claim rests on it.

## Scope

| Step | Delivered |
|---|---|
| H-1 | `kn-conformance`: 13 receiver cases (valid, duplicate, content conflict, out of order, out of range, not JSON, other device's topic, wrong origin, future time, failed read, unsynced clock, bad identity, unknown field); publisher checks (schema, size, device and origin, consecutive sequence numbers, resend marking and identical content, ack-gated retention, constant interval, non-decreasing dropped counter); `selftest` without a broker |
| H-2 | `kn-sim`: deterministic scenarios (`stable_dry`, `humid_rise`, `sensor_fault`, `late_ntp`, `never_synced`), the firmware's 288-sample buffer, ack handling and resend marking, in-process and MQTT links. Registered with `source_origin=SIMULATOR`, so the backend stores its readings as SYNTHETIC |
| H-3 | Alembic migration `0002_sensor_readings` (tables, checks, index) as the single owner of the schema; the ingestion store reads and writes it, refuses an unmigrated database, and returns jsonb as dicts |

## Contract used

`contracts/sensor/v1/*.schema.json` and `kisaniti/docs/contracts/L3b_SENSOR_STORAGE_CONTRACT_v1.md` §2, §3, §6. Nothing in the contract was changed.

## Explicit exclusions (deliberate)

- **No HTTP endpoints for readings.** The contract resolves the FPO from the device-assignment history, which needs the device registry (H-4). Exposing readings before that would break multi-FPO isolation. The tables have no `fpo_id` for the same reason. The plan's "per-lot queries" therefore move to H-4; the per-device query exists at store level.
- No send-order rule in the publisher checks: the contract does not specify one.
- No change to firmware, schemas, or the storage rule.
- No claim that simulator scenarios resemble real grain or a real sensor.

## Tests and evidence

| Check | Result |
|---|---|
| `backend/ingestion`: pytest | 45 passed (17 earlier + 28 new); ruff, ruff format, mypy strict clean |
| `backend`: pytest on real PostgreSQL 17.6 (Docker, port 5434) | 25 passed; ruff and mypy strict clean |
| Fault injection: 11 contract violations in a publisher trace plus 2 broken backends | each caught by the matching finding or failed case |
| 16 concurrent writers of one reading | exactly 1 stored, 15 DUPLICATE |
| 4 concurrent simulated devices with outages and QoS 1 duplicates | 240 rows, 0 conflicts, complete sequences |
| Database checks | 6 malformed rows rejected by constraints; a well-formed row accepted |
| In-process and PostgreSQL backends on the same receiver cases | identical answers |
| Live: Mosquitto 2.0.20 with TLS and ACL, `kn-ingest` on PostgreSQL, `e2e/run_sim_conformance_e2e.sh` | PASS: publisher test (2 acks withheld, node resent, 14 messages, 0 FAIL, 0 WARN); 13 of 13 receiver cases; 48 of 48 simulator readings stored, complete sequence, origin SYNTHETIC / SIMULATOR |

## Findings during the work

- A live-only bug: `MqttLink.send` read the identity from the payload and crashed on deliberately malformed messages. Found by the live run, fixed, covered by the live script (the in-process tests cannot reach it).
- A modelling error in my first simulator: samples buffered while the link was down were marked `resent` later. They had never been sent. The checker flagged it; the link now distinguishes "could not send" (`ConnectionError`) from "sent, no ack".
- The checker needed an overflow rule: the contract lets a device drop its oldest sample on buffer overflow, so an unacknowledged, unresent sample is only a FAIL when no overflow was reported.
- Live, 6 receiver readings reach PostgreSQL against 7 in-process: the broker's ACL stops case R07 (publishing on another device's topic) before it reaches the backend. Both layers hold.
- CI had no job for the ingestion service, so its tests ran only locally. A job was added (lint, types, tests, `kn-conformance selftest`).
- The first CI run failed: the backend environment has no `paho`, so strict mypy rejected the ingestion modules' MQTT imports. My local mypy run had not reproduced it. Fixed with a `paho.*` entry in the backend's `ignore_missing_imports`; the next run (`d3d7601`) was green on all five jobs, including the one-command stack, which applies migration 0002 in the container.

## Not verified

- Not run on a real ESP32 or on the Wokwi twin (the harness is ready for both: `kn-conformance publisher --origin WOKWI_TWIN`).
- Behaviour when PostgreSQL goes away while `kn-ingest` runs: a failed write sends no ack, so the device resends; the worker has no reconnect logic.
- `uv` is not installed on this machine, so lock files were not regenerated; the existing virtual environments were used with explicit `PYTHONPATH` (the known macOS hidden-`.pth` issue).

## Next

H-4 device registry and assignment with per-device broker credentials and tenant isolation; then H-5 scheduled assessment with stored hysteresis.

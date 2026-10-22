# Sensor and storage contracts (Layer 3b, v1 — frozen 22 Oct 2026)

JSON Schemas (draft 2020-12) for the frozen contract slice in the planning repo:
[`L3b_SENSOR_STORAGE_CONTRACT_v1.md`](../../../kisaniti/docs/contracts/L3b_SENSOR_STORAGE_CONTRACT_v1.md) (decision `W01-L3B`).

| Schema | Producer → consumer |
|---|---|
| `v1/device_telemetry.schema.json` | ESP32 firmware / Wokwi twin → MQTT ingestion |
| `v1/device_ack.schema.json` | Ingestion → device (after durable store) |
| `v1/sensor_reading.schema.json` | Ingestion → storage rule, API |
| `v1/device_assignment.schema.json` | Inventory/placement → storage rule |
| `v1/storage_assessment.schema.json` | `core.storage` (STORAGE-DEMO-0.1) → planner, UI |

`v1/examples/valid` and `v1/examples/invalid` (mapped by `examples/index.json`) are checked by the shared test `contracts/tests/test_contract_examples.py` (`core/.venv/bin/pytest contracts/tests`). Instants and provenance come from `shared/v1/common.schema.json`.

Changing a field or enum needs a new version and a decision-register entry; do not edit these files silently.

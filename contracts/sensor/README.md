# Sensor and storage contracts (Layer 3b, v1 — frozen 22 Oct 2026)

JSON Schemas (draft 2020-12) for the frozen contract slice in the planning repo:
[`L3b_SENSOR_STORAGE_CONTRACT_v1.md`](../../../kisaniti/docs/contracts/L3b_SENSOR_STORAGE_CONTRACT_v1.md) (decision `W01-L3B`).

| Schema | Producer → consumer |
|---|---|
| `device_telemetry.v1.schema.json` | ESP32 firmware / Wokwi twin → MQTT ingestion |
| `device_ack.v1.schema.json` | Ingestion → device (after durable store) |
| `sensor_reading.v1.schema.json` | Ingestion → storage rule, API |
| `device_assignment.v1.schema.json` | Inventory/placement → storage rule |
| `storage_assessment.v1.schema.json` | `core.storage` (STORAGE-DEMO-0.1) → planner, UI |

`examples/` holds one valid instance per case. `backend/ingestion/tests/test_contracts.py` validates every example against its schema, and validates real firmware output captured from the Wokwi twin.

Changing a field or enum needs a new version and a decision-register entry; do not edit these files silently.

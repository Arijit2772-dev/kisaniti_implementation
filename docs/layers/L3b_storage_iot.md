# Layer 3b guide — storage sensing and the storage-condition rule

22 October 2026 • Contract `W01-L3B`: [`kisaniti/docs/contracts/L3b_SENSOR_STORAGE_CONTRACT_v1.md`](../../../kisaniti/docs/contracts/L3b_SENSOR_STORAGE_CONTRACT_v1.md).

## How it works

```text
SHT30 probe in a basmati bag ─I²C─► ESP32 (firmware/) ─MQTT over TLS─► Mosquitto (backend/ingestion/broker)
  ─► kn-ingest: validate, store once per (device, boot, seq), then ack ─► SensorReadingV1 rows
  ─► kisaniti_core.storage.assess (STORAGE-DEMO-0.1) ─► StorageAssessmentV1 ─► planner / screens (not built)
```

| Part | Where | Tests / evidence |
|---|---|---|
| Firmware v1 (real + Wokwi profiles) | `firmware/` | `pio test -e native` (13), `pio run -e esp32dev -e wokwi` |
| SHT3x custom chip for Wokwi | `firmware/wokwi/chips/` | `wokwi/chips/test/run_test.sh` (chip vs firmware decoder) |
| JSON Schemas + examples | `contracts/sensor/v1/` | `contracts/tests` (valid and invalid examples) |
| Storage rule | `core/src/kisaniti_core/storage/` | `core/tests/storage/` (31: bands, boundaries, hysteresis, properties, schema) |
| Ingestion | `backend/ingestion/` | 17 tests; `e2e/run_local_e2e.sh` live broker check |
| Research comparison | `kisaniti_research_paper/layers/L3b_storage_iot/` | simulated scenarios, `results/L3b_storage/` |

## Why these choices (alternatives)

- **Probe inside the grain, not in shed air:** inter-granular RH tracks grain water activity, which is what the FAO
  mould limits are written in; shed air swings daily and agreed with the in-grain condition on only 19–61 % of hours
  in the simulation.
- **24 h median + hysteresis instead of the latest reading:** ≤ 0.7 category changes per week instead of up to 19
  (latest in-grain reading) or 14–27 (shed air). Cost: about 12 h delay and a cautious hold near a limit.
- **SHT30 over DHT22:** digital I²C with CRC and typical ±2 %RH in the 20–80 %RH mid range (±3–4 near the ends) and ±0.2 °C (Sensirion v7); protected probe.
- **MQTT QoS 1 + application ack after the database write:** a broker PUBACK does not prove storage; duplicates are
  expected and handled by the `(device, boot, seq)` key.
- **RAM buffer (288 samples):** simplest firmware; loses unsent samples on power cut (stated limit; flash queue later).

## How to run

See [`firmware/README.md`](../../firmware/README.md), [`firmware/BUILD_GUIDE.md`](../../firmware/BUILD_GUIDE.md) and
[`backend/ingestion/README.md`](../../backend/ingestion/README.md).

## Limits

The category estimates conditions favouring mould and insects at one probe; it is not moisture %, spoilage or shelf
life. Thresholds and the 6 h settling time are desk-research values until the bench tests (BUILD_GUIDE H4–H7) are
done. Sensor bias of ±2 %RH changes results noticeably, so calibration comes first. Product integration (PostgreSQL
table in the API, scheduled assessments with stored hysteresis, device and lot screens) is still to do.

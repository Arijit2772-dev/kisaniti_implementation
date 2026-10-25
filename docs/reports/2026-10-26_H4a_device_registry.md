# Completion report — H-4a device registry with tenant isolation

26 October 2026 • Contract `devices/v1` (frozen, CHAT-40 and CHAT-41). Fictional FPOs and devices; readings from the software node are SYNTHETIC.

## Delivered
- Contract slice `devices/v1`: 5 schemas, 22 examples (contract suite 178 passed).
- Migration `0004_devices`: `devices` (global id, one FPO, source origin, active), append-only `device_assignments` (composite key to the device's own FPO), row-level security on both, and row-level security on `sensor_readings` through the device (a tenant role can read a reading only if its device belongs to the tenant; column grants hide the payload hash, topic and raw data).
- API (`kisaniti_api/devices`): list, register (needs `users.manage`), read, assign (`data.write`), assignment history, readings (`data.read`, limit 1 to 1000), deactivate. Another FPO's device answers 404 exactly like a missing one and is audited as `CROSS_TENANT_REFERENCE`.
- `kn-ingest` registry from the database (`KN_REGISTRY=db`): cached, refreshes within seconds, one lookup per 5 s for an unknown device, deactivated devices are no longer acknowledged.

## Evidence
| Check | Result |
|---|---|
| Backend tests on real PostgreSQL | 106 passed (24 new device tests); ruff, format, strict mypy clean |
| Ingestion tests | 49 passed (4 new registry tests with a fake clock) |
| Contract examples | 178 passed |
| Tests of the tests | readings policy opened to everyone: 2 isolation tests failed; restored: 106 pass |
| Database walls | restricted role sees only its FPO's devices, assignments and readings; cannot read raw columns or conflict and rejected tables; cannot register a device for another FPO; an assignment must carry the device's own FPO; the history cannot be edited or deleted |
| End to end (in process) | device registered through the API, 30 simulator readings ingested by the real ingestion code with the registry from the table, shown only to its FPO (404 for the other); an unregistered device's readings are quarantined and visible to nobody; a never-synced clock gives no trusted last-reading time; the 13 receiver conformance cases pass with the database registry |
| **Live** (real Mosquitto TLS+ACL, `kn-ingest` with `KN_REGISTRY=db`, PostgreSQL) | publisher check 0 failures; 13 of 13 receiver cases; 48 of 48 readings stored; read back through the restricted role: own FPO 48, another FPO 0 |

## Findings while building
- An unacknowledged device keeps resending its oldest sample, so an unregistered device stores one quarantined reading, not one per sample; my first test expected five. Behaviour is right; the test was wrong.
- Device registration and assignment are not in the audit trail: the frozen audit event list has no device events. They need a new access slice version; until then `devices.registered_at` and `device_assignments.created_by` are the only trace.

## Limits
- The placement valid at a reading's time (assignment at `measured_at`) is defined in the contract but not yet computed anywhere; it arrives with the assessment job (H-5). Untrusted times must stay unmatched.
- Registering a device needs the admin capability `users.manage`; a dedicated capability is a later access version.
- Broker credentials are still created by hand (`broker/add_user.sh`); H-4b would issue them.
- No page yet (H-6).
- The CI run for this commit had not finished when this report was written.

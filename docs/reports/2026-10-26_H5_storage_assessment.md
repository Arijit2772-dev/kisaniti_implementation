# Completion report — H-5 scheduled storage assessment with persisted hysteresis

26 October 2026 • Contract `devices/v1` amendment A1 (CHAT-42); the rule `STORAGE-DEMO-0.1` and its thresholds are unchanged (L3B contract §5). Fictional FPOs and devices; simulator readings are SYNTHETIC.

## Delivered
- `devices/placement.py` (pure): the assignment valid at an instant; a reading counts for an assignment only if the assignment valid at *its own* `measured_at` is that one; readings with an untrusted or missing time are never matched.
- Migration `0005_storage_assessments`: append-only table with the full `StorageAssessmentV1` and the hysteresis state of each assessment; row-level security (a tenant reads only its own devices'); written only by the job.
- `devices/assess_job.py`: hourly slots per active device, catch-up of missed slots (at most 14 days), state carried from the last stored assessment, a new assignment resets it, one `NO_ASSIGNMENT` result for a device never assigned, per-device transaction with an advisory lock, one failing device never stops the others. CLI `--once` or `--every SECONDS`; compose service `assessor` runs it hourly.
- Endpoints `GET /devices/{id}/assessment` and `/assessments?limit=` (tenant-scoped, `data.read`).
- `scripts/demo_storage.sh`: registers a simulated device through the website, feeds 30 hours of readings through the real ingestion code, runs the job, reads the result back through the website.

## Evidence
| Check | Result |
|---|---|
| Backend tests on real PostgreSQL | 121 passed (15 new: 5 placement, 10 job and API); ruff, format, strict mypy clean |
| Equivalence | stored assessments equal the rule's own uninterrupted hourly replay (`assess_series`), every field of every slot, for a 60-hour run; and for an early run followed by a late catch-up run |
| Persisted hysteresis | readings at RH 72 for 30 h then RH 66: category HIGH is held through the 48-hour hold, including across a restart in the middle (the `below_since` timestamp carries over unchanged), and releases to MEDIUM afterwards |
| Re-assignment | the new assignment resets the state; readings of the old grain do not count; SETTLING after the move |
| Untrusted times | a device whose clock never syncs yields only UNKNOWN with zero samples used |
| Idempotence, cap, isolation of failure | a second run stores nothing; first run capped at 14 days; one device's exception leaves the others stored |
| Tenant walls | another FPO's device answers 404 with the same body as a missing one (even the message); the restricted role sees only its own assessments and cannot write, edit or delete; the owner cannot edit either; an assessment must carry the device's own FPO |
| Tests of the tests | the job forgetting the stored state: the restart test fails; counting untrusted readings: the placement test fails; both restored, 121 pass |
| Live stack | `scripts/demo_storage.sh`: 360 readings acknowledged, assessment HIGH at RH median 71.1 % (aw 0.711), 283 of 288 samples, origin SYNTHETIC, 30 hourly assessments stored; the `assessor` service runs in compose |

## Findings while building
- Real behaviour worth knowing: the rule raises LOW to MEDIUM at 25 °C or warmer with RH at least 60 %, so a warm shed never shows LOW at 60 % and above. My first test wrongly expected a LOW stage in the simulator's warm scenario; I replaced the guess with a property that must hold (rising humidity never lowers the category).
- A first run starts at the *current* assignment; slots under earlier assignments exist only if the job was running then. This is in the contract text; one test first expected otherwise.
- My demo script first used a boot ID with a non-hex letter; nothing was stored and the output showed "0 samples used". The script now fails if any reading is unacknowledged.

## Limits
- Late-arriving readings (a device that was offline and sends its buffer later) do not change slots already assessed; they count from the next slot on. A recompute option is not built.
- Slot length (1 hour) and the 14-day cap are constants in the job, not settings yet.
- Nothing is sent when a category changes (no alerts); no page yet (H-6).
- The state of the rule is the DEMO policy; thresholds, durations and the 48-hour hold remain desk-research values until the bench tests.
- Cross-FPO job runs as the database owner by design (a system process); its writes always carry the device's own FPO, enforced by a composite key.

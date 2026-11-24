# HW-S5 Full rehearsal from a fresh clone (24 Nov 2026) — Gate 1

**Ledger item:** HW-S5. `scripts/hw_rehearsal.sh [commit]`:
- clones `main` from GitHub into a temporary folder and runs it as its own compose project (`kn-rehearsal`), with **new containers and new volumes**, so nothing from an existing stack is reused;
- runs every check in order, with PASS or FAIL and the time for each step;
- refuses to start if ports 3000, 8000, 5433 or 8883 are in use;
- removes the clone and its data at the end.

## Result: REHEARSAL PASS for `af7f01b` (log: `docs/reports/evidence/2026-11-24_HW-S5_rehearsal_af7f01b.txt`)
| Step | What it proves | Time |
|---|---|---|
| broker-setup | certificates and the worker's broker user made from nothing | 2 s |
| stack-up | all six services (hardware profile) build and become healthy | 64 s |
| health | API READY (database, migrations) | 0 s |
| chain | the whole flow on fictional data: FPOs → prices → stock → sensor readings → assessment → lot linked → plan → review/edit → approval with reservation → messages → handover → sales → close → ledger audit → isolation | 22 s |
| chain-sabotage-fails | a corrupted ledger is caught at the ledger audit | 11 s |
| smoke-api, smoke-website | login, two-FPO isolation, lots, stock ledger, prices, directly and through the website | 11 s, 10 s |
| software-node-mqtt | the software node over MQTT and TLS: 12 readings acknowledged after storing, wrong password refused, other FPO gets 404, database restart under the worker with nothing lost or doubled, HW-S4 preflight, no secret in logs | 121 s |
| browser-tests | 10 browser tests at 375 px and 1280 px | 91 s |

After the run: 0 containers and 0 volumes of `kn-rehearsal`, and the clone was removed.

## Limits, stated
- The chain feeds the plan's sensor readings in-process through the real ingestion code. The MQTT path is proved separately by `software-node-mqtt`; a reading that arrives over MQTT is not followed through to a plan in the same run.
- The software node, not the DHT11, is used here; the real node is Phase 2 (HW-A to HW-F).

## Rollback
`git revert` of this commit (the script and this report only).

# Implementation status

Updated 24 October 2026. Built so far: shared `core/` (exact values, storage rule, allocation core with oracle and validator), contract schemas, MQTT ingestion, firmware, API/DB/Next.js skeleton with CI (green). No manager screens, forecasting, WhatsApp or approval yet. Overall checklist: `../kisaniti/docs/PROJECT_TRACKER.md`.

| Gate | State | Source |
|---|---|---|
| Product coding | **OPEN LAYER BY LAYER** (CHAT-28): a layer's code starts after its contracts are frozen; see [master plan](../kisaniti/docs/MASTER_PLAN.md) | [planning blueprint status](../kisaniti/docs/blueprint/00_BLUEPRINT_STATUS.md) |
| Version control | Git, GitHub `kisaniti_implementation` (private) | [open issues](../kisaniti/docs/OPEN_ISSUES.md) |
| Market-data access | Not available (OI-05; user must register) | [open issues](../kisaniti/docs/OPEN_ISSUES.md) |
| Hardware | ESP32 + SHT30 decided (CHAT-29); not purchased yet. Firmware and Wokwi twin built (22 Oct) | [hardware](../kisaniti/docs/HARDWARE_CURRENT.md), [build guide](firmware/BUILD_GUIDE.md) |

## Work packages

Definitions: [Layer 1–3 handoff §10](../kisaniti/docs/handoff/2026-10-14/KisanNiti_Layers_1_to_3_Handoff.md) and [continuation tasks](../kisaniti/docs/blueprint/08_CONTINUATION_TASKS.md).

| ID | Work package | Status | Report |
|---|---|---|---|
| W00 | Promote amendments/status; reconcile supersessions | Done in planning folder (15 Oct) | Planning registers |
| W01 | Freeze contracts, numeric/date semantics, DEMO policy bundle (incl. MSP_SCHEDULE, STORAGE-DEMO-0.1, privacy contract, hold/sell advisory spec) | **PARTIAL (22 Oct):** shared basics (`shared/v1`), L3b sensor and L3d allocation slices frozen; L1, L2, L3a, L3c, L4, L5 slices and the DEMO policy bundle still to freeze | planning decision register |
| W02 | Foundation: tenancy, exact values, migrations, audit, jobs/idempotency | NOT STARTED (needs gate) | — |
| W03 | Organisation/storage fixtures, inventory core | NOT STARTED | — |
| W04 | Market adapter + bounded authorised import | BLOCKED on OI-05 | — |
| W05 | Cleaning, readiness, dataset releases | NOT STARTED | — |
| W06 | Sensor simulator + ingestion + UNKNOWN path | **DONE for L3b slice (22 Oct)**: contract `W01-L3B` frozen, schemas `contracts/sensor/v1`; `core.storage` STORAGE-DEMO-0.1 (31 tests); `backend/ingestion` validate/de-duplicate/store/ack (17 tests) + live TLS broker check PASS. **26 Oct (H-1..H-3):** software node `kn-sim`, conformance harness `kn-conformance`, PostgreSQL migration 0002 with 25 backend tests on real PostgreSQL, live broker + PostgreSQL run PASS ([report](docs/reports/2026-10-26_H1-H3_simulator_conformance_postgres.md)). Not done: device registry/assignment, persisted hysteresis state, device/lot screens | [layer guide](docs/layers/L3b_storage_iot.md), [e2e report](docs/reports/2026-10-22_L3b_ingestion_e2e.md) |
| W07 | Forecast baselines and evaluation | BLOCKED on data | — |
| W08 | Route/quote adapters, transport math | NOT STARTED | — |
| W09 | Optimizer oracle, MILP, PlanValidator | NOT STARTED | — |
| W10 | Planning orchestration, snapshots, proposals | NOT STARTED | — |
| W11 | Real ESP32 firmware and hardware acceptance | Firmware v1 **builds** (real TLS + Wokwi profiles, 13 host tests); Wokwi twin ran on wokwi.com (sensor read OK; MQTT leg not yet observed). Acceptance (H4–H6) **BLOCKED on hardware purchase** | [firmware/README.md](firmware/README.md), [BUILD_GUIDE](firmware/BUILD_GUIDE.md) |
| W12 | Layers 4–5 (approval, WhatsApp, sales, reconciliation) and full acceptance | NOT STARTED | — |

**26 Oct:** interface language foundation (Punjabi and English, CHAT-36): [report](docs/reports/2026-10-26_language_foundation_punjabi.md).

**26 Oct:** access slice (login, sessions, roles, two-FPO isolation, contract `access/v1`): [report](docs/reports/2026-10-26_access_login_two_fpo.md). No sign-in page yet.

## Experiments

All EXP-00…14 are **NOT_RUN**. Definitions: [research plan](../kisaniti/docs/research/RESEARCH_AND_EXPERIMENT_PLAN.md). Results go in `experiments/EXP-xx/<run-id>/report.md`.

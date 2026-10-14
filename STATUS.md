# Implementation status

Created 15 October 2026. **Nothing is implemented yet.** This folder contains only its structure and entry documents.

| Gate | State | Source |
|---|---|---|
| Product coding | **PROHIBITED** until the user authorises it and W01 contracts are frozen | [planning blueprint status](../kisaniti/docs/blueprint/00_BLUEPRINT_STATUS.md) |
| Version control | Local Git, branch `main`, first commit 15 Oct 2026; no remote yet (OI-09) | [open issues](../kisaniti/docs/OPEN_ISSUES.md) |
| Market-data access | Not available (OI-05; user must register) | [open issues](../kisaniti/docs/OPEN_ISSUES.md) |
| Hardware | Not purchased/assembled | [hardware](../kisaniti/docs/HARDWARE_CURRENT.md) |

## Work packages

Definitions: [Layer 1–3 handoff §10](../kisaniti/docs/handoff/2026-10-14/KisanNiti_Layers_1_to_3_Handoff.md) and [continuation tasks](../kisaniti/docs/blueprint/08_CONTINUATION_TASKS.md).

| ID | Work package | Status | Report |
|---|---|---|---|
| W00 | Promote amendments/status; reconcile supersessions | Done in planning folder (15 Oct) | Planning registers |
| W01 | Freeze contracts, numeric/date semantics, DEMO policy bundle (incl. MSP_SCHEDULE, STORAGE-DEMO-0.1, privacy contract, hold/sell advisory spec) | NOT STARTED | — |
| W02 | Foundation: tenancy, exact values, migrations, audit, jobs/idempotency | NOT STARTED (needs gate) | — |
| W03 | Organisation/storage fixtures, inventory core | NOT STARTED | — |
| W04 | Market adapter + bounded authorised import | BLOCKED on OI-05 | — |
| W05 | Cleaning, readiness, dataset releases | NOT STARTED | — |
| W06 | Sensor simulator + ingestion + UNKNOWN path | NOT STARTED | — |
| W07 | Forecast baselines and evaluation | BLOCKED on data | — |
| W08 | Route/quote adapters, transport math | NOT STARTED | — |
| W09 | Optimizer oracle, MILP, PlanValidator | NOT STARTED | — |
| W10 | Planning orchestration, snapshots, proposals | NOT STARTED | — |
| W11 | Real ESP32 firmware and hardware acceptance | BLOCKED on hardware | — |
| W12 | Layers 4–5 (approval, WhatsApp, sales, reconciliation) and full acceptance | NOT STARTED | — |

## Experiments

All EXP-00…14 are **NOT_RUN**. Definitions: [research plan](../kisaniti/docs/research/RESEARCH_AND_EXPERIMENT_PLAN.md). Results go in `experiments/EXP-xx/<run-id>/report.md`.

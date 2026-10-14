# Claude — start here for KisanNiti implementation

You are in the **implementation folder**. The design and every decision are in the planning folder [`../kisaniti`](../kisaniti/CLAUDE.md). You need no earlier chat context; both folders together are complete.

## 1. Read first (in this order)

1. [`../kisaniti/CLAUDE.md`](../kisaniti/CLAUDE.md) and its reading guide [`../kisaniti/docs/START_HERE.md`](../kisaniti/docs/START_HERE.md). This covers the architecture, amendments, decisions (CHAT-01…20), unknowns, the Layer 1–5 designs, the WhatsApp/multilingual requirement and hardware.
2. [`../kisaniti/docs/OPEN_ISSUES.md`](../kisaniti/docs/OPEN_ISSUES.md): what can break later and who decides.
3. [`../kisaniti/docs/blueprint/08_CONTINUATION_TASKS.md`](../kisaniti/docs/blueprint/08_CONTINUATION_TASKS.md) and the work-package table W00–W12 in [`../kisaniti/docs/handoff/2026-10-14/KisanNiti_Layers_1_to_3_Handoff.md`](../kisaniti/docs/handoff/2026-10-14/KisanNiti_Layers_1_to_3_Handoff.md) §10.
4. [`../kisaniti/docs/research/RESEARCH_AND_EXPERIMENT_PLAN.md`](../kisaniti/docs/research/RESEARCH_AND_EXPERIMENT_PLAN.md) for experiments.
5. [`../kisaniti/docs/QUALITY_AND_VERIFICATION.md`](../kisaniti/docs/QUALITY_AND_VERIFICATION.md) (CI, data audits, review rules) and [`../kisaniti/docs/design/UI_UX_PLAN.md`](../kisaniti/docs/design/UI_UX_PLAN.md) for frontend work.
6. [STATUS.md](STATUS.md) in this folder for what is already done.

## 2. Gate — check before writing any product code

The governing gate is [`../kisaniti/docs/blueprint/00_BLUEPRINT_STATUS.md`](../kisaniti/docs/blueprint/00_BLUEPRINT_STATUS.md).

- While it says **Product coding: PROHIBITED**, do not write product code here.
- Until then, you may only prepare folder structure, tooling proposals and machine-readable drafts of contracts that the planning folder already specifies.
- Product coding begins only after the user explicitly authorises it **and** the affected contracts are frozen (task W01).
- Record that authorisation in the planning folder's blueprint status and decision register, then mirror it in [STATUS.md](STATUS.md).

## 3. Where things go

| Kind of change | Folder |
|---|---|
| Design, decision, policy, contract meaning, new unknown, conflict, research plan, paper text | **Planning** (`../kisaniti`): registers and design docs |
| Code, JSON Schemas/examples, lockfiles, migrations, fixtures generators, firmware, experiment scripts and results, test evidence, completion reports | **Here** |

- If implementation reveals that the design is wrong or incomplete, **stop that piece**. Record a conflict or proposal in the planning folder, and continue only with unaffected work. Never change behaviour silently in code.
- Paper numbers come only from runs recorded in `experiments/`.

## 4. How to work

- **One small work package at a time**, using the planning folder's [task template](../kisaniti/docs/blueprint/06_IMPLEMENTATION_TASK_TEMPLATE.md): scope, contract, exclusions, tests and completion report.
- Write the completion report to `docs/reports/<date>_<task-id>.md` and update [STATUS.md](STATUS.md).
- **Binding rules (summary — the planning docs are authoritative):**
  - Exact decimal or integer quantities and money; never floats for stock or ₹.
  - Multi-FPO isolation is enforced on the server.
  - Missing data is never zero; unknown storage is `UNKNOWN`.
  - The optimizer is pure: no network or database access.
  - Validators are independent of the optimizer.
  - A proposal ≠ an approval ≠ a sale.
  - Idempotent approvals, sales and webhooks.
  - Synthetic data is labelled and never counted as forecast accuracy.
  - MSP is a fixed policy price (CHAT-20), not a forecast or a training row.
  - No real messages to fictional contacts.
- **Reporting:** test results are PASS / FAIL / NOT_RUN / BLOCKED. Mocked or simulated results are never reported as live passes.
- **Housekeeping:**
  - Pin versions with lockfiles (OI-11).
  - Keep secrets in environment variables and never commit them.
  - Large data stays in `data/` (git-ignored) with a manifest.
- **What only the user can do:** create external accounts, get API keys, handle WhatsApp sender setup, buy hardware, contact the FPO and get ethics approval. Ask the user for these; never work around them.

## 5. Planned stack (from the architecture; versions not yet pinned)

- **App:** Next.js frontend + FastAPI backend + PostgreSQL, built as one modular application (no microservices, Kafka, Kubernetes or Redis unless justified).
- **Optimizer:** MILP prototype with SCIP (PySCIPOpt), benchmarked against HiGHS and OR-Tools CP-SAT.
- **Forecasting:** XGBoost/LightGBM, statsmodels/statsforecast baselines, Chronos-2.
- **Data and devices:** Parquet for the research archive; MQTT over TLS for the ESP32.

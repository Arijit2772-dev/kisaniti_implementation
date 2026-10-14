# KisanNiti — implementation

This folder holds the **actual implementation**: code, machine-readable contracts, experiments, test evidence and build reports.

**All planning lives in a separate folder:** [`../kisaniti`](../kisaniti/README.md) (`/Users/arijitsingh/Documents/kisaniti`). That folder says *how things should be*: architecture, decisions, unknowns, layer designs, research plan and paper outline. This folder says *what has actually been built and tested*.

| Folder | Holds |
|---|---|
| `backend/` | FastAPI modular monolith (Layers 1–5 services, InventoryService, optimizer, validators) |
| `frontend/` | Next.js manager app (mobile-first) |
| `firmware/` | ESP32-WROOM-32 + SHT30 node |
| `contracts/` | Machine-readable versions of the frozen contracts (JSON Schemas, examples, policy bundles) |
| `experiments/` | EXP-00…14 runs, one folder per experiment/run with `report.md` |
| `data/` | Local datasets and raw downloads — **never committed** (see `.gitignore`) |
| `docs/reports/` | Completion report for every implementation task |

Start here as an agent: [CLAUDE.md](CLAUDE.md). Current progress: [STATUS.md](STATUS.md).

## Clone layout

The planning repo must be cloned beside this one, in a folder named `kisaniti`:

```bash
git clone https://github.com/Arijit2772-dev/kisaniti_plan.git kisaniti
```

```bash
git clone https://github.com/Arijit2772-dev/kisaniti_implementation.git kisaniti_implementation
```

Quality rules for all work here: [`../kisaniti/docs/QUALITY_AND_VERIFICATION.md`](../kisaniti/docs/QUALITY_AND_VERIFICATION.md).

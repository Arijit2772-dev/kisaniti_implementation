# KisanNiti implementation — instructions for all agents

Same rules as [CLAUDE.md](CLAUDE.md); read it first.

- **Before any work:**
  - The planning folder [`../kisaniti`](../kisaniti/AGENTS.md) is the source of truth for design.
  - Its [blueprint status](../kisaniti/docs/blueprint/00_BLUEPRINT_STATUS.md) decides whether product code may be written.
- **Where changes go:**
  - Design, decision and policy changes go to the planning folder.
  - Code and evidence go here.
- **How to work:**
  - Do one small, tested work package at a time.
  - Write a completion report in `docs/reports/` and update [STATUS.md](STATUS.md).
- **Never:**
  - invent data, results, thresholds, workflows or accuracy;
  - bypass access controls or CAPTCHAs;
  - commit secrets or large data;
  - send real messages without explicit user authorisation.

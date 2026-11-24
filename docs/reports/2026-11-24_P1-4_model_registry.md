# P1-4 Forecast models page (L3a registry, models/v1), 24 Nov 2026

**Contract:** `kisaniti:docs/contracts/L3a_MODEL_REGISTRY_CONTRACT_v1.md`, schema `contracts/models/v1/registry.schema.json`.

## Scope
- `scripts/make_model_registry.py` builds `backend/src/kisaniti_api/models/registry_c0.json` from the research repo's frozen C.0 test-season result. It records the source file's SHA-256 (`0a16b16f…`), the result commit `2444bea`, the protocol commit `864005d`, the season and the run time. It applies protocol §8/§9 and refuses what they do not settle. `--check` compares the committed file with a fresh build (run 24 Nov: matches).
- `GET /api/v1/models/registry` needs `data.read` and serves the committed document unchanged. `models/v1` is added to `/api/v1/meta`.
- Website page `/[lang]/models` (en, pa), linked in the menu after Prices. It shows:
  - the evidence;
  - the rule;
  - for each variety and horizon, the model in use and why, plus every model's status, change in error against naive, 95% interval, Holm p and pairs;
  - the untested horizons, 28 and 60 days.

## Result shown (copied, not retyped)
42 of 42 comparisons give NO_DETECTABLE_DIFFERENCE, so naive is in use for 1121 and Basmati 1509 at 1, 7 and 14 days ("the baseline is enough").

## Tests
- Contracts: 447 passed. New examples:
  - valid: the real registry, and a hypothetical promoted case;
  - invalid: status against verdict, bad checksum, an unsettled verdict, an extra field.
- Backend: 389 passed (8 new).
  - Endpoint: needs sign-in; matches the schema; is the committed document; is the same for two FPOs; carries the frozen facts (42, none promoted, naive everywhere, untested 28/60, checksum).
  - Generator on a fictional committed research repo: keeps naive when nothing is better; promotes the single BETTER model; refuses two BETTER models; refuses an unsettled verdict; refuses an untested horizon with no treatment.
- `deploy/prod_local_check.sh` ALL PASS: the models page served through Caddy over HTTPS lists 42 not-promoted rows and names "Naive (last price)".
- Frontend: lint, typecheck and i18n (745 keys) pass. The Punjabi text is AI-drafted, like the rest (REVIEW.md).

## Exclusions
- The plan/v1 policy label `PLAN_USES_NAIVE_DEMO_V1` is unchanged (needs its own decision).
- CI cannot rerun `--check`, because the research repo is not checked out there; the committed file is checked by the schema and the backend tests.

## Rollback
`git revert` of this commit.

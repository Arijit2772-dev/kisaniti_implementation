# Slice 7 — the one-command chain (X.1)

26 October 2026. Scope frozen first: planning repo `docs/design/SLICE7_ONE_COMMAND_CHAIN.md` (CHAT-48). No new contract; the chain only uses the frozen HTTP API (`shared/v1` … `sales/v1`) plus four operator steps that have no HTTP endpoint (create an FPO, import prices, feed the simulator, run the assessor).

## What it is

```bash
docker compose up --build -d --wait
python3 scripts/chain.py                      # exit 0 = every step held; exit 1 names the step
python3 scripts/chain.py --sabotage ledger    # must exit 1 at "ledger-audit"
```

Standard library only (`scripts/chain.py`), plus `scripts/chainlib.py`: an independent re-implementation of the arithmetic (grams, paise, half-even rounding, weighted average, ledger replay, message text checks). `backend/tests/test_chain_lib.py` (8 tests) checks that independent code against the product's own functions, so the two cannot drift apart unnoticed. Two new FPOs and new fictional farmers are created on every run; nothing is deleted.

Steps and what each one checks against an independent calculation: preflight (ready, all nine contracts listed); fpos (two FPOs, first-login password change); data (forecast label, naive point = newest index rounded half-even, at least two eligible mandis; fixture prices imported if the stack has none); stock (three farmers in pa/hi/en, lots 1250/800/500 kg of 1121 and one 1509 lot, an FPO B lot); sensor (30 h of simulated readings through the real ingestion code, assessor run, assessment ≥ 276 of 288 readings, linked to lot 1, invisible to FPO B); plan (proposal, optimality, idempotent replay, net return recomputed line by line, no reservation yet); review (over-allocation refused with the validator's reason, revision 2 moves 100 kg, proposal unchanged); approval (missing acknowledgement 422, stale revision 409, approve, second approve 409, reserved = approved quantities to the gram, re-planning finds `NO_ELIGIBLE_LOTS`); messages (exact text per language, blocked farmer and its fix as version 2, masked preview, reply changes nothing, a VIEWER account and FPO B never see a full number); sales (weighted price, `EXCEEDS_RESERVATION`, off-plan reconciliation with explicit release, void, two rounding ties 0.500 kg and 1.500 kg at 4401.00, execution, report, outcomes kept apart for fictional rows); close (release by a person, cancellation only to previewed farmers); ledger-audit (every lot's events compared one by one with what the chain did, replayed balances against the API); isolation.

## Run record

- Clean run against the rebuilt local stack at commit `79cb2ba` (working tree with the new files): **PASS**, all 13 steps, about 10 s.
- `--sabotage ledger` (a 1000 g RESERVED event written straight into PostgreSQL): **exit 1, `ledger-audit`**, the output shows the extra event.
- Deliberate backend bugs, applied by hand to `sales/routes.py`, backend rebuilt, chain run, source restored and rebuilt, clean run again:

| Bug | Result |
|---|---|
| amount rounded down (`//`) | FAIL at `sales`: 1.500 kg at 4401.00 expected 6602 paise, got 6601 |
| amount rounded half-up | FAIL at `sales`: 0.500 kg at 4401.00 expected 2200, got 2201 |
| weighted average divides by grams + 1000 | FAIL at `sales`: expected 445833, got 445462 |
| weighted average divides by grams + 1 | **PASS, not detected**: one gram changes the average by less than a paisa. Recorded as a limit, not hidden |

The tie cases were added to the chain because the first version had no rounding tie and could not have seen the first two bugs.

## Found while building

- The assessment job works on whole hours, so the newest assessment holds up to 12 readings fewer than 288; my first expectation (≥ 280) was wrong, the real bound is 276. Fixed in the chain, not in the product.
- The unit "a viewer sees no full number" had been checked with an admin account, which is allowed to see numbers; the check now uses a real VIEWER user.
- The backend image has no `jsonschema`, so the simulator feed runs on the host (as `scripts/demo_storage.sh` does). In CI the stack job installs the two pinned packages first.

## Limits (honest)

- Single-wall bugs are invisible by design: removing only the explicit `fpo_id` filter of a query leaves row-level security in place, so the chain still passes. The two walls are tested separately in the backend suite.
- The fixture-seeding branch of the `data` step is exercised in CI only (fresh stack, the chain runs before the smoke script); locally the stack already had prices, and I ran its three commands by hand once.
- No browser-driven run (K.8, waits for the Playwright decision), no backtest (X.2), no load test, no real WhatsApp. Everything is fictional and the translations are unreviewed.

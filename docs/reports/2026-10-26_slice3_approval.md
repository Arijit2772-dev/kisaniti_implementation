# Completion report — slice 3: the manager's review, approval and exact stock reservation

26 October 2026 • Contract `approval/v1` (frozen first, CHAT-45; text in `kisaniti/docs/contracts/L4_APPROVAL_CONTRACT_v1.md`). Fictional FPOs and farmers; real price data underneath the plans.

## What it does
A person reviews the system's proposal (plan/v1). They may change it into a new **immutable revision** that the independent validator must accept, then **approve** or **reject** the current revision. Approval writes the decision **and** the stock reservation in one database transaction. A person can later **release** what is still reserved. The system's proposal is never edited; the human decision and the stock ledger are separate records.

## Delivered
- **Migration `0009_approval`**: `plan_revisions`, `plan_decisions`, `plan_reservations`, `plan_releases`; row-level security, composite keys to the plan and lots of the same FPO, append-only (also for the owner); one decision per plan.
- **API**: `GET /plan-states`, `/plans/{id}/state`, `/revisions`, `/approval-context`; `POST /plans/{id}/revisions`, `/approve`, `/reject`, `/release`; `Idempotency-Key` on every write; capability `plans.approve` (the FPO admin) for decisions and release, `data.write` to edit.
- **Edits**: only the (lot, mandi, date) choices of the plan's frozen input, whole selling steps, no lot over its quantity, checked by the shared `PlanValidator`; a valid edit may be less fair or lower in return than the optimum (the validator does not judge optimality). Changes against the previous revision are listed line by line.
- **Approval**: refuses a stale revision, a plan the planner did not propose, an already decided plan, nothing to reserve, not enough free stock, and unacknowledged warnings. The warnings to acknowledge are the plan's own plus three non-blocking context warnings computed at that moment (a newer price dataset, a changed storage category, changed stock). No freshness threshold is invented.
- **Reservation**: one `RESERVED` ledger event per lot (exact sum of the approved revision's lines) and a row linking it to the plan; a later plan sees only the stock that is still free. **Release** returns the remainder with `RESERVATION_RELEASED` events; the approval stays on record.
- **Pages (Punjabi and English)**: on the plan page a review box with the revision history and the changes of each revision, an edit grid with the validator's refusal shown, the approval context with one tick box per warning (the button stays disabled until all are ticked), rejection with a reason, the reserved stock and the release. The plans list shows the decision and reservation state.
- **Smoke test** extended (direct and through the website): approval without acknowledgements refused, another FPO gets 404 on state, approval and release, approval replay, a second approval refused, exactly 1200.000 kg reserved once, release frees it.

## Evidence
| Check | Result |
|---|---|
| Backend tests on real PostgreSQL | 243 passed (33 new in `test_approval.py`); ruff, format, strict mypy clean; the new file was run five times in a row after the last fix without a failure |
| Concurrency | 8 simultaneous approvals of one plan with different keys: one 201, seven 409, one decision row, reserved once; 6 simultaneous double-clicks with one key: all 201 with the same decision; two plans needing the same 1000 kg approved at once: exactly one succeeds, the other stays PROPOSED and shows `INSUFFICIENT_AVAILABLE`, reserved never exceeds on hand; an edit and an approval at the same moment never split the plan |
| Atomicity | when the ledger's trigger refuses a reservation after the pre-check passed (simulated), no decision row, no reservation row and no ledger event remain |
| Reservation arithmetic | after approval reserved equals the sum of the approved revision's lines per lot, read back through the lots API and SQL; approving an edit reserves 700 kg, not the proposal's 1200 kg; release restores available stock exactly |
| Mutation checks | no reservation written; reserving the proposal instead of the edit; no advisory lock; acknowledgements not required; no "nothing left to release" check; a not-proposed plan approvable; a stale revision accepted: each made a named test fail |
| Isolation | another FPO gets the same 404 body for a real plan and a missing one on all seven endpoints (state, revisions, context, edit, approve, reject, release), audited as a cross-tenant reference; nothing it tried changed A's stock; the four tables are invisible without the tenant and cannot be updated or deleted by the tenant role or the owner |
| Contract examples | 321 passed (44 for `approval/v1`: 15 valid, 29 invalid) |
| Frontend | lint, typecheck, build pass; 500 keys in both dictionaries |
| In the browser | on a real-price plan: an edit with 450 kg refused with the validator's two issues; a valid edit saved as revision 2 with its two changed lines; the approve button disabled until all five warnings were ticked; approval reserved 2400.000 and 900.000 kg (lots page showed available 0.000); release returned both to available; Punjabi page renders |
| Smoke test | PASS directly and through the website |

## Findings and mistakes
- **A concurrency test of mine was wrong, not the code:** it chose the "loser" by completion order. It now records which plan each request belonged to.
- **A first design flaw found by the tests:** with the pre-check for acknowledgements before the stock check, the loser of a race sometimes got 422 (a new "stock changed" warning appeared the moment the winner committed) instead of 409. Stock is now checked first (a hard fact), then warnings; documented as a clarification in the contract.
- **A decided plan showed "stock changed" warnings** caused by its own reservation; a decided plan's context now carries none.
- My release test first used a rejection reason as a release reason (correctly refused as invalid).
- The contract commit message said 36 examples and this time it was right (13 + 23); the later revision examples were generated from real builder output and bring it to 44.

## Limits
- **Roles:** only the FPO admin can approve in the demo fixture; who may approve in a real FPO is UNKNOWN. "Request a change" from another person needs a workflow that is UNKNOWN and is not built.
- **No partial approval, no partial release** (one assignment or one lot); release is for the whole remaining reservation.
- **No freshness blocking**, no cutoff rules (UNKNOWN); the three context warnings only ask for an explicit acknowledgement.
- **Validator messages** shown after a refused edit are the validator's English technical sentences, in both languages; the codes around them are translated.
- **No consumption yet**: a reservation can only be released; sales consuming a reservation come in slice 5. Reason-code lists are DEMO.
- Nothing is messaged or sold; an approved plan is not yet used by any farmer-facing screen (slice 4).
- Punjabi text of the review box is unreviewed by a native speaker (extends K.6).

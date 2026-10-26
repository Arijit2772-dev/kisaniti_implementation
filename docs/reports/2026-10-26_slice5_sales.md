# Completion report — slice 5: replies, actual sales that consume the reservation, reconciliation, planned against actual

26 October 2026 • Contract `sales/v1` (frozen first, CHAT-47; text in `kisaniti/docs/contracts/L5_SALES_CONTRACT_v1.md`). Fictional FPOs and farmers; the prices under the plans are the real price data.

## What it does
Reality comes back as its own records. Staff enter **actual sales** from the J-form and the farmers' **replies**. A sale that follows an approved plan **consumes** that plan's reservation (one `RESERVATION_CONSUMED` and one `SOLD` ledger event, in that order, in the same transaction as the sale). A sale outside any plan is always recordable if the lot has the stock. Execution is derived from the sales, never toggled. The plan page shows the plan and what happened side by side, and an outcomes view compares the price the plan assumed with the price realised. The plan, the revisions, the approval and the messages are never changed by any of this.

## Delivered
- **Migration `0011_sales`**: `actual_sales`, `sale_releases`, `sale_voids`, `replies`; row-level security, composite keys, append-only (also for the owner); J-form numbers unique per FPO and lot.
- **API**: `POST/GET /sales`, `GET /sales/{id}`, `POST /sales/{id}/void`, `GET /plans/{id}/execution`, `GET /plans/{id}/report`, `GET /forecast-outcomes`, replies on instructions (`POST/GET /instructions/{id}/replies`, `GET /replies`).
- **Validation**: positive exact quantity, positive two-decimal price, date not in the future (Asia/Kolkata) and not before the harvest, known mandi, J-form number format and uniqueness, the plan line must exist in the approved revision, never below zero stock. The amount is computed by the system, half-even to the paisa.
- **Off-plan reconciliation (provisional policy `OFF_PLAN_MANAGER_CHOOSES_V1`)**: within free stock nothing else happens; beyond it the request must name which reservations to give up (exactly the shortfall, each no more than that plan still holds); without that the answer is `RECONCILIATION_REQUIRED` listing the candidates; more than the lot holds is `EXCEEDS_STOCK`. The system never chooses which reservation to break. The architecture lists this workflow as UNKNOWN to validate with a real FPO; it is a named, replaceable DEMO policy here (CHAT-47).
- **Void**: a compensating record with a reason; the sold quantity returns through a positive `ADJUSTED` event; a consumed reservation is not restored; voided sales are excluded from every figure.
- **Execution** per planned line (`NOT_STARTED`, `PARTIAL`, `COMPLETED`, deviation when sold exceeds planned) and per lot (reserved, consumed, released by a person, given up for a sale, remaining, sold off-plan). `approval/v1`'s reservation numbers now subtract consumption and sale releases, so both views agree.
- **Report**: planned against actual per line (plan price, realised weighted-average price, gap, values), each farmer's planned and sold share as exact fractions, totals including off-plan sold quantity, all labelled gross.
- **Outcomes**: plan price against realised price, with fictional and real rows summarised separately and labelled "not evidence".
- **Replies**: stored as typed (1 to 500 characters), shown to staff with the right to send messages, never parsed; a reply does not change consent, stock, plans or sales.
- **Pages (Punjabi and English)**: sales (record from the J-form, follow a plan line or not, the reconciliation prompt, void), execution and report on the plan page, outcomes, replies under each message.
- **Smoke test** extended (direct and through the website).

## Evidence
| Check | Result |
|---|---|
| Backend tests on real PostgreSQL | 295 passed (30 new in `test_sales.py`); ruff, format, strict mypy clean; the new file ran three times in a row without a failure |
| Ledger arithmetic | linked sale: events `REGISTERED 1250, RESERVED 1200, CONSUMED 500, SOLD 500`, on hand 750, reserved 700, available 50; the second sale leaves 0 reserved and the plan state shows no reserved stock and no person-release; a sale over the line but within the reservation is a 200 kg deviation; the half-way cases (25.000 kg at 0.10 per quintal = 2.5 paise gives 0.02, not 0.03) |
| Reconciliation | beyond free stock the answer lists the plan and its 1200.000 kg and writes nothing; wrong, partial, excessive, duplicate and foreign releases are refused; the accepted case writes `RESERVATION_RELEASED 250` then `SOLD 300`, leaving on hand 950, reserved 950 |
| Void | on hand returns to 1250, reserved stays 800 (not restored), the line shows 0 sold, the J-form number stays taken |
| Concurrency | 6 simultaneous sales of 300 kg on a 1000 kg lot: three succeed, three are refused, on hand 100, never negative; 5 simultaneous linked sales of 500 kg against 1200 reserved: two succeed, consumption 1000, 200 left; a sale racing a release of the same plan: the ledger stays consistent in both orders |
| Atomicity | a crash after the ledger events were written leaves no event and no sale |
| Report arithmetic | hand calculation: 500 kg at 4400.00 and 700 kg at 4500.00 give a weighted 4458.33 against the plan's 4415.00 (gap 43.33), value 53 500.00; a second farmer's sold share 0.500000 against planned 1.000000 |
| Outcomes | with one fictional and one real farmer the two summaries are separate and equal the hand-computed differences; no sales means no outcomes |
| Interplay | a fully sold plan blocks a stale sale message (`COMMITMENT_NOT_ACTIVE`) and cannot be "cancelled" (a cancellation needs a person's release) |
| Privacy | a reply containing a phone number appears in no captured log and no audit record |
| Mutation checks | no consumption written; half-up rounding (caught only after I added a true tie case); J-form duplicate check removed; void not restoring stock; execution counting voided sales; outcomes mixing real and synthetic; future dates allowed; off-plan taking from reservations without the person's choice: each made a named test fail |
| Isolation | another FPO gets the same 404 on six probes (sale, void, execution, report, reply write and read), audited; all four tables are invisible without the tenant and append-only for the tenant role and the owner |
| Contract examples | 417 passed (38 for `sales/v1`: 12 valid, 26 invalid) |
| Frontend | lint, typecheck, build pass; 670 keys in both dictionaries |
| In the browser | a linked sale of 1000 kg at 4400.00 recorded (amount 44000.00); an off-plan sale of 100 kg on a lot with no free stock produced the reconciliation prompt naming the plan and "exactly 100.000 kg"; after entering 100.000 it was recorded and the lots showed on hand 800, reserved 800, sold 100; the Punjabi plan page shows execution, plan price 4415.00 against 4400.00 (gap −15.00) and the shares 1.000000 planned, 0.416667 sold |
| Smoke test | PASS directly and through the website |

## Findings and mistakes
- My first rounding test did not contain a real tie, so a half-up mutation survived; I added an exact tie (2.5 paise) and it is now caught.
- The per-plan reservation numbers in `approval/v1` ignored sales until I changed their query; a `sum()` returned a decimal and broke every approval test with a 500 until I cast it, which the existing tests caught at once.
- The sales page first offered plan lines of plans whose reservation was used up; it now offers only lines with stock still reserved.
- The contract says void can answer `EXCEEDS_STOCK`; it cannot (a void adds stock). Noted below as a clarification, not changed in the frozen text.
- This time the contract commit message count (29) was right; the examples later grew to 38 with real execution and report documents.

## Limits
- **The off-plan reconciliation workflow is provisional** (§14 of the architecture: UNKNOWN until a real FPO shows what it does). Do not treat it as validated.
- **Gross values only:** transport costs actually paid are not recorded, so planned value and sale value are compared gross, not net.
- **Plan price against realised price is not a forecast evaluation.** The realised price may come from another mandi or day; the pre-registered protocol (C.0) defines the real evaluation, which has not run. Fictional rows must never count.
- **No correction of a sale** except a void (which does not restore a consumed reservation); no editing.
- **Replies are typed in by staff**; the webhook source is reserved, not built (needs a public HTTPS address). The study of farmers' understanding (S5.6) needs ethics approval and is not built.
- Release of an unused reservation is whole-plan only (as in `approval/v1`).
- Punjabi text of the new pages is unreviewed by a native speaker.

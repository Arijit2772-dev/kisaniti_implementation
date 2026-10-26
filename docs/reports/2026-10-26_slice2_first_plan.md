# Completion report — slice 2: baseline forecast with ranges, sensor link, first plan proposal

26 October 2026 • Contract `plan/v1` (frozen first, CHAT-44; text in `kisaniti/docs/contracts/L3_PLAN_CONTRACT_v1.md`). Fictional FPOs and farmers; the prices are the real AGMARKNET basmati release (still `PENDING_HAND_CHECK`).

## What a plan is, and is not
A plan is an **immutable proposal**: the planner freezes the inputs (stock, prices, mandi basis, entered transport, storage state), runs the existing exact optimiser and the independent validator, and stores the result with a coded explanation. It is **not** an approval, reserves no stock and sends nothing; approval is stage 3. The forecast is a **baseline** and says so on its face (`NOT_ASSESSED`, `NOT_VERIFIED`, "not validated on held-out data"). The plan uses the naive forecast, as the draft protocol's promotion rule (§9) says to when nothing has been shown to beat it.

## Delivered
- **Core `kisaniti_core.forecast`** (pure, exact): the variety index as the research script defines it (median modal price of at least three mandis, dated rows only, duplicate mandi-days dropped); naive and 5-day moving-average forecasts at 1, 7, 14 days; empirical 80 % and 95 % ranges (split-conformal form: the ⌈(n+1)c⌉-th smallest past error, no range below 40 pairs); the mandi basis (median difference to the index over the last 30 days, at least 5 days).
- **Migration `0008_plans`**: `sensor_links` and `plans`, tenant-owned with row-level security, composite keys, append-only (also for the owner).
- **API**: `GET /forecasts`, sensor links (`POST/GET /lots/{id}/sensor-links`, `GET /sensor-links/current`), `POST/GET /plans`, `GET /plans/{id}`; `Idempotency-Key` on plan creation; every refusal has an issue code from the contract.
- **Storage from the linked sensor**: the category of the lot's sensor if its latest assessment is at most 2 hours old, else UNKNOWN with the reason; UNKNOWN never gets a rank.
- **Optimiser in the product**: `highspy` added to the backend (lock file regenerated with uv 0.9.5 in Docker). A result that is not proven optimal, has no solution or crashed is stored as `NOT_PROPOSED` with its reason.
- **Pages (Punjabi and English)**: plans list; new plan with the price outlook (both methods, ranges, the unchecked-data warning), mandi choice with the transport cost entered by the manager, horizons, selling step; plan detail with warnings, what to sell, totals, each farmer's share, the explanation in words, lots considered with their storage state, rules used. Sensor link on the lots page.
- **Smoke test** extended (direct and through the website): forecast label, a plan from a seeded fixture, idempotent replay, another FPO cannot read or list it or plan on its stock, nothing reserved, sensor link isolation.

## Evidence
| Check | Result |
|---|---|
| Index against the research script on the real data | 433 of 433 days for 1121 and 369 of 369 for Basmati 1509: identical value and mandi count for every day |
| Core tests | 533 passed (59 new in `test_forecast.py`); ruff, strict mypy clean |
| Mutation checks (core) | floor instead of ceil, no "target not after origin" filter, averaging duplicate mandi-days, a 4-day average, half-up rounding: each made a named test fail |
| Backend tests on real PostgreSQL | 209 passed (34 new in `test_plan.py`); ruff, format, strict mypy clean |
| Mutation checks (backend) | reserved stock ignored, not-proven result accepted, no sensor-age check, idempotency key reuse undetected, transport ignored, unknown storage given HIGH: each made a named test fail |
| Plan against independent computation | net return recomputed per line in the test equals the stored total; a small plan equals the brute-force oracle's allocation, fairness, storage and net return; the validator accepts the stored plan |
| Contract examples | 277 passed (37 for `plan/v1`) |
| Frontend | lint, typecheck, build pass; 409 keys in both dictionaries |
| In the browser, real prices | outlook for 1121 from the 2026-10-19 index (4355.00); chose Dirba (+30.00 basis, 25.00 transport) and Mansa (+60.00, 40.00); the plan sent all 3,300 kg to Mansa on the first date: 3,300 kg × (4,415.00 − 40.00) / 100 = 144,375.00 ₹ net, which is the stored total; Punjabi page renders |
| Smoke test | PASS directly and through the website |

## Findings
- **Real ranges are wide.** For 1121 the naive 7-day 80 % range is 4055 – 4655 and the 95 % range 3340 – 5370 around 4355 (₹ per quintal). A manager should see that, and the page shows it. No claim about accuracy follows; the protocol's held-out test has not been run.
- **Real data limits the choices.** Of nine mandis with any recent 1121 price, only five have at least five recent price days; the page lists the others as not eligible.
- **The newest price is 7 days old** (monthly files); every plan warns about it, and its dates are counted from that day.
- **A CI trap, caught locally.** Importing the optimiser made backend `mypy` follow the core's solver wrapper, which has ignores for solvers not installed in the backend. Overrides were added to the backend's mypy settings (the same family of problem as BUG-003).
- **Tool note:** the in-app browser pane must be visible while testing; with the pane hidden the website's pages did not finish hydrating (a property of the hidden pane, not of the product).
- **My own errors:** the contract commit message said 36 examples (26 at that commit); two early test assertions ended in `or True` and were replaced with real checks; my first "price not positive" test did not test that, because the basis is relative to the index, so I rebuilt it with an index that falls at the end of the window.

## Limits
- Every DEMO policy in `plan/v1` §1 is a stated setting, not evidence: increment, no transport model (entered by the manager), no waiting cost, basis window, the 2-hour sensor age, the 30 s solver limit. See O48 to O52.
- No hold-or-sell advice, no model comparison, no seasonal naive; the words "no reliable signal" have no rule yet.
- Inactive members' lots are still planned (O52). Plans for fictional farmers are labelled `SYNTHETIC`.
- Approval, reservation and release do not exist; two plans can propose the same stock. That is stage 3.
- The page shows lot labels as member and quantity; two lots of one member with the same quantity look alike.
- Punjabi text of the new pages is unreviewed by a native speaker (extends K.6).

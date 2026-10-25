# Completion report — slice 1: members, lots, stock ledger, market reference data, price import, pages

26 October 2026 • Contracts `org/v1` and `market/v1` (frozen, CHAT-43; text in `kisaniti/docs/contracts/L1_L2_ORG_AND_MARKET_CONTRACT_v1.md`). Fictional FPOs, members and lots (origin `SYNTHETIC`). The price rows are real AGMARKNET data from the research repository's release `basmati_punjab_release1`.

## Delivered
- **Members and storage places** (`org/routes.py`, migration `0007_org`): name, village, preferred language (Punjabi, Hindi, English), active flag; places of kind collection centre, farm, warehouse, third party. No phone numbers yet (they come with consent, slice 4).
- **Lots and the stock ledger**: a lot is one crop of one member with a registered quantity in exact kilograms (integer grams). Quantities cannot be typed into a balance: `stock_events` is append-only (REGISTERED, ADJUSTED, RESERVED, RESERVATION_RELEASED, RESERVATION_CONSUMED, SOLD) and the balances are derived. A database trigger locks the lot and refuses any event that would make on-hand, reserved or available negative (HTTP 409 `STOCK_INVARIANT`). Registration and adjustment take an `Idempotency-Key`; the same key and body replays the first answer, the same key with another body answers 409.
- **Placements**: a lot's place over time; the current place is the latest one not in the future.
- **Market reference data** (`market/routes.py`, migration `0006_market`): global (not per FPO) commodities, mandis, MSP entries, dataset versions with a status log, and price observations; a rejected dataset is hidden from the tenant role by row-level security.
- **Import with refusals** (`market/import_release.py`, CLI): all or nothing; every file's checksum must match the manifest, row counts must reconcile exactly (rows = dated + blank-date), prices and arrivals must be exact decimals, no duplicate row IDs, every market must have an entry. A release always arrives as `PENDING_HAND_CHECK`, whatever its manifest says; only an explicit, logged `set-status` changes it. A repeat import is recognised; a corrected release is a new version, nothing is updated.
- **MSP import**: 18 values of the verified table; a changed file is refused by checksum.
- **Website pages in Punjabi and English** (UI to be restyled by the owner): members, lots (register, correct stock with a reason, history, move to a place, add places), prices (dataset status with the warning while unchecked, counts, newest date and age, season-by-variety table, filter by mandi, variety and dates, MSP table with source links), storage sensors (H-6: list, category with reasons in plain words, last reading, humidity of the last 24 hours with the 65 % and 70 % limits, placement form, registration and deactivation for admins). Navigation links; every error shows its request reference.
- **Smoke test extended** (`scripts/smoke_access.sh`, run directly and through the website in CI): members, lots, ledger isolation between two FPOs (list, read, adjust, register against another FPO's member), idempotent replay, key reuse 409, overdraw 409, exact balance after an adjustment, the ledger listing, and the market reads. CI seeds a fixture release with invented numbers (`scripts/make_ci_release.py`, flag `SMOKE_SEED_MARKET=1`, never for a stack with real prices).

## Evidence
| Check | Result |
|---|---|
| Backend tests on real PostgreSQL | 175 passed (54 more than at H-5: 21 org and 12 market test functions, some parametrized); ruff, format and strict mypy clean |
| Contract examples | 240 passed |
| Frontend | lint, typecheck and production build pass; `npm run i18n:check`: English and Punjabi have the same 286 keys |
| Real import (local stack) | 6 678 rows, 86 mandis, dates 2021-09-16 to 2026-10-22; 6 098 with their own date, 580 with the date taken from the row above, 8 flagged; status `PENDING_HAND_CHECK`; a repeat is recognised; MSP 18 added; a copy with one appended byte refused (exit code 2) |
| In the browser (phone width, both languages) | invalid quantity refused in Punjabi; a lot of 1250.000 kg registered through the form, balances 1250.000 / 0.000 / 1250.000 / 0.000; an overdraw of −2000.000 refused with the message and request reference, stock unchanged; prices page shows the warning and the filter returns Khanna 1121 rows exactly as stored; sensors page shows HIGH at RH median 71.3 %, 287 of 288 readings, 287 chart points, label "experimental approximation" |
| Smoke test, direct and through the website | PASS both (the second only after BUG-007 was fixed) |
| Earlier mutation checks (backend) | removing row-level security, carrying no state, removing the row lock, removing the overdraw check and removing the rejected-dataset policy each made a test fail |

## Bug found and fixed
- **BUG-007** (`docs/BUGS.md`): through the website a lot registration sent twice with the same `Idempotency-Key` created two lots. The website's API proxy forwards only a fixed list of headers and the key was not on it; the form sends one, so every retry after a lost answer would have double-registered stock. Found by the smoke test run through the proxy (red before the fix, green after). The direct API run had passed, which is why the proxy run exists.

## Findings while building
- `str(Quantity)` includes the unit ("1250.000 kg"); the wire form must use `.to_json()`. Negative quantities answer `OUT_OF_RANGE`, which I first misjudged in a test.
- `now()` is the start of the transaction; a placement stamped slightly later looked like it lay in the future, so the current-place query uses `clock_timestamp()`.
- A sale that uses a reservation must write RESERVATION_CONSUMED and SOLD together in one transaction (matters for slice 3 and 5).
- Two of my own tests had passing-by-accident assertions (`or True`, an always-true check); I replaced them and used the mutation checks above.
- Commit-message corrections: the `contracts/access` commit says "31 examples" (actual 34); the org/market contracts commit says "98 examples" (actual 58; 236 contract tests then, 240 now).

## Limits
- **The price dataset is not validated.** It carries `PENDING_HAND_CHECK` until your 56-row hand check (B.2) and the rule for rows without a date (O9) are decided; the page says so. Nothing may be claimed from it before then.
- Mandi prices are mandi-level, not what a farmer received; the page says so.
- Not built: a link from a sensor to a lot; distance and transport-cost inputs for mandis (transport cost is UNKNOWN); per-source and per-mandi staleness on the readiness page (only the newest date and its age, from the monthly files); reservations and sales endpoints (slice 3 and 5); phone numbers and consent (slice 4); a live price source (S1.7, on hold).
- The price list shows at most 200 rows without paging.
- The Punjabi text for the new pages is unreviewed by a native speaker (extends K.6); the interface is plain on purpose.
- The API refuses writes by a viewer (tested for devices and for org); the pages also hide the write forms without `data.write`, but that hiding is not covered by an automated browser test (K.8 undecided) and I checked only the admin view by hand.

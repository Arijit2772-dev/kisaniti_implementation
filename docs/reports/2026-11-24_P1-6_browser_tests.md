# P1-6 Browser tests at phone and laptop width (24 Nov 2026)

**Plan item:** FULL_PRODUCT_PLAN Phase 1 item 6. New dev dependency: `@playwright/test` 1.56.1, pinned exactly; only Chromium is used. No product behaviour changed.

## Scope
`frontend/playwright.config.ts` defines two projects: `phone-375` (375 × 812, touch, mobile) and `laptop-1280` (1280 × 800). `frontend/e2e/` holds the tests. Each test makes its own fictional FPO with the operator command, and the texts come from the English catalog, so the tests check what the site really shows.

| Test (runs at both widths) | Checks |
|---|---|
| Wrong password | the error message is shown; a protected page then sends to sign-in |
| First sign-in, every page | the site insists on a new password, then the home page; all 12 menu pages show their heading and the build footer, nothing is wider than the screen, and there are no page errors or console errors |
| Member and lot | add a member through the form; it appears; register a 1250.000 kg lot for that member; the row shows exactly 1250.000 |
| Language | switch to Punjabi (`lang="pa"`, Punjabi footer, no sideways scroll), back to English, sign out; a protected page then sends to sign-in |
| New password | after sign-out, the new password signs in |

CI: the `stack` job runs `npx playwright test` against the running compose stack, after the smoke check (which seeds the fixture prices).

## Results (Mac, 24 Nov, against `docker compose up --build`)
- The first full run had 3 failures, all faults in my tests:
  - the "New password" label also contains its hint, so the exact label match never matched;
  - the status page's h1 is the brand name, not "System status".
  - One "sign out" click timed out at laptop width. It did not happen again: that test passed alone, and the next two full runs passed 10/10 each (20/20).
- After the fixes: two consecutive full runs, 10 passed each.
- On a 375 px screen, none of the 12 pages scrolls sideways.

## Exclusions (next increment, stated)
- Through the browser: plan review, approval, edits, messaging preview, handover, and sales. These workflows are covered end to end at API level by `scripts/chain.py` (13 steps, CI) but not yet by clicking. They are the next browser-test step and depend on Phase 5 page designs, so they will be written against the final pages.
- Real phones (Gate 3 is on the public URL from a phone and a laptop).

## Rollback
`git revert` of this commit.

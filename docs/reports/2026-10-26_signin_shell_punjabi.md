# Completion report — sign-in page, application shell, users page (Punjabi and English)

26 October 2026 • Contracts `access/v1` (amendment A1, CHAT-39) and the interface-language decision CHAT-36. Fictional FPOs and users only.

## Delivered
- **Pages** under `/pa` and `/en`: sign-in, change password (forced for new and reset accounts), home (who you are, which FPO, what you can do, system status), users (list, add, reset password, deactivate; temporary password shown once with copy), public system status.
- **Application shell:** header with FPO name, person and role; language switch that saves the choice on the account; sign out; navigation (the Users link only for `users.manage`).
- **Website-to-API proxy** (`/api/v1/*`, a runtime route handler): the session cookie belongs to the website's own address, the API address is read when the container starts, request IDs are forwarded, errors pass through unchanged.
- **Real redirects:** no session cookie on a page that needs one gives HTTP 307 to sign-in (a courtesy check; the page still asks the API).
- **Errors in the person's language** from API codes, each with the request reference.
- **Contract amendment A1:** `PrincipalV1` carries `display_name` and `fpo_name` so the header can show who and which FPO.
- Catalog check now covers 104 keys per language.

## Evidence
| Check | Result |
|---|---|
| Backend tests (real PostgreSQL) | 82 passed; ruff, format, strict mypy clean |
| Contract examples | 158 passed |
| Frontend `build`, `typecheck`, `lint` (0 warnings), `i18n:check` | pass |
| Smoke script through the API and through the website proxy | both PASS (login, must-change gate, CSRF, logout, two-FPO isolation, throttle) |
| In the browser, by hand | signed out `/en` goes to sign-in; Patiala admin signs in with the temporary password, is forced to change it, reaches home; language switch to Punjabi keeps the page and saves the choice; create a staff user through the form (temporary password shown once, list updated); sign out; Ludhiana admin signs in and the Users page shows **only** Ludhiana's user, not Patiala's |
| Phone-width screenshots | Gurmukhi renders correctly (zoom-checked); the users table stacks into labelled cards (the first version hid the Actions column off screen; found by this check and fixed) |

## Limits, stated plainly
- **No automated browser tests.** The click-through above was done by hand in the app's browser. Automated browser tests need a new dependency (e.g. Playwright); the decision is open.
- **Punjabi text is AI-drafted and unreviewed** (`src/i18n/REVIEW.md`); the Punjabi role names and error messages especially need a native reader.
- A forged or expired cookie gets HTTP 200 with a page that redirects in the browser once the API says 401; no protected data is sent. A signed-out visit with no cookie gets a true 307.
- The language switch saves the preference with a best-effort call; if that call fails the page still switches language.
- Only one browser engine and one narrow viewport were checked; no real phones.
- Users page shows reset and deactivate with a browser confirmation dialog, which is plain but not localised beyond the message text.
- Not built yet: pages for data, prices, storage, plans, messages; the device registry (H-4a).

## Next
H-4a: `devices` and `device_assignments` with `fpo_id`, the ingestion registry read from PostgreSQL, the FPO of a reading resolved from the assignment history at `measured_at`, tenant-scoped device endpoints, and two-FPO tests for all of it.

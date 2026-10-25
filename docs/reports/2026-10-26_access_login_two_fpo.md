# Completion report — access slice: login, sessions, roles, two-FPO isolation

26 October 2026 • Contract `access/v1` (frozen, planning repo `docs/contracts/L0_ACCESS_CONTRACT_v1.md`; decisions CHAT-35 to CHAT-38). All users and FPOs in tests and demos are fictional (SYNTHETIC).

## Delivered
- Contract slice: 10 schemas, 34 examples (13 valid pass, 21 invalid fail), checked by `contracts/tests` (157 examples across all slices).
- Migration `0003_access`: `users`, `sessions`, `login_attempts`, append-only `audit_log`; restricted database role `kisaniti_app`; row-level security on `users` and `audit_log`; column-level grants (the restricted role cannot read `password_hash`, cannot touch `sessions` or `login_attempts`, can only append to the audit log).
- API (`backend/src/kisaniti_api/access/`): sign-in, sign-out, `me`, change password, language, list/read/create/reset/deactivate users; scrypt hashing (N=2^17, r=8, p=1, OWASP setting read 26 Oct) with parameters stored per hash; server-side sessions with idle and absolute expiry; CSRF token plus Origin check; per-username throttling that does not reveal whether a user exists; must-change-password gate; capabilities per role; operator command (`python -m kisaniti_api.access.cli`) that refuses to create a non-fictional FPO.
- Two walls for tenant data: every tenant query filters on the principal's FPO, and PostgreSQL row-level security restricts the transaction to that FPO (`SET LOCAL ROLE kisaniti_app` + `app.fpo_id`).
- `scripts/smoke_access.sh`: the same properties over HTTP against the running compose stack, in CI.

## Evidence
| Check | Result |
|---|---|
| Backend tests on real PostgreSQL 17.6 | 81 passed (56 new); ruff, ruff format, strict mypy clean |
| Contract examples | 157 passed |
| Responses vs schemas | principal, user, temporary-password response, throttled problem and audit event validated against `contracts/access/v1` |
| **Tests of the tests** | with the application's FPO filter removed, the database wall alone kept the FPOs apart (56 passed); with the filter removed and row-level security off, 3 isolation tests failed |
| Live stack (Docker, real HTTP) | must-change gate, change password, create user, B cannot list or read A's user (404, same body as a missing user), reset/deactivate by B refused (404, A's hash and flag unchanged), logout needs the CSRF token and ends the session, throttle sequence 401 x5 then 429 for a nonexistent username |

## Decisions made while building (all configuration, listed in the contract)
Scrypt instead of Argon2id (no new dependency or lock change); idle 120 min, absolute 12 h, 5 failures in 15 min block for 15 min, password length 10 to 256: DEMO policy values, to be reviewed before any pilot.

## Not done / limits
- **No sign-in page yet.** The website still has only the status page; the sign-in screen, application shell and interface language saved per user come next, in both languages.
- Throttling is per username, not per address (the address behind a proxy is unreliable). A determined attacker can lock a known username out for 15 minutes; the admin can still reset it. This is a stated trade-off.
- No breached-password check and no second factor.
- `SESSION_EXPIRED` is in the audit-event list but is not emitted; expired sessions simply answer 401.
- A password reset changes the hash and the must-change flag in one transaction, then revokes that user's sessions in a second one. If the second step failed, old sessions would remain, but they are blocked by the must-change gate (they can only change the password or sign out).
- Usernames are unique across the platform, so a conflict tells an admin that a name exists in another FPO. Accepted for admins only.
- Cookies tested over plain HTTP with `Secure` off for local development; the `Secure` flag is asserted in a unit test only. A real HTTPS deployment is not yet exercised.
- CI: the new smoke step and updated backend job had not run on GitHub at the time of writing.

# P3 first production deploy to app.kisaniti.in (28 Nov 2026)

**Decisions used:** CHAT-54 (Azure VM, kisaniti.in, deploy by commit), CHAT-53 (public URL, fictional data only). Owner approved the deploy in chat ("Yes, deploy it").

## What was done
- DNS (owner's registrar, YesDomain Premium DNS on Cloudflare nameservers): A records `@`, `app`, `mqtt` → 172.128.33.73, proxy off (DNS only), added 28 Nov after the registrar's KYC hold was lifted 27 Nov. Verified with `dig` against the Cloudflare nameservers, 1.1.1.1 and 8.8.8.8.
- `KN_DOMAIN=kisaniti.in deploy/deploy.sh a20c469`: first run created the server's `.env` (random database password, mode 600, never printed), pulled the GHCR images of `a20c469`, started db, backend, assessor, frontend and Caddy healthy, checked HTTPS and API readiness, logged `OK` in `deploys.log`.
- Commit choice: `a20c469` is the newest main commit whose CI run passed all 9 jobs including image publishing. The newer `b664ae6` adds only a record file; its CI failed on the flaky browser test below.

## Checks from outside (28 Nov, from the owner's Mac)
| Check | Result |
|---|---|
| `https://app.kisaniti.in/` | 307 → `/en`; certificate Let's Encrypt (YE2), CN app.kisaniti.in, valid to 26 Feb 2027 |
| `http://app.kisaniti.in/` and `https://kisaniti.in/` | 308 → `https://app.kisaniti.in/` |
| Security headers | HSTS max-age 31536000, nosniff, strict-origin-when-cross-origin |
| `/en/sign-in`, `/pa/sign-in` | 200 |
| Login with an unknown account | 401 |
| PostgreSQL 5432, API 8000 from the internet | closed (8000 silently dropped) |
| Server memory after start | 401 of 887 MB used; frontend 120 MB, backend 49 MB, Caddy 42 MB, db 26 MB, assessor 21 MB |

## Not done (explicit)
- No FPO or account exists on the server: the public demo FPO with demo login and nightly reset waits for approval A4. `smoke_access.sh` was not run there because it would leave fictional smoke FPOs in the production database.
- Broker on `mqtt.kisaniti.in:8883` and re-provisioning the node: P3-2.
- Nightly backup cron and uptime check on the server: later P3 items.
- WhatsApp settings in the server `.env`: Phase 4, owner's Meta steps.

## Finding: flaky browser test
`frontend e2e/workflows.spec.ts:82` "the new password works for the next sign-in" failed in CI on `a6b3a70`, `efb0a08` and `b664ae6` (record-only commits) at phone-375 and laptop-1280 with `page.goto … interrupted by another navigation` / `net::ERR_ABORTED` on `/en/sign-in`. Logged as BUG-010; not fixed here.

## Rollback
`deploy/deploy.sh --previous` (no earlier release exists yet), or stop the stack on the server: `cd kisaniti && docker compose down`.

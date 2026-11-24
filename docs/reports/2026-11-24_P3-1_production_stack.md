# P3-1 Production stack, image publishing, deploy and rollback (24 Nov 2026)

**Decision used:** CHAT-54 (Azure VM, kisaniti.in, images from GHCR, deploy and rollback by commit). No product behaviour changed.

## Scope
- `deploy/compose.prod.yaml`: the server's stack. Images `ghcr.io/arijit2772-dev/kisaniti-*:<commit>` (nothing is built on the server). Only Caddy publishes ports (80, 443 TCP and UDP). PostgreSQL, the API and the website are reachable only inside the compose network. Every value comes from the server's `.env`, and a missing value stops compose (`${VAR:?}`). Logs are capped at 3 × 10 MB per service. `restart: unless-stopped` everywhere. Cookies are Secure, and the allowed origin is `https://<app host>`.
- `deploy/Caddyfile`: automatic HTTPS for `app.<domain>` (reverse proxy to the website, which forwards `/api/v1/*`), `mqtt.<domain>` (a certificate for the broker in P3-2), and the bare domain (308 redirect to the app). HSTS, nosniff, referrer policy; the Server header is removed. No contact e-mail is sent to Let's Encrypt.
- `deploy/release.sh` (runs on the server): the first run creates `.env` with a random 48-hex database password (mode 600, never printed). Then it pulls the commit's images, runs the **migration guard**, starts everything with `--wait`, checks the website over HTTPS through Caddy and the API's `/health/ready`, and only then writes `KN_TAG` to `.env`. Every attempt is appended to `deploys.log` as OK, FAILED or REFUSED with a reason.
- `deploy/deploy.sh` (runs on the Mac):
  - `deploy.sh <commit>` requires the commit to be on `origin/main` with a successful CI run. It copies that commit's compose file, Caddyfile and release script, so a rollback also rolls back the configuration, and runs the release over SSH.
  - `deploy.sh --previous` rolls back to the last OK release of a different commit.
- CI:
  - new job `prod-stack` runs `deploy/prod_local_check.sh`;
  - new job `publish` (push to main only, after `stack` and `prod-stack`) pushes the backend, frontend and ingest images tagged with the full commit hash, with OCI source and revision labels.

## Explicit failure semantics
- Rollback across a migration: when the database is at an Alembic revision that the target commit does not know, the release is **REFUSED**, nothing is changed, and the message says to restore the backup taken before that migration. A schema is never downgraded silently.
- Invalid or short commit hash: refused before anything runs.
- Pull failure, unhealthy services, website not answering over HTTPS, or the API not READY: the attempt is FAILED, logged, and the previous `KN_TAG` stays in `.env`.

## Tests (run 24 Nov on the Mac, `deploy/prod_local_check.sh`, ALL PASS, about 2 minutes)
1. Images built for the commit under test. `.env` is created with mode 600 and a random password.
2. `release.sh` starts every service healthy, checks HTTPS and readiness, and records the commit.
3. The only published ports are `caddy:80` and `caddy:443`.
4. HTTPS works for app, mqtt and the bare domain; HTTP → HTTPS is a 308; the bare domain gives a 308 to the app; HSTS is present; the Punjabi page is served.
5. Sign-in through Caddy sets a Secure cookie. A signed-in change sent with `Origin: https://evil.example` gets 403.
6. `scripts/smoke_access.sh https://app.localhost` (login, two-FPO isolation, lots, stock ledger, prices) passes through Caddy.
7. With the database set to an unknown revision, the release is REFUSED and recorded. After the real revision is restored, the release is OK again.
8. An invalid commit is refused.

On a fresh folder, the first release is expected to log FAILED (website not reachable over HTTPS), because Caddy's local CA does not exist until Caddy first starts. The check then takes that CA and releases again. The test log records this honestly. On the server, Let's Encrypt certificates are publicly trusted, so this does not happen.

## Observation, not changed
access/v1 checks the Origin only on **signed-in** unsafe requests (CSRF). The sign-in request itself is accepted from any origin. This is the existing contract, and I recorded it here rather than changing it.

## Exclusions
- Not deployed to the server yet: DNS for kisaniti.in is not live (YesDomain still shows Pending).
- Broker on 8883 with the public certificate, and re-provisioning the node: P3-2.
- Nightly backup with a restore test, uptime check, and the public demo FPO with nightly reset: later P3 items.
- Memory on the 887 MB server: not yet measured, to be measured at the first real deploy.
- GHCR visibility, checked after the first publish (CI run 45331500821, all 9 jobs green): the server pulled all three images for `de54129` **anonymously**, so the packages are public and nothing is needed from the owner. Server at that time: 887 MB RAM (544 MB available), 56 GB disk free.

## Rollback of this change
`git revert` of this commit removes the deploy files and the two CI jobs; nothing else depends on them.

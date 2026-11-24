# P1-2 Structured logs, request ID, build identity (24 Nov 2026)

**Plan item:** FULL_PRODUCT_PLAN Phase 1 item 2. Contracts: no schema changed. `/api/v1/meta` (not covered by a contract schema) gains one field. Request IDs now follow the length rule that access/v1 `audit_event.schema.json` already sets (8–64 characters).

## Scope
- `kisaniti_api/logs.py`: JSON lines on stdout (`ts`, `level`, `logger`, `msg`, and `request_id`, `method`, `path`, `status`, `duration_ms` for requests). `build_commit()` returns `KN_BUILD_COMMIT` when it is a full 40-character hash, otherwise "unknown".
- Request middleware (`problems.py`):
  - keeps the request ID the website's proxy sends when it matches `^[A-Za-z0-9-]{8,64}$`, and replaces anything else with a new one, so a client cannot write arbitrary text into logs or the audit trail;
  - writes one access line per request, with the path only (the query string can hold search text);
  - skips successful `/health/*` probes;
  - logs status 500 when the handler raises.
- uvicorn runs with `--no-access-log` (replaced by the line above).
- Build identity:
  - `KN_BUILD_COMMIT` build argument in the backend, ingest and frontend Dockerfiles; CI's publish job passes `github.sha`;
  - shown in `/api/v1/meta` `build_commit`, in a footer on every page (`Build abc1234`, `data-build-commit` holds the full hash), and on the status page with the other versions (en and pa).
- `release.sh` now also requires the running API to report exactly the released commit, or the release is FAILED.

## Tests
- Backend: 381 passed (16 new in `tests/test_logs_build.py`; the old request-ID test now uses an ID of valid length), ruff and mypy clean. Mutation check: allowing any request ID makes `test_request_id_not_shaped_like_an_id_is_replaced` fail.
- Frontend: lint, typecheck, i18n check (710 keys, en and pa).
- `deploy/prod_local_check.sh` ALL PASS:
  - the footer and `/api/v1/meta` show the commit under test;
  - the `X-Request-ID` a browser request received through Caddy appears in the API's JSON log line for `/api/v1/meta`.

## Exclusions
- The website server (Next.js) and the ingest worker do not write JSON logs. Their own log lines stay plain; the ingest worker is in P3-2.
- The Punjabi footer and status words are AI-drafted, like the rest of the Punjabi text (i18n/REVIEW.md).

## Rollback
`git revert` of this commit.

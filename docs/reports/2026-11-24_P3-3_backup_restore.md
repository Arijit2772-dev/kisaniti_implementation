# P3-3 Nightly backup with a restore test, and a proven restore (24 Nov 2026)

**Decision used:** CHAT-54 (nightly backup with one restore proven). No product behaviour changed.

## Scope
- `deploy/backup.sh` (server, deploy folder): `pg_dump -Fc` to `backups/kisaniti-<UTC>.dump` (folder mode 700, file mode 600). Each backup is then restored into a throwaway `postgres:17.6` container with **no network**. The live cluster's roles are created there first (the FPO isolation role `kisaniti_app` lives outside the dump), and the dump is restored with `--exit-on-error`, including the grants and row-level security policies. A backup passes only when:
  - the table set is the same as live;
  - the Alembic revision is the same as live;
  - every table's row count lies between the live counts taken just before and just after the dump.
  A failed backup is renamed `*.dump.FAILED` and the script exits non-zero. Every run is logged to `backups/backup.log`. The newest `KN_KEEP` (default 14) passed backups are kept.
- `deploy/restore.sh <backup>`: accepts only a file logged as OK; asks for the name to be typed again unless `KN_YES=1`; stops the services that write; drops and recreates the database; restores; starts the same services again; logs RESTORED.
- `release.sh` installs one cron line: nightly at 22:00 UTC (03:30 IST). It is idempotent, marked `# kisaniti-backup`, and `KN_CRON=0` skips it. `deploy.sh` now also copies backup.sh and restore.sh from the deployed commit.

## Tests (added to `deploy/prod_local_check.sh`; ALL PASS on the Mac 24 Nov, about 3 minutes; CI runs it in `prod-stack`)
1. Three backups after the smoke data: each passed its restore test, the log has 3 OK lines, 2 are kept with `KN_KEEP=2`, and the folder and file are private.
2. An FPO created after the newest backup exists. `restore.sh` refuses a file that is not a passed backup. After restoring the newest backup, the later FPO is gone, the earlier user is still there, and that user signs in over HTTPS through Caddy.

## Limits, stated
- The backups sit on the server's own disk. They protect against data mistakes and bad releases, not against losing the VM. An off-site copy (for example to the Mac or Azure Blob) is not built; it needs a decision on where.
- No mutation run proves that the row-count comparison catches a damaged dump; only `pg_restore --exit-on-error` failing on a broken file is relied on, by design of pg_restore.
- Not running on the server yet: the first real deploy waits for DNS (kisaniti.in still Pending at YesDomain).

## Rollback of this change
`git revert` of this commit; the cron line is removed with `crontab -e` (line marked `# kisaniti-backup`).

# Trial run of the production stack on the real server (24 Nov 2026)

**Why:** to measure whether the Standard_B1s VM (887 MB RAM, 2 GB swap, CHAT-54) can carry the production stack before the domain exists, and to run the release and backup scripts on the server itself. Fictional data only; nothing was left running.

## What ran
- Commit `d21584a`. Its images came from GHCR (anonymous pull) and its deploy files came from that commit, placed in a temporary `~/trial` folder as project `kn-trial`. Host names were `*.localhost`, so Caddy used its local CA. The cron line was skipped (`KN_CRON=0`).
- `release.sh`, twice:
  - the first run took 133 s including the image pull, and ended FAILED at the HTTPS check, as expected, because the local CA did not exist yet;
  - the second run, trusting that CA, ended `released d21584a…`.
- `scripts/smoke_access.sh https://app.localhost` with the fixture prices: **PASS in 32 s**.
- Page times through Caddy: `/en/sign-in` 0.21 s, `/en/status` 0.08 s, `/pa/status` 0.07 s.
- `backup.sh`: **backup OK**, 41 tables, 194 rows; the restore test passed on the server.

## Memory (after the smoke run)
| | used | available | swap used |
|---|---|---|---|
| server | 374 MB | 512 MB | 293 MB |

Per container: frontend 120 MB, backend 42 MB, PostgreSQL 44 MB, assessor 10 MB, Caddy 8 MB. Disk: 6.8 GB used, 55 GB free.

Conclusion: the stack fits, with about 500 MB available. The broker and kn-ingest (P3-2) are small. Swap is in use, so memory will be watched after the real deploy.

## Fault found and fixed
After cleanup, one anonymous Docker volume remained. `backup.sh` removed its throwaway restore-test database with `docker rm -f` but not its data volume. Every nightly backup would have left about 40 MB behind until the disk filled.
- Fix: `docker rm -f -v`.
- `deploy/prod_local_check.sh` now fails if the backups leave any volume behind (ALL PASS after the fix).
- The leftover volume on the server and 9 on the Mac were removed. All were checked first: PostgreSQL 17 data, created by these test runs, with no container.

## State after the trial
`docker compose down -v` was run, and `~/trial` was removed. On the server: 0 containers, 0 volumes, no cron lines. The pulled images remain cached, which speeds up the real deploy.

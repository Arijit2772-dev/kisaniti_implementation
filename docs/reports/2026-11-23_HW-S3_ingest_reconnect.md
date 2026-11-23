# HW-S3 — kn-ingest survives a PostgreSQL restart, 23 Nov 2026

**Problem (reproduced first).** With the hardware stack running, `docker compose restart db` made the next reading raise `psycopg.errors.AdminShutdown` out of the MQTT loop; `kn-ingest` stopped and, with no restart policy, stayed down. The new stack-check step failed on the old code with that traceback.

**Contract kept.** L3B v1: an ack only after durable storage; the device keeps and resends anything not acknowledged. No contract change.

**Change.**
- `store.py`: a lost connection (`psycopg.OperationalError`/`InterfaceError`) raises `StoreUnavailableError`; the dead connection is closed and never reused; the next operation opens a new one (`connect_timeout=5`). Nothing is retried inside the worker: the device's resend is the retry, and the `(device, boot, seq)` key turns a resend of an already committed reading into a DUPLICATE ack, not a second row. Other errors are not hidden. SQLite path unchanged.
- `registry.py`: if the device list cannot be read after one reconnect, the same outcome; a stale list is not used.
- `service.handle`: returns `STORE_UNAVAILABLE` with no ack instead of raising. `worker.py` logs it as a warning.
- `compose.yaml`: `restart: unless-stopped` on `ingest` as a backstop only.

**Excluded.** Readings lost to a power cut on the node (RAM buffer, stated limit); broker restarts (paho reconnects already, not re-tested here).

**Tests (run 23 Nov 2026).**
- `backend/ingestion/tests/test_store_outage.py`, 7 tests (no database): no ack on a cut, resend stored once, database down for several messages then recovery, commit-then-cut gives DUPLICATE, rejected message during an outage, unreadable registry, other errors stay visible. Ingestion suite: all pass; ruff and mypy clean.
- `backend/tests/test_ingest_reconnect.py` on real PostgreSQL: the server ends the store's connection twice in a 30-sample run; exactly 2 `STORE_UNAVAILABLE`, 30 `NEW`, no duplicate, buffer empty, a new backend PID. With the fix switched off (`lost_errors=()`) it fails with `AdminShutdown`, so the test can fail. Backend suite: 365 passed.
- `scripts/hw_stack_check.sh`, new step: real `docker compose restart db` under the running worker, then 12 readings: 12 ACCEPTED plus 1 NO_ACK (the reading in flight, resent), worker still running, `STORE_UNAVAILABLE` in its log, 24 readings on the website, all distinct. ALL PASS.
- `python3 scripts/chain.py`: PASS.

# HW-S1: broker and ingestion worker in the one-command stack

4 November 2026. Work item HW-S1 (planning repo `docs/BUILD_LEDGER.md` section 2b; parts and architecture CHAT-50). Closes O72.

## Scope and contract

Mosquitto and `kn-ingest` (`KN_REGISTRY=db`) run under a compose profile named `hardware`. No contract changed: L3B v1 and devices/v1 stay as frozen. Sensor data in this work is SYNTHETIC, on fictional FPOs.

Excluded: `secrets.h` and device-user provisioning (HW-S2), worker recovery after a database restart (HW-S3), preflight (HW-S4), the fresh-clone rehearsal (HW-S5), anything on the real board.

## Use

```bash
backend/ingestion/broker/setup_stack.sh <address the node uses>
docker compose --profile hardware up --build -d --wait
scripts/hw_stack_check.sh     # the check; also a CI step
```

## Changes

| File | Change |
|---|---|
| `compose.yaml` | services `broker` and `ingest` under the profile; volume `mqttdata`. Normal stack unchanged |
| `backend/ingestion/Dockerfile` | new image: the backend image has no MQTT client. Secrets reach it only by read-only mounts of the CA and the password file |
| `backend/ingestion/broker/setup_stack.sh` | new: certificates if absent, the `ingestion` broker user, git-ignored `ingestion.password`; keeps existing certificates; refuses a certificate that does not name the host or `broker` |
| `backend/ingestion/broker/gen_certs.sh` | the certificate also names `broker`, the service name the worker connects to. **Certificates made earlier fail `setup_stack.sh` and must be regenerated.** None had been flashed |
| `.dockerignore` | `backend/ingestion/broker` excluded |
| `worker.py` | logs the kind of store, not its address |
| `scripts/hw_stack_check.sh`, `.github/workflows/ci.yml` | the check, as a step of the stack job; logs and teardown include the profile |

## Two faults found and fixed

1. **Database password in the worker's log.** `worker.py` logged the full `KN_STORE` address, which holds the password. As a service, this would have reached `docker compose logs` and CI failure logs. Fixed; the check now searches the broker and worker logs for all three passwords.
2. **Broker secrets could enter the backend image.** The backend Dockerfile's `COPY backend` would have copied `certs/` (including the CA key), `passwd` and `ingestion.password` once they existed. Fixed by `.dockerignore`; confirmed in the rebuilt image, which has no `backend/ingestion/broker`.

## Evidence (local, on the running stack)

`scripts/hw_stack_check.sh`, final run:

```text
PASS: normal stack has its 4 services; the hardware profile adds only broker and ingest
PASS: broker is up and kn-ingest connected over TLS as its own broker user
PASS: host port 8883 gives TLSv1.3 and the certificate verifies against the CA
PASS: device kn-stack-179381191095887 registered and assigned through the website
PASS: the broker refuses a wrong password
PASS: software node: 12 readings over MQTT and TLS, 12 acknowledged after storing
PASS: the website returns all 12 readings, labelled SYNTHETIC
PASS: another FPO gets 404 for this device's readings
PASS: no password appears in the broker or worker logs
ALL PASS
```

Other checks: ingestion 49 tests pass, ruff and mypy clean; `scripts/chain.py` PASS with the profile running; `check_esp32_tls.sh 192.168.43.10` PASS on a newly generated certificate (BUG-009 still fixed with the added name).

Two earlier runs of the check failed because of the check itself: Python's strict default certificate mode, which the worker does not use (the check now verifies as the worker does), and a wrong field name in the reading view.

## Deviations and limits

- "Reaches the website" means the website's API proxy (`:3000/api/v1/...`). The Storage sensors page was not opened in a browser in this check.
- The software node runs inside the `ingest` container and reaches the broker by service name. The host's port 8883 is checked separately with a TLS connection. A real node on the hotspot is not tested (HW-C).
- `ingest` has no health check; the check script confirms the connection from its log. It has no restart policy and stops if PostgreSQL restarts (HW-S3).
- The broker's mount includes `certs/ca.key`, and `passwd`, `server.key` and `ingestion.password` are world-readable on this machine so that the containers' users can read them. Development setup only: revisit for hosting (P-2). Mosquitto warns that a later version will refuse a world-readable `passwd`; the image is pinned to 2.0.20.
- Adding a node's broker user is still by hand (`add_user.sh`, then `chmod` and a HUP to the broker) until HW-S2.

# P3-2 Storage broker on the public server (28 Nov 2026)

**Decisions used:** CHAT-54 (Azure VM, kisaniti.in, images by commit), FULL_PRODUCT_PLAN Phase 3 ("broker on `mqtt.<domain>:8883` with the same public certificate (the ESP32 checks it) … Node re-provisioned to the public broker"), contract L3B v1 (unchanged). Owner asked for it in chat ("Yes, start P3-2").

## Scope
- `deploy/compose.prod.yaml`: three services.
  - `brokercert` (caddy image, root): copies Caddy's certificate and key for `mqtt.<domain>` into the `mqtt_tls` volume as uid 1883, key mode 600; waits up to 180 s for the first certificate; re-checks hourly and counts copies in `/tls/serial`.
  - `broker` (eclipse-mosquitto 2.0.20): port 8883, TLS 1.2+, no anonymous access, per-device users from server-only `broker/auth/passwd`, topic rules from the same `acl` as the local broker. Network alias `mqtt.<domain>` inside the stack.
  - `ingest` (GHCR `kisaniti-ingest:<commit>`): registry from the database, connects to `mqtt.<domain>:8883` inside the stack so the certificate name matches; trusts the image's public roots (the local check points `KN_INGEST_CA` at Caddy's local CA).
- `deploy/release.sh`: creates the broker's `ingestion` user once (random password in `broker/ingestion.password`, never printed; `broker/` mode 700); after the website checks, requires kn-ingest to be subscribed, restarts the broker once and records the certificate copy it serves (`broker/served.serial`); installs the daily `broker_cert.sh` cron (21:30 UTC) next to the backup cron.
- `deploy/deploy.sh`: copies the broker files from the same commit (none for older commits, so a rollback to a commit without the broker still works).
- `deploy/broker_user.sh` (server): adds or replaces one device user from a `<id>:<mosquitto hash>` line on stdin; refuses plain text, bad IDs and the service users; reloads the broker with HUP.
- `deploy/broker_cert.sh` (server, daily): restarts the broker only when brokercert copied a renewed certificate after the broker started.
- `deploy/check_public_broker_tls.sh`: fetches the live chain from `<host>:<port>` and verifies it with mbedTLS 2.28 (the ESP32's release line) against `firmware/certs/public_broker_roots.pem` for that host name.
- `firmware/certs/public_broker_roots.pem`: ISRG Root X1 and X2 (public certificates), exported from macOS SystemRootCertificates, SHA-256 fingerprints checked.
- `scripts/provision_node.py --server user@host --host mqtt.<domain>`: runs the live chain check first, hashes the device password locally with mosquitto_passwd, sends only the hash line over SSH stdin to `broker_user.sh`, writes `secrets.h` with the two roots. `render_secrets_h` accepts a bundle of PEM certificates (comment lines dropped).

## Contract
No message, schema or firmware behaviour change. The node still checks the broker's certificate and host name (BUG-009 rule); only its trust anchors (from `secrets.h`) and host change.

## Explicit failure semantics
- No certificate from Caddy within 180 s: brokercert exits non-zero, the broker does not start, the release FAILS.
- kn-ingest not subscribed within 60 s of the website checks: release FAILED, logged, previous `KN_TAG` kept.
- `broker_user.sh`: anything other than one valid hash line for a contract-pattern device ID is refused; nothing changes.
- Provisioning: the live chain check runs before any user or file exists; on failure nothing is written.

## Tests
- `deploy/prod_local_check.sh`, 28 Nov on the Mac, **ALL PASS** (20 PASS lines). New: private broker folder and ingestion user; broker TLS on 8883 verified (chain and host name) with Caddy's local CA; kn-ingest subscribed by the public name; `broker_user.sh` adds a device from a hash and refuses plain text and the service user; a software node with its own user published 4 readings over TLS, all 4 stored, a wrong password refused; `broker_cert.sh` leaves the broker alone when unchanged and restarts it after a renewal, kn-ingest resubscribes; published ports are exactly caddy 80/443 and broker 8883. All earlier checks (smoke, backups, restore, rollback guard) still pass.
- `deploy/check_public_broker_tls.sh mqtt.kisaniti.in 443` against the live Let's Encrypt chain (leaf ← YE1 ← Root YE ← ISRG Root X2 ← ISRG Root X1, ECDSA): PASS. Negative: same chain for `app.kisaniti.in`: FAIL flags 0x4 (CN mismatch).
- `backend/tests/test_provision_node.py`: 56 pass (12 new: bundle round trip through the C compiler, malformed bundles refused, the roots file is the two ISRG roots, bad `--server` targets refused before anything runs). ruff and mypy clean.

## Exclusions
- The real node on the public broker (flash and acks) is done after deploy; it needs the owner's Wi-Fi password at the prompt.
- If Let's Encrypt stops serving the cross-signed chain to ISRG Root X1/X2, the node needs Root YE added to the bundle and a reflash; not handled automatically.
- `scripts/hw_preflight.sh` still checks the local broker only.
- Server memory with the three new services: measured at deploy.

## Rollback
`deploy/deploy.sh --previous` returns to `a20c469` (no broker); `git revert` of this commit removes the files.

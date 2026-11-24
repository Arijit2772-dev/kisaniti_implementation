# HW-S4 Hardware preflight (24 Nov 2026)

**Ledger item:** HW-S4. One command, `scripts/hw_preflight.sh --device-id <id>`, that checks everything a storage node depends on and changes nothing. It prints one PASS or FAIL per line, runs every check even after a FAIL, and exits 1 if any failed.

## What it checks
1. Services db, backend, broker and ingest are running, and healthy where they have a health check.
2. The secrets file the node was flashed with (`firmware/include/secrets.h` by default) is for this device and this host, and holds this broker's CA.
3. The node's host is an address of this machine **now** (a hotspot can hand out a new IP), and is not loopback for a real node. The fix is printed: rerun `setup_stack.sh` with the new address, provision, reflash.
4. TLS on 8883 verifies exactly as the node checks it (this CA, this host name), and the certificate has at least 14 days left.
5. The device's broker user exists.
6. kn-ingest subscribed and has not restarted.
7. The device is registered, active and assigned to a placement on the website.
8. The newest stored reading is at most `--max-age-min` old (default 10).

`--software-node` allows localhost and needs no secrets file (simulator on the same machine).

## Tests (added to `scripts/hw_stack_check.sh`, which CI runs; ALL PASS 24 Nov on the Mac)
- For the working software node: PREFLIGHT PASS.
- For a device that is not set up: exit 1, naming the missing broker user, the missing registration and no readings.
- For the same node without `--software-node`: exit 1, "localhost is this machine's loopback".
- A manual run for a device whose broker user had been removed reported exactly two FAILs: the secrets file was for another device, and no broker user existed. All other checks passed.

## Exclusions
- Run against a real ESP32: in Phase 2, when the node is built.
- The production broker (public certificate on mqtt.kisaniti.in) is P3-2. There the CA check becomes the public root.

## Rollback
`git revert` of this commit.

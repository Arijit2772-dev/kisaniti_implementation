# HW-S2 — node provisioning without hand-edited secrets, 23 Nov 2026

**Scope.** `scripts/provision_node.py` runs broker setup and the ESP32 certificate check, then makes the node's broker user with a generated password and writes the git-ignored `firmware/include/secrets.h` (owner-only, written atomically). The Wi-Fi password is typed at a terminal prompt (twice), never an argument; the broker password is never printed. `scripts/hw_stack_check.sh` now provisions its test node through the same script.

**Contract.** Device ID must match the frozen pattern `[a-z0-9][a-z0-9-]{2,31}` and may not be a service user; host, SSID (1–32 bytes, no control characters) and WPA2 password (8–63 printable or 64 hex) are checked; an output path inside the public repo must be git-ignored; nothing for the node is written if the certificate check fails; an existing file needs `--force` (new password, node must be reflashed). Registration on the website stays a separate step.

**Excluded.** Issuing credentials from the website (O39, H-4b); the public-host certificate (Phase 4).

**Tests (run 23 Nov 2026).**
- `backend/tests/test_provision_node.py`: 44 pass (C escaping read back by the compiler, refusals, no password in output); ruff and mypy clean.
- `scripts/hw_stack_check.sh`: ALL PASS (10 checks: provisioning, TLS 1.3, registration, wrong password refused, 12/12 readings acknowledged, another FPO gets 404, no password in logs).
- Firmware `esp32dev_dht11` builds with a generated `secrets.h` (scratch copy; the existing local `secrets.h` was not touched).

**Note.** The script was written in an earlier chat and left uncommitted; reviewed here, three long lines fixed, no behaviour change.

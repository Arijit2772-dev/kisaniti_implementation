#!/bin/sh
# P3-2: add or replace one device's broker user on the server, then make the running broker read it.
# Reads ONE line "<device-id>:<mosquitto password hash>" on stdin, made on the developer's machine by
# scripts/provision_node.py --server, so the plain password never leaves that machine. Never prints the hash.
# Usage (in ~/kisaniti): sh broker_user.sh < line
set -eu
cd "${KN_DIR:-$HOME/kisaniti}"
fail() { echo "FAIL: $*" >&2; exit 1; }
read -r LINE || fail "no input"
ID="${LINE%%:*}"
echo "$ID" | grep -Eq '^[a-z0-9][a-z0-9-]{2,31}$' || fail "device ID does not match the contract pattern"
case "$ID" in ingestion|conformance) fail "$ID is a service user, not a device" ;; esac
echo "${LINE#*:}" | grep -Eq '^\$7\$[0-9]+\$[A-Za-z0-9+/=]+\$[A-Za-z0-9+/=]+$' || fail "not a mosquitto password hash"
[ -f broker/auth/passwd ] || fail "no broker/auth/passwd here; deploy a release with the broker first"
( umask 022; { grep -v "^$ID:" broker/auth/passwd || true; echo "$LINE"; } > broker/auth/passwd.new )
chmod 644 broker/auth/passwd.new && mv broker/auth/passwd.new broker/auth/passwd
docker compose kill -s HUP broker >/dev/null
echo "broker user $ID ready (running broker reloaded)"

#!/bin/sh
# P3-2: keep the broker's TLS volume (/tls) holding Caddy's current certificate for the broker's host name, readable
# by the broker's user (uid 1883) only. Runs in the caddy image as root with caddy_data read-only at /data.
# First copy: waits up to 180 s for Caddy's first certificate, else exits non-zero (compose restarts it). Then checks
# every hour and copies a renewed certificate, counting copies in /tls/serial; deploy/broker_cert.sh (daily cron)
# restarts the broker when that count differs from the one the broker started with.
# Caddy's local test CA root is copied too when it exists (local checks only).
set -eu
H="${KN_MQTT_HOST:?}"
find_crt() { find /data/caddy/certificates -path "*/$H/$H.crt" 2>/dev/null | head -1; }
copy() {
  CRT="$(find_crt)"; [ -n "$CRT" ] && [ -f "${CRT%.crt}.key" ] || return 1
  if [ -f /tls/fullchain.pem ] && cmp -s "$CRT" /tls/fullchain.pem; then return 0; fi
  cp "$CRT" /tls/fullchain.pem.new && cp "${CRT%.crt}.key" /tls/key.pem.new
  chown 1883:1883 /tls/fullchain.pem.new /tls/key.pem.new
  chmod 644 /tls/fullchain.pem.new && chmod 600 /tls/key.pem.new
  mv /tls/key.pem.new /tls/key.pem && mv /tls/fullchain.pem.new /tls/fullchain.pem
  N=$(( $(cat /tls/serial 2>/dev/null || echo 0) + 1 )); echo "$N" > /tls/serial
  echo "$(date -u +%FT%TZ) copied the certificate for $H (copy $N)"
}
i=0
until copy; do
  i=$((i + 1)); [ "$i" -le 90 ] || { echo "FAIL: no certificate for $H from Caddy after 180 s" >&2; exit 1; }
  sleep 2
done
LOCAL=/data/caddy/pki/authorities/local/root.crt
[ -f "$LOCAL" ] && cp "$LOCAL" /tls/local-root.crt && chmod 644 /tls/local-root.crt
while sleep 3600; do copy || echo "$(date -u +%FT%TZ) WARN: certificate for $H not found; keeping the current copy"; done

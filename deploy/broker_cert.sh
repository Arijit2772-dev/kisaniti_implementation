#!/bin/sh
# P3-2, daily from cron: brokercert counts its certificate copies in /tls/serial; broker/served.serial holds the count
# the running broker started with (written by release.sh and here). When they differ, a renewed certificate was copied
# after the broker started: restart the broker so it serves it. Nodes reconnect and resend what they buffered (L3B v1).
set -eu
cd "${KN_DIR:-$HOME/kisaniti}"
NOW="$(docker compose exec -T brokercert cat /tls/serial)"
SERVED="$(cat broker/served.serial 2>/dev/null || echo none)"
if [ "$NOW" != "$SERVED" ]; then
  docker compose restart broker >/dev/null
  echo "$NOW" > broker/served.serial
  echo "$(date -u +%FT%TZ) certificate copy $NOW (broker had $SERVED): broker restarted"
else
  echo "$(date -u +%FT%TZ) certificate copy $NOW unchanged"
fi

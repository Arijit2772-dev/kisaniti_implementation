#!/bin/sh
# Whole-stack check of login and FPO isolation against the running compose stack (docker compose up -d --wait).
# Creates two fictional FPOs with the operator command, signs in over HTTP, and fails on any deviation.
# Usage: scripts/smoke_access.sh [api-url]     (default http://localhost:8000)
set -eu
API="${1:-http://localhost:8000}"
J="$(mktemp -d)"; trap 'rm -rf "$J"' EXIT
SUF="$(date +%s)$$"
PW="smoke test passphrase $SUF"
fail() { echo "FAIL: $*" >&2; exit 1; }
field() { python3 -c "import json,sys;print(json.load(sys.stdin)$1)"; }
mkfpo() {  # label -> prints "username temp"
  docker compose exec -T backend python -m kisaniti_api.access.cli create-fpo --name "Smoke FPO $1 (fictional) $SUF" \
    --admin-username "smoke.$1.$SUF" --synthetic | python3 -c "import json,sys;d=json.load(sys.stdin);print(d['username'],d['temporary_password'])"
}
signin() {  # label username password -> cookie jar + csrf file
  curl -fsS -c "$J/$1.jar" -H 'Content-Type: application/json' -d "{\"username\":\"$2\",\"password\":\"$3\"}" "$API/api/v1/auth/login" \
    | field "['csrf_token']" > "$J/$1.csrf"
}
call() {  # label method path [json] -> body on stdout, status in $J/status
  l="$1"; m="$2"; p="$3"; d="${4:-}"
  if [ -n "$d" ]; then
    curl -sS -o "$J/body" -w '%{http_code}' -X "$m" -b "$J/$l.jar" -H "X-CSRF-Token: $(cat "$J/$l.csrf")" -H 'Content-Type: application/json' -d "$d" "$API$p" > "$J/status"
  else
    curl -sS -o "$J/body" -w '%{http_code}' -X "$m" -b "$J/$l.jar" -H "X-CSRF-Token: $(cat "$J/$l.csrf")" "$API$p" > "$J/status"
  fi
  cat "$J/body"
}
status() { cat "$J/status"; }

set -- $(mkfpo a); UA="$1"; TA="$2"
set -- $(mkfpo b); UB="$1"; TB="$2"
signin a "$UA" "$TA"; signin b "$UB" "$TB"
call a GET /api/v1/users >/dev/null; [ "$(status)" = 403 ] || fail "must-change gate did not block (got $(status))"
call a POST /api/v1/auth/change-password "{\"current_password\":\"$TA\",\"new_password\":\"$PW\"}" >/dev/null; [ "$(status)" = 204 ] || fail "change password $(status)"
call b POST /api/v1/auth/change-password "{\"current_password\":\"$TB\",\"new_password\":\"$PW\"}" >/dev/null; [ "$(status)" = 204 ] || fail "change password b $(status)"
SID="$(call a POST /api/v1/users "{\"username\":\"smoke.staff.$SUF\",\"display_name\":\"Staff (fictional)\",\"role\":\"STAFF\"}" | field "['user']['id']")"
[ "$(status)" = 201 ] || fail "create user $(status)"
call a GET /api/v1/users | grep -q "smoke.staff.$SUF" || fail "A does not see its own user"
call b GET /api/v1/users | grep -q "smoke.staff.$SUF" && fail "B sees A's user in its list"
call b GET "/api/v1/users/$SID" >/dev/null; [ "$(status)" = 404 ] || fail "B reading A's user answered $(status), expected 404"
call b POST "/api/v1/users/$SID/reset-password" >/dev/null; [ "$(status)" = 404 ] || fail "B resetting A's user answered $(status)"
DID="smoke-$SUF"
call a POST /api/v1/devices "{\"device_id\":\"$DID\",\"source_origin\":\"SIMULATOR\"}" >/dev/null; [ "$(status)" = 201 ] || fail "register device $(status)"
call a GET /api/v1/devices | grep -q "$DID" || fail "A does not see its own device"
call b GET /api/v1/devices | grep -q "$DID" && fail "B sees A's device in its list"
call b GET "/api/v1/devices/$DID" >/dev/null; [ "$(status)" = 404 ] || fail "B reading A's device answered $(status), expected 404"
call b GET "/api/v1/devices/$DID/readings" >/dev/null; [ "$(status)" = 404 ] || fail "B reading A's readings answered $(status)"
call b POST /api/v1/devices "{\"device_id\":\"$DID\",\"source_origin\":\"SIMULATOR\"}" >/dev/null; [ "$(status)" = 409 ] || fail "B registering the same device id answered $(status)"
call a GET /api/v1/auth/me | grep -q '"fpo_id"' || fail "me"
curl -sS -o /dev/null -w '%{http_code}' -X POST -b "$J/a.jar" "$API/api/v1/auth/logout" | grep -q 403 || fail "logout without CSRF token was not refused"
call a POST /api/v1/auth/logout >/dev/null; [ "$(status)" = 204 ] || fail "logout $(status)"
call a GET /api/v1/auth/me >/dev/null; [ "$(status)" = 401 ] || fail "session still valid after logout ($(status))"
CODES=""; for i in 1 2 3 4 5 6; do CODES="$CODES$(curl -sS -o /dev/null -w '%{http_code} ' -H 'Content-Type: application/json' -d "{\"username\":\"ghost.$SUF\",\"password\":\"wrong wrong wrong\"}" "$API/api/v1/auth/login")"; done
[ "$CODES" = "401 401 401 401 401 429 " ] || fail "throttle sequence was: $CODES"
echo "PASS: login, must-change gate, CSRF, logout, two-FPO isolation (users and devices: list, read, reset, readings), throttle"

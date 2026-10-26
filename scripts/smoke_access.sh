#!/bin/sh
# Whole-stack check of login and FPO isolation against the running compose stack (docker compose up -d --wait).
# Creates two fictional FPOs with the operator command, signs in over HTTP, and fails on any deviation.
# Usage: scripts/smoke_access.sh [api-url]     (default http://localhost:8000)
# SMOKE_SEED_MARKET=1 first imports a tiny fixture price release (invented numbers) so a commodity exists; CI sets it.
# Never set it on a stack that holds real prices.
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

if [ "${SMOKE_SEED_MARKET:-}" = 1 ]; then
  D="$(mktemp -d)"; python3 "$(dirname "$0")/make_ci_release.py" "$D/release"
  docker compose cp "$D/release" backend:/tmp/ci_release >/dev/null
  docker compose exec -T backend python -m kisaniti_api.market.import_release import-release --dir /tmp/ci_release >/dev/null
  rm -rf "$D"
fi

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
# ---- members, lots, the stock ledger (org/v1) and market reference data (market/v1)
CMD="$(call a GET /api/v1/commodities | field "[0]['id']" 2>/dev/null)" || fail "no commodity: import a price release (SMOKE_SEED_MARKET=1)"
MID="$(call a POST /api/v1/members '{"display_name":"Smoke member (fictional)","preferred_language":"pa"}' | field "['id']")"; [ "$(status)" = 201 ] || fail "create member $(status)"
call b GET /api/v1/members | grep -q "$MID" && fail "B sees A's member"
call b GET "/api/v1/members/$MID" >/dev/null; [ "$(status)" = 404 ] || fail "B reading A's member answered $(status), expected 404"
LOT_BODY="{\"member_id\":\"$MID\",\"commodity_id\":\"$CMD\",\"source_variety\":\"1121\",\"registered_quantity_kg\":\"1250.000\"}"
idem() {  # label key method path json -> body; status in $J/status
  curl -sS -o "$J/body" -w '%{http_code}' -X "$3" -b "$J/$1.jar" -H "X-CSRF-Token: $(cat "$J/$1.csrf")" -H "Idempotency-Key: $2" \
    -H 'Content-Type: application/json' -d "$5" "$API$4" > "$J/status"; cat "$J/body"
}
LID="$(idem a "smoke-$SUF-lot" POST /api/v1/lots "$LOT_BODY" | field "['id']")"; [ "$(status)" = 201 ] || fail "register lot $(status)"
[ "$(idem a "smoke-$SUF-lot" POST /api/v1/lots "$LOT_BODY" | field "['id']")" = "$LID" ] || fail "an idempotent replay made a second lot"
idem a "smoke-$SUF-lot" POST /api/v1/lots "{\"member_id\":\"$MID\",\"commodity_id\":\"$CMD\",\"registered_quantity_kg\":\"9.000\"}" >/dev/null
[ "$(status)" = 409 ] || fail "the same key with a different body answered $(status), expected 409"
call a GET /api/v1/lots | grep -q '"on_hand_kg":"1250.000"' || fail "A's lot does not show 1250.000 kg on hand"
call b GET /api/v1/lots | grep -q "$LID" && fail "B sees A's lot"
call b GET "/api/v1/lots/$LID" >/dev/null; [ "$(status)" = 404 ] || fail "B reading A's lot answered $(status), expected 404"
call b POST "/api/v1/lots/$LID/adjustments" '{"quantity_delta_kg":"-1.000","reason_code":"OTHER"}' >/dev/null; [ "$(status)" = 404 ] || fail "B adjusting A's lot answered $(status)"
call b POST /api/v1/lots "$LOT_BODY" >/dev/null; [ "$(status)" = 404 ] || fail "B registering a lot for A's member answered $(status)"
call a POST "/api/v1/lots/$LID/adjustments" '{"quantity_delta_kg":"-2000.000","reason_code":"OTHER"}' >/dev/null; [ "$(status)" = 409 ] || fail "an overdraw answered $(status), expected 409"
call a POST "/api/v1/lots/$LID/adjustments" '{"quantity_delta_kg":"-50.000","reason_code":"SPILLAGE"}' >/dev/null; [ "$(status)" = 201 ] || fail "adjustment $(status)"
call a GET "/api/v1/lots/$LID" | grep -q '"on_hand_kg":"1200.000"' || fail "on hand is not 1200.000 kg after the adjustment"
call a GET "/api/v1/lots/$LID/events" | grep -q '"ADJUSTED"' || fail "the ledger does not list the adjustment"
# ---- forecasts, sensor links and a plan proposal (plan/v1)
call a GET "/api/v1/forecasts?variety=1121" | grep -q '"signal_verdict":"NOT_ASSESSED"' || fail "forecast set missing or not labelled"
MND="$(call a GET "/api/v1/forecasts?variety=1121" | python3 -c "import json,sys;print(next(c['mandi_id'] for c in json.load(sys.stdin)['candidate_mandis'] if c['status']=='ELIGIBLE'))")"
PLAN_BODY="{\"variety\":\"1121\",\"horizons_days\":[1,7],\"mandis\":[{\"mandi_id\":\"$MND\",\"transport_inr_per_quintal\":\"10.00\"}],\"increment_kg\":\"100.000\"}"
PID="$(idem a "smoke-$SUF-plan" POST /api/v1/plans "$PLAN_BODY" | field "['id']")"; [ "$(status)" = 201 ] || fail "create plan $(status)"
call a GET "/api/v1/plans/$PID" | grep -q '"status":"PROPOSED"' || fail "the plan was not proposed"
[ "$(idem a "smoke-$SUF-plan" POST /api/v1/plans "$PLAN_BODY" | field "['id']")" = "$PID" ] || fail "an idempotent replay made a second plan"
call a GET /api/v1/plans | grep -q "$PID" || fail "A does not list its plan"
call b GET /api/v1/plans | grep -q "$PID" && fail "B lists A's plan"
call b GET "/api/v1/plans/$PID" >/dev/null; [ "$(status)" = 404 ] || fail "B reading A's plan answered $(status), expected 404"
call b POST /api/v1/plans "$PLAN_BODY" >/dev/null; [ "$(status)" = 422 ] || fail "B planning without stock answered $(status), expected 422"
call a GET "/api/v1/lots/$LID" | grep -q '"reserved_kg":"0.000"' || fail "a proposal reserved stock"
# ---- approval (approval/v1): decision and reservation are one step; another FPO cannot touch the plan
call a GET "/api/v1/plans/$PID/state" | grep -q '"status":"PROPOSED"' || fail "plan state is not PROPOSED"
ACKS="$(call a GET "/api/v1/plans/$PID/approval-context" | field "['required_acknowledgements']" | tr "'" '"')"
call a POST "/api/v1/plans/$PID/approve" '{"revision_no":1,"acknowledged_warnings":[]}' >/dev/null; [ "$(status)" = 422 ] || fail "approval without acknowledgements answered $(status), expected 422"
call b GET "/api/v1/plans/$PID/state" >/dev/null; [ "$(status)" = 404 ] || fail "B reading A's plan state answered $(status), expected 404"
call b POST "/api/v1/plans/$PID/approve" "{\"revision_no\":1,\"acknowledged_warnings\":$ACKS}" >/dev/null; [ "$(status)" = 404 ] || fail "B approving A's plan answered $(status), expected 404"
APPROVE_BODY="{\"revision_no\":1,\"acknowledged_warnings\":$ACKS}"
idem a "smoke-$SUF-approve" POST "/api/v1/plans/$PID/approve" "$APPROVE_BODY" | grep -q '"decision":"APPROVED"' || fail "approval failed ($(status))"
[ "$(status)" = 201 ] || fail "approve $(status)"
idem a "smoke-$SUF-approve" POST "/api/v1/plans/$PID/approve" "$APPROVE_BODY" >/dev/null; [ "$(status)" = 201 ] || fail "approval replay answered $(status)"
call a POST "/api/v1/plans/$PID/approve" "$APPROVE_BODY" >/dev/null; [ "$(status)" = 409 ] || fail "a second approval answered $(status), expected 409"
call a GET "/api/v1/lots/$LID" | grep -q '"reserved_kg":"1200.000"' || fail "the approval did not reserve 1200.000 kg exactly once"
call a GET "/api/v1/plans/$PID/state" | grep -q '"state":"ACTIVE"' || fail "reservation is not ACTIVE"
# ---- messaging (messaging/v1): consent, frozen text, preview that sends nothing, another FPO sees none of it
call a GET /api/v1/contacts | grep -q "$MID" || fail "A's contacts list lacks its member"
call b GET /api/v1/contacts | grep -q "$MID" && fail "B sees A's member among its contacts"
call b POST "/api/v1/members/$MID/contact" '{"phone":"9000000009"}' >/dev/null; [ "$(status)" = 404 ] || fail "B setting A's member's phone answered $(status), expected 404"
call a POST "/api/v1/members/$MID/contact" '{"phone":"not a number"}' >/dev/null; [ "$(status)" = 422 ] || fail "an invalid phone answered $(status), expected 422"
call a POST "/api/v1/plans/$PID/instructions" '{"kind":"SALE_INSTRUCTION"}' | grep -q '"NO_CONTACT"' || fail "a farmer without a contact was not blocked"
call a POST "/api/v1/members/$MID/contact" '{"phone":"90000 00001"}' >/dev/null; [ "$(status)" = 201 ] || fail "set phone $(status)"
call a POST "/api/v1/members/$MID/consents" '{"kind":"WHATSAPP","action":"GRANTED","notice_version":"NOTICE-DEMO-0.1","method":"STAFF_ASSISTED_ORAL"}' >/dev/null; [ "$(status)" = 201 ] || fail "grant consent $(status)"
INS="$(call a POST "/api/v1/plans/$PID/instructions" '{"kind":"SALE_INSTRUCTION"}' | field "[0]['id']")"; [ "$(status)" = 201 ] || fail "prepare $(status)"
call a GET "/api/v1/instructions/$INS" | grep -q '"status":"PREPARED"' || fail "the instruction was not prepared once contact and consent existed"
call b GET "/api/v1/instructions/$INS" >/dev/null; [ "$(status)" = 404 ] || fail "B reading A's instruction answered $(status), expected 404"
call b POST "/api/v1/instructions/$INS/send" '{"channel":"PREVIEW"}' >/dev/null; [ "$(status)" = 404 ] || fail "B previewing A's instruction answered $(status)"
call a POST "/api/v1/instructions/$INS/send" '{"channel":"WHATSAPP"}' >/dev/null; [ "$(status)" = 422 ] || fail "the WhatsApp channel answered $(status), expected 422 (not available)"
PREV="$(call a POST "/api/v1/instructions/$INS/send" '{"channel":"PREVIEW"}')"
echo "$PREV" | grep -q '"status":"PREVIEWED"' || fail "preview failed ($(status))"
echo "$PREV" | grep -q 'nothing was sent' || fail "the preview is not labelled"
echo "$PREV" | grep -q '9000000001' && fail "a preview showed the full phone number"
call a POST "/api/v1/members/$MID/consents" '{"kind":"WHATSAPP","action":"WITHDRAWN","notice_version":"NOTICE-DEMO-0.1","method":"STAFF_ASSISTED_ORAL"}' >/dev/null; [ "$(status)" = 201 ] || fail "withdraw consent $(status)"
call a POST "/api/v1/instructions/$INS/send" '{"channel":"PREVIEW"}' | grep -q '"CONSENT_WITHDRAWN"' || fail "a send after withdrawal was not blocked"
call a GET /api/v1/instruction-queue | grep -q "$INS" || fail "the blocked send is not in the queue"
# ---- sales (sales/v1): a sale that follows the plan uses up its reservation; reality never edits the plan
LINE="$(call a GET "/api/v1/plans/$PID/execution" | python3 -c "import json,sys;l=json.load(sys.stdin)['lines'][0];print(json.dumps({'plan_id':'$PID','mandi_id':l['mandi_id'],'date':l['date']}))")"
SALE_BODY="{\"lot_id\":\"$LID\",\"plan_ref\":$LINE,\"mandi_id\":\"$MND\",\"sale_date\":\"2025-11-10\",\"quantity_kg\":\"100.000\",\"price_inr_per_quintal\":\"4400.00\",\"j_form_no\":\"J/SMOKE/$SUF\"}"
SID="$(idem a "smoke-$SUF-sale" POST /api/v1/sales "$SALE_BODY" | field "['id']")"; [ "$(status)" = 201 ] || fail "record sale $(status)"
idem a "smoke-$SUF-sale" POST /api/v1/sales "$SALE_BODY" >/dev/null; [ "$(status)" = 201 ] || fail "a sale replay answered $(status)"
call a GET "/api/v1/sales/$SID" | grep -q '"amount_inr":"4400.00"' || fail "the amount of the sale is wrong"
call a POST /api/v1/sales "$SALE_BODY" >/dev/null; [ "$(status)" = 409 ] || fail "a repeated J-form number answered $(status), expected 409"
call a GET "/api/v1/lots/$LID" | grep -q '"reserved_kg":"1100.000"' || fail "the sale did not consume exactly 100.000 kg of the reservation"
call a GET "/api/v1/plans/$PID/execution" | grep -q '"status":"PARTIAL"' || fail "execution is not PARTIAL after a part-sale"
call a GET "/api/v1/plans/$PID/report" | grep -q 'separate records' || fail "the report is missing or unlabelled"
call a GET /api/v1/forecast-outcomes | grep -q 'not evidence' || fail "outcomes are missing or unlabelled"
call a POST /api/v1/sales "{\"lot_id\":\"$LID\",\"plan_ref\":null,\"mandi_id\":\"$MND\",\"sale_date\":\"2025-11-10\",\"quantity_kg\":\"10.000\",\"price_inr_per_quintal\":\"4300.00\",\"j_form_no\":\"J/OFF/$SUF\"}" >/dev/null; [ "$(status)" = 409 ] || fail "an off-plan sale beyond the free stock answered $(status), expected 409 (reconciliation required)"
call b GET "/api/v1/sales/$SID" >/dev/null; [ "$(status)" = 404 ] || fail "B reading A's sale answered $(status), expected 404"
call b POST /api/v1/sales "$SALE_BODY" >/dev/null; [ "$(status)" = 404 ] || fail "B selling A's lot answered $(status), expected 404"
call b GET "/api/v1/plans/$PID/execution" >/dev/null; [ "$(status)" = 404 ] || fail "B reading A's execution answered $(status)"
call b GET /api/v1/sales | grep -q "$SID" && fail "B lists A's sale"
call a POST "/api/v1/instructions/$INS/replies" '{"text":"smoke reply"}' >/dev/null; [ "$(status)" = 201 ] || fail "store a reply $(status)"
call b GET "/api/v1/instructions/$INS/replies" >/dev/null; [ "$(status)" = 404 ] || fail "B reading A's replies answered $(status)"
call a GET "/api/v1/members/$MID/consents" | grep -q '"WITHDRAWN"' || fail "the consent state changed unexpectedly"
call b POST "/api/v1/plans/$PID/release" '{"reason_code":"OTHER"}' >/dev/null; [ "$(status)" = 404 ] || fail "B releasing A's plan answered $(status)"
call a POST "/api/v1/plans/$PID/release" '{"reason_code":"PLAN_CANCELLED"}' | grep -q '"state":"RELEASED"' || fail "release failed ($(status))"
call a GET "/api/v1/lots/$LID" | grep -q '"reserved_kg":"0.000"' || fail "the release did not free the stock"
call b POST "/api/v1/lots/$LID/sensor-links" '{"device_id":null}' >/dev/null; [ "$(status)" = 404 ] || fail "B linking A's lot answered $(status), expected 404"
call a POST "/api/v1/lots/$LID/sensor-links" "{\"device_id\":\"$DID\"}" >/dev/null; [ "$(status)" = 201 ] || fail "link sensor $(status)"
call a GET /api/v1/sensor-links/current | grep -q "$DID" || fail "current link missing"
for path in /api/v1/prices/summary /api/v1/msp /api/v1/datasets /api/v1/mandis "/api/v1/prices?limit=5"; do
  call b GET "$path" >/dev/null; [ "$(status)" = 200 ] || fail "market reference data $path answered $(status)"
done
call a GET /api/v1/auth/me | grep -q '"fpo_id"' || fail "me"
curl -sS -o /dev/null -w '%{http_code}' -X POST -b "$J/a.jar" "$API/api/v1/auth/logout" | grep -q 403 || fail "logout without CSRF token was not refused"
call a POST /api/v1/auth/logout >/dev/null; [ "$(status)" = 204 ] || fail "logout $(status)"
call a GET /api/v1/auth/me >/dev/null; [ "$(status)" = 401 ] || fail "session still valid after logout ($(status))"
CODES=""; for i in 1 2 3 4 5 6; do CODES="$CODES$(curl -sS -o /dev/null -w '%{http_code} ' -H 'Content-Type: application/json' -d "{\"username\":\"ghost.$SUF\",\"password\":\"wrong wrong wrong\"}" "$API/api/v1/auth/login")"; done
[ "$CODES" = "401 401 401 401 401 429 " ] || fail "throttle sequence was: $CODES"
echo "PASS: login, must-change gate, CSRF, logout, two-FPO isolation (users, devices, members, lots, ledger), idempotency, stock refusal, market reads, forecast, plan proposal, approval and reservation, consent and message preview, sale that consumes the reservation, reply, release, sensor link, throttle"

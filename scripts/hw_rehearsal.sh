#!/bin/sh
# HW-S5 full rehearsal: a fresh clone of main from GitHub, a fresh database, the software node, every check in order.
# Gate 1 of FULL_PRODUCT_PLAN: chain, smoke, browser tests and the hardware checks all pass from nothing.
# Usage: scripts/hw_rehearsal.sh [commit]     (default: origin/main). Needs ports 3000, 8000, 5433 and 8883 free,
# so stop the normal stack first (docker compose --profile hardware stop). Fictional data only. Prints PASS or FAIL
# per step with its time, writes rehearsal_<commit>.txt in the current folder, and removes the clone and its data.
set -eu
REPO=https://github.com/Arijit2772-dev/kisaniti_implementation.git
OUT="$(pwd)"
T="$(mktemp -d)"
export COMPOSE_PROJECT_NAME=kn-rehearsal   # own containers and volumes: nothing from an existing stack is reused
cleanup() { (cd "$T/repo" 2>/dev/null && docker compose --profile hardware down -v >/dev/null 2>&1) || true; rm -rf "$T"; }
trap cleanup EXIT
LOG="$T/log.txt"
fail() { echo "FAIL: $*" | tee -a "$LOG" >&2; cp "$LOG" "$OUT/rehearsal_failed.txt"; exit 1; }
step() {  # name, command...: runs it, records PASS with seconds or FAIL with the tail of its output
  name="$1"; shift; s=$(date +%s)
  if "$@" > "$T/$name.out" 2>&1; then echo "PASS: $name ($(( $(date +%s) - s )) s)" | tee -a "$LOG"
  else tail -30 "$T/$name.out" >> "$LOG"; fail "$name, see the output above"; fi
}
for p in 3000 8000 5433 8883; do
  if lsof -nP -iTCP:$p -sTCP:LISTEN >/dev/null 2>&1; then fail "port $p is in use; stop the running stack first"; fi
done

git clone -q "$REPO" "$T/repo"
cd "$T/repo"
git checkout -q "${1:-origin/main}"
SHA="$(git rev-parse HEAD)"
echo "rehearsal of $SHA from a fresh clone, $(date -u +%Y-%m-%dT%H:%M:%SZ)" | tee "$LOG"
python3 -m venv "$T/venv"
step tools sh -c "'$T/venv/bin/pip' install -q jsonschema==4.25.1 'psycopg[binary]==3.2.10'"
export PATH="$T/venv/bin:$PATH"

step broker-setup sh backend/ingestion/broker/setup_stack.sh localhost
step stack-up docker compose --profile hardware up --build -d --wait --wait-timeout 600
step health sh -c 'curl -fsS http://localhost:8000/health/ready | grep -q "\"status\":\"READY\""'
step chain python3 scripts/chain.py --report "$T/chain_report.json"
step chain-sabotage-fails sh -c "! python3 scripts/chain.py --sabotage ledger --report '$T/sab.json' && python3 -c \"import json,sys;sys.exit(json.load(open('$T/sab.json'))['failure']['step']!='ledger-audit')\""
step smoke-api env SMOKE_SEED_MARKET=1 sh scripts/smoke_access.sh
step smoke-website sh scripts/smoke_access.sh http://localhost:3000
step software-node-mqtt sh scripts/hw_stack_check.sh
step browser-tests sh -c "cd frontend && npm ci --silent && npx playwright test"
echo "REHEARSAL PASS for $SHA" | tee -a "$LOG"
cp "$LOG" "$OUT/rehearsal_$(echo "$SHA" | cut -c1-7).txt"

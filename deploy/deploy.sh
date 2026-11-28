#!/bin/sh
# Deploys one exact commit of main to the server, or rolls back (CHAT-54). Run on the developer's machine.
# Usage: deploy/deploy.sh <commit>       deploy that commit (its CI run must have passed and published images)
#        deploy/deploy.sh --previous     roll back to the last commit released OK before the current one
# KN_SERVER (default kisaniti@172.128.33.73) and KN_SSH_KEY (default ~/.ssh/kisaniti_azure) choose the server.
# The compose file, Caddyfile and the release, backup and restore scripts are taken from that same commit, so a rollback also rolls back config.
# First deploy only: KN_DOMAIN=kisaniti.in deploy/deploy.sh <commit> creates the server's .env.
set -eu
cd "$(dirname "$0")/.."
SERVER="${KN_SERVER:-kisaniti@172.128.33.73}"
KEY="${KN_SSH_KEY:-$HOME/.ssh/kisaniti_azure}"
DIR=kisaniti
fail() { echo "FAIL: $*" >&2; exit 1; }
remote() { ssh -i "$KEY" -o BatchMode=yes "$SERVER" "$@"; }
[ $# = 1 ] || fail "usage: deploy/deploy.sh <commit> | --previous"

if [ "$1" = --previous ]; then
  # the newest OK release, then the newest OK release of a different commit before it
  SHA="$(remote "cat $DIR/deploys.log 2>/dev/null" | awk '$3=="OK"{s[++n]=$2} END{for(i=n-1;i>0;i--) if(s[i]!=s[n]){print s[i]; exit}}')"
  [ -n "$SHA" ] || fail "no earlier OK release in the server's deploys.log"
  echo "rolling back to $SHA"
else
  git fetch -q origin main
  SHA="$(git rev-parse --verify "$1^{commit}")" || fail "unknown commit $1"
  git merge-base --is-ancestor "$SHA" origin/main || fail "$SHA is not on origin/main; only main has published images"
  CI="$(gh run list --commit "$SHA" --workflow CI --json conclusion --jq '.[0].conclusion' 2>/dev/null || true)"
  [ "$CI" = success ] || fail "CI for $SHA is '$CI', not success; its images may not exist"
fi

T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
for f in compose.prod.yaml Caddyfile release.sh backup.sh restore.sh; do
  git show "$SHA:deploy/$f" > "$T/$f" 2>/dev/null || fail "commit $SHA has no deploy/$f (older than the deploy files)"
done
mv "$T/compose.prod.yaml" "$T/compose.yaml"
# P3-2 broker files, from the same commit; older commits have no broker and copy none
EXTRA=""
for f in deploy/broker_user.sh deploy/broker_cert.sh; do
  git show "$SHA:$f" > "$T/$(basename "$f")" 2>/dev/null && EXTRA="$EXTRA $T/$(basename "$f")"
done
mkdir -p "$T/broker"
for f in deploy/broker/mosquitto.conf deploy/broker/brokercert.sh backend/ingestion/broker/acl; do
  git show "$SHA:$f" > "$T/broker/$(basename "$f")" 2>/dev/null || rm -f "$T/broker/$(basename "$f")"
done
remote "mkdir -p $DIR/broker && chmod 700 $DIR/broker"
# shellcheck disable=SC2086
scp -q -i "$KEY" -o BatchMode=yes "$T/compose.yaml" "$T/Caddyfile" "$T/release.sh" "$T/backup.sh" "$T/restore.sh" $EXTRA "$SERVER:$DIR/"
if [ -n "$(ls "$T/broker")" ]; then scp -q -i "$KEY" -o BatchMode=yes "$T"/broker/* "$SERVER:$DIR/broker/"; fi
remote "cd $DIR && KN_DOMAIN='${KN_DOMAIN:-}' sh release.sh $SHA"

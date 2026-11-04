#!/bin/sh
# Prepare what the compose "hardware" profile needs: broker certificates and the ingestion worker's broker user.
# Safe to run again: existing certificates are kept (a node may already hold this CA) and the password stays the same.
# Usage: ./setup_stack.sh 192.168.43.10     (the address the node will use; "localhost" when no node is involved)
# Output, all git-ignored: certs/, passwd, ingestion.password
set -e
HOST="${1:?give the broker IP address or DNS name the node will use}"
cd "$(dirname "$0")"
if [ ! -f certs/server.crt ]; then
  ./gen_certs.sh "$HOST"
fi
NAMES="$(openssl x509 -in certs/server.crt -noout -text)"
for n in broker "$HOST"; do
  case "$NAMES" in
    *"DNS:$n"*) ;;
    *) echo "FAIL: certs/server.crt does not name $n. Run ./gen_certs.sh $HOST, then give the node the new CA." >&2; exit 1 ;;
  esac
done
touch passwd
if [ ! -s ingestion.password ]; then
  ( umask 077; openssl rand -hex 24 > ingestion.password )
fi
docker run --rm -u "$(id -u):$(id -g)" -v "$PWD":/mosquitto/config eclipse-mosquitto:2.0.20 \
  mosquitto_passwd -b /mosquitto/config/passwd ingestion "$(cat ingestion.password)"
# read through read-only mounts by the containers' own users; these stay out of git, and this is a development setup
chmod 644 passwd ingestion.password
echo "broker ready for $HOST: certs/, passwd, ingestion.password"

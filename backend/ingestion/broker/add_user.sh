#!/bin/sh
# Add or update a broker user (a device ID, or "ingestion"). Prompts for the password.
# Usage: ./add_user.sh kn-node-001
set -e
cd "$(dirname "$0")"
touch passwd
docker run --rm -it -v "$PWD":/mosquitto/config eclipse-mosquitto:2.0.20 \
  mosquitto_passwd /mosquitto/config/passwd "${1:?user name}"

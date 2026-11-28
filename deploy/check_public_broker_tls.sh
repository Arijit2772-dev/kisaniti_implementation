#!/bin/sh
# P3-2 / BUG-009 style check before flashing: would the ESP32 accept the certificate chain that <host>:<port> serves,
# trusting only firmware/certs/public_broker_roots.pem? Fetches the live chain with openssl, then verifies it with
# mbedTLS 2.28 (Debian bookworm, the release line of the ESP32's Arduino 2.0.17 mbedTLS 2.28.7) for <host>.
# Usage: deploy/check_public_broker_tls.sh mqtt.kisaniti.in [8883]     Needs Docker and openssl. Prints PASS or FAIL.
set -eu
HOST="${1:?give the broker host name exactly as written in KN_MQTT_HOST}"; PORT="${2:-8883}"
HERE="$(cd "$(dirname "$0")/.." && pwd)"
W="$(mktemp -d)"; trap 'rm -rf "$W"' EXIT
echo | openssl s_client -connect "$HOST:$PORT" -servername "$HOST" -showcerts 2>/dev/null \
  | awk '/BEGIN CERTIFICATE/,/END CERTIFICATE/' > "$W/chain.pem"
[ -s "$W/chain.pem" ] || { echo "FAIL: no certificate chain from $HOST:$PORT"; exit 1; }
grep -v '^#' "$HERE/firmware/certs/public_broker_roots.pem" > "$W/roots.pem"
cat > "$W/v.c" <<'C'
#include <stdio.h>
#include <mbedtls/x509_crt.h>
int main(int argc, char **argv) {
  mbedtls_x509_crt ca, chain; uint32_t flags = 0;
  mbedtls_x509_crt_init(&ca); mbedtls_x509_crt_init(&chain);
  if (mbedtls_x509_crt_parse_file(&ca, argv[1]) || mbedtls_x509_crt_parse_file(&chain, argv[2])) { puts("FAIL: cannot read certificates"); return 1; }
  int ret = mbedtls_x509_crt_verify(&chain, &ca, NULL, argv[3], &flags, NULL, NULL);
  if (ret == 0) { printf("PASS: the ESP32 will accept the chain served for %s\n", argv[3]); return 0; }
  char buf[512]; mbedtls_x509_crt_verify_info(buf, sizeof buf, "  ", flags);
  printf("FAIL: flags=0x%x\n%s", flags, buf); return 1;
}
C
docker run --rm -v "$W":/w debian:bookworm-slim sh -c \
  "apt-get update -qq >/dev/null && apt-get install -y -qq gcc libmbedtls-dev >/dev/null 2>&1 && gcc /w/v.c -o /w/v -lmbedx509 -lmbedcrypto && /w/v /w/roots.pem /w/chain.pem '$HOST'"

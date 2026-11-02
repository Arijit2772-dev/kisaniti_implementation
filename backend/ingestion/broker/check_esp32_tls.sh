#!/bin/sh
# Check that the broker certificate will be accepted by the ESP32 before flashing (BUG-009).
# Verifies certs/server.crt against certs/ca.crt for the host the node will use (KN_MQTT_HOST in secrets.h) with
# mbedTLS 2.28 from Debian bookworm, the same release line as the ESP32's Arduino 2.0.17 (mbedTLS 2.28.7).
# Usage: ./check_esp32_tls.sh 192.168.43.10        Needs Docker. Prints PASS or FAIL.
set -e
HOST="${1:?give the host exactly as written in KN_MQTT_HOST}"
cd "$(dirname "$0")/certs"
cat > /tmp/kn_tls_verify.c <<'EOF'
#include <stdio.h>
#include <mbedtls/x509_crt.h>
int main(int argc, char **argv) {
  mbedtls_x509_crt ca, crt;
  mbedtls_x509_crt_init(&ca);
  mbedtls_x509_crt_init(&crt);
  if (mbedtls_x509_crt_parse_file(&ca, argv[1]) || mbedtls_x509_crt_parse_file(&crt, argv[2])) { puts("FAIL: cannot read certificates"); return 1; }
  uint32_t flags = 0;
  int ret = mbedtls_x509_crt_verify(&crt, &ca, NULL, argv[3], &flags, NULL, NULL);
  if (ret == 0) { printf("PASS: the ESP32 will accept this certificate for %s\n", argv[3]); return 0; }
  printf("FAIL: flags=0x%x%s\n", flags, (flags & MBEDTLS_X509_BADCERT_CN_MISMATCH) ? " (host name does not match; rerun gen_certs.sh with this host)" : "");
  return 1;
}
EOF
docker run --rm -v "$PWD":/c:ro -v /tmp/kn_tls_verify.c:/v.c:ro debian:bookworm-slim sh -c \
  "apt-get update -qq >/dev/null && apt-get install -y -qq gcc libmbedtls-dev >/dev/null 2>&1 && gcc /v.c -o /v -lmbedx509 -lmbedcrypto && /v /c/ca.crt /c/server.crt '$HOST'"

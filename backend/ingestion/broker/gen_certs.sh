#!/bin/sh
# Create a private CA and a broker certificate for the laptop/server address the ESP32 will use.
# Usage: ./gen_certs.sh 192.168.43.10        (IP of the machine running Mosquitto on the hotspot)
#        ./gen_certs.sh broker.example.org   (or a DNS name)
# Output: certs/ca.crt (copy into the firmware's secrets.h), certs/server.crt, certs/server.key
set -e
HOST="${1:?give the broker IP address or DNS name}"
cd "$(dirname "$0")"
mkdir -p certs && cd certs
case "$HOST" in
  *[!0-9.]*) SAN="DNS:$HOST" ;;
  *) SAN="IP:$HOST" ;;
esac
openssl req -x509 -newkey rsa:2048 -nodes -days 825 -keyout ca.key -out ca.crt -subj "/CN=KisanNiti dev CA"
openssl req -newkey rsa:2048 -nodes -keyout server.key -out server.csr -subj "/CN=$HOST"
printf "subjectAltName=%s,DNS:localhost,IP:127.0.0.1\nextendedKeyUsage=serverAuth\n" "$SAN" > san.ext
openssl x509 -req -in server.csr -CA ca.crt -CAkey ca.key -CAcreateserial -days 825 -out server.crt -extfile san.ext
rm -f server.csr san.ext ca.srl
chmod 644 server.key   # read by the mosquitto user inside the container; keep this folder private
echo "certs written to $(pwd); SAN = $SAN"

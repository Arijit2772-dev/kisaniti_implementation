// Copy to include/secrets.h (git-ignored) and fill in. Never commit real values.
#pragma once

#define KN_WIFI_SSID "your-hotspot-name"
#define KN_WIFI_PASS "your-hotspot-password"

#define KN_MQTT_HOST "192.168.43.10"  // laptop or server running Mosquitto (see BUILD_GUIDE.md)
#define KN_MQTT_PORT 8883
#define KN_MQTT_USER "kn-node-001"
#define KN_MQTT_PASS "per-device-password"

#define KN_DEVICE_ID "kn-node-001"

// PEM of the CA that signed the broker certificate (backend/ingestion/broker/certs/ca.crt).
// The broker certificate must name KN_MQTT_HOST (IP or DNS name).
#define KN_MQTT_CA_PEM \
  "-----BEGIN CERTIFICATE-----\n" \
  "REPLACE_WITH_YOUR_CA\n" \
  "-----END CERTIFICATE-----\n"

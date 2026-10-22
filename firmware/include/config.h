// Build-time configuration. Two profiles share all code:
//   real node (default)  - secrets.h, MQTT over TLS, 5-minute samples, application ack
//   KN_SIMULATION=1      - Wokwi twin: Wokwi-GUEST Wi-Fi, public test broker, 10-second samples
#pragma once

#ifndef KN_FIRMWARE_VERSION
#define KN_FIRMWARE_VERSION "kn-fw-1.0.0"
#endif

#define KN_SENSOR_MODEL "SHT30"
#define KN_I2C_SDA 21
#define KN_I2C_SCL 22
#define KN_LED_PIN 2

#define KN_BUFFER_SIZE 288           // 24 h at 5 minutes (contract section 2)
#define KN_ACK_TIMEOUT_MS 60000UL    // resend if no application ack within 60 s
#define KN_MAX_SENDS_PER_LOOP 10     // keep loop() responsive while draining a backlog
#define KN_UNSYNCED_HOLD_MS 600000UL // hold samples up to 10 min waiting for NTP before sending them untimed
#define KN_NTP_SERVER_1 "pool.ntp.org"
#define KN_NTP_SERVER_2 "time.google.com"

#if KN_SIMULATION
// ---- Wokwi digital twin (no secrets, no personal data) ----
#define KN_WIFI_SSID "Wokwi-GUEST"
#define KN_WIFI_PASS ""
#define KN_WIFI_CHANNEL 6            // Wokwi's virtual access point is on channel 6
#define KN_MQTT_HOST "test.mosquitto.org"
#define KN_MQTT_PORT 1883
#define KN_MQTT_TLS 0
#define KN_MQTT_USER ""
#define KN_MQTT_PASS ""
#ifndef KN_DEVICE_ID
#define KN_DEVICE_ID "kn-twin-001"
#endif
#define KN_SOURCE_ORIGIN "WOKWI_TWIN"
#define KN_SAMPLE_INTERVAL_S 10      // shortened for the simulation; real nodes use 300
#ifndef KN_APP_ACK
#define KN_APP_ACK 0                 // stand-alone twin: broker PUBACK; set 1 when backend/ingestion runs
#endif

#else
// ---- Real node ----
#if __has_include("secrets.h")
#include "secrets.h"
#else
#warning "include/secrets.h not found: building with placeholder values from secrets.example.h"
#include "secrets.example.h"
#endif
#define KN_WIFI_CHANNEL 0
#define KN_MQTT_TLS 1
#define KN_SOURCE_ORIGIN "DEVICE"
#define KN_SAMPLE_INTERVAL_S 300     // 5 minutes (HARDWARE_CURRENT)
#ifndef KN_APP_ACK
#define KN_APP_ACK 1
#endif
#endif

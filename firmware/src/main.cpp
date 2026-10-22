// KisanNiti storage node: SHT30 probe in the grain -> ESP32 -> MQTT.
// Publishes DeviceTelemetryV1 (kisaniti/docs/contracts/L3b_SENSOR_STORAGE_CONTRACT_v1.md).
// The same source builds the real node and the Wokwi twin (see include/config.h).
#include <Arduino.h>
#include <WiFi.h>
#include <Wire.h>
#include <MQTT.h>
#include <esp_random.h>
#include <esp_timer.h>
#include <sys/time.h>
#include <time.h>

#include "config.h"
#include "sht3x_codec.h"
#include "sample_buffer.h"
#include "telemetry_json.h"

#if KN_MQTT_TLS
#include <WiFiClientSecure.h>
static WiFiClientSecure net;
#else
static WiFiClient net;
#endif

static MQTTClient mqtt(1024);
static kn::SampleBuffer<KN_BUFFER_SIZE> buffer;
static kn::DeviceInfo device;

static char topic_telemetry[96];
static char topic_ack[96];
static char topic_status[96];
static char client_id[48];

static uint32_t next_seq = 0;
static uint64_t next_sample_ms = 0;
static uint64_t last_wifi_attempt_ms = 0;
static uint64_t last_mqtt_attempt_ms = 0;
static uint64_t mqtt_backoff_ms = 2000;
static uint64_t last_health_ms = 0;
static bool clock_synced = false;
static uint32_t published_total = 0;
static uint32_t acked_total = 0;

static uint64_t uptime_ms() { return (uint64_t)(esp_timer_get_time() / 1000); }

static int64_t epoch_ms_now() {
  struct timeval tv;
  gettimeofday(&tv, nullptr);
  return (int64_t)tv.tv_sec * 1000 + tv.tv_usec / 1000;
}

// The clock counts as synced once SNTP has moved it past 2024-01-01.
static bool clock_is_valid() { return time(nullptr) > 1704067200; }

// ---------- SHT30 (single shot, high repeatability, no clock stretching) ----------

static bool sht3x_command(uint16_t cmd) {
  Wire.beginTransmission(kn::SHT3X_ADDR_DEFAULT);
  Wire.write((uint8_t)(cmd >> 8));
  Wire.write((uint8_t)(cmd & 0xFF));
  return Wire.endTransmission() == 0;
}

static kn::ReadStatus sht3x_measure_once(float *t, float *rh) {
  if (!sht3x_command(kn::SHT3X_CMD_SINGLE_HIGH_NOSTRETCH)) return kn::ReadStatus::NO_ACK;
  delay(kn::SHT3X_MEAS_HIGH_MAX_MS);
  // Without clock stretching the sensor NACKs the read header until data is ready.
  for (int attempt = 0; attempt < 3; attempt++) {
    if (Wire.requestFrom((uint8_t)kn::SHT3X_ADDR_DEFAULT, (uint8_t)6) == 6) {
      uint8_t frame[6];
      for (int i = 0; i < 6; i++) frame[i] = Wire.read();
      return kn::sht3x_decode(frame, t, rh);
    }
    delay(5);
  }
  return kn::ReadStatus::TIMEOUT;
}

static kn::ReadStatus sht3x_measure(float *t, float *rh) {
  kn::ReadStatus st = sht3x_measure_once(t, rh);
  if (st != kn::ReadStatus::OK) st = sht3x_measure_once(t, rh);  // one retry
  return st;
}

// ---------- sampling ----------

static void take_sample() {
  kn::Sample s{};
  s.seq = next_seq++;
  s.uptime_ms = uptime_ms();
  float t = 0, rh = 0;
  s.status = sht3x_measure(&t, &rh);
  s.temperature_c = kn::round2(t);
  s.humidity_pct = kn::round2(rh);
  if (clock_synced) {
    s.epoch_ms = epoch_ms_now();
    s.time_quality = kn::TimeQuality::NTP_SYNCED;
  } else {
    s.epoch_ms = 0;
    s.time_quality = kn::TimeQuality::UNSYNCED;
  }
  s.state = kn::SendState::PENDING;
  bool dropped = buffer.push(s);
  if (s.status == kn::ReadStatus::OK) {
    Serial.printf("[kn] sample seq=%lu T=%.2fC RH=%.2f%% time=%s buffered=%u%s\n",
                  (unsigned long)s.seq, s.temperature_c, s.humidity_pct,
                  kn::time_quality_name(s.time_quality), (unsigned)buffer.size(),
                  dropped ? " (oldest dropped: buffer full)" : "");
  } else {
    Serial.printf("[kn] sample seq=%lu read_status=%s buffered=%u\n", (unsigned long)s.seq,
                  kn::read_status_name(s.status), (unsigned)buffer.size());
  }
}

// ---------- MQTT ----------

static void on_message(String &topic, String &payload) {
  if (topic != topic_ack) return;
  uint32_t seq;
  if (kn::parse_ack(payload.c_str(), payload.length(), device.boot_id, &seq)) {
    if (buffer.remove_seq(seq)) acked_total++;
    Serial.printf("[kn] ack seq=%lu buffered=%u\n", (unsigned long)seq, (unsigned)buffer.size());
  }
}

static void publish_status(bool online) {
  char payload[128], at[21];
  if (clock_synced) {
    kn::format_utc(epoch_ms_now(), at);
    snprintf(payload, sizeof payload, "{\"state\":\"%s\",\"boot_id\":\"%s\",\"at\":\"%s\"}",
             online ? "online" : "offline", device.boot_id, at);
  } else {
    snprintf(payload, sizeof payload, "{\"state\":\"%s\",\"boot_id\":\"%s\",\"at\":null}",
             online ? "online" : "offline", device.boot_id);
  }
  mqtt.publish(topic_status, payload, strlen(payload), true, 1);
}

static void maintain_wifi() {
  if (WiFi.status() == WL_CONNECTED) return;
  uint64_t now = uptime_ms();
  if (last_wifi_attempt_ms != 0 && now - last_wifi_attempt_ms < 30000) return;
  last_wifi_attempt_ms = now;
  Serial.printf("[kn] wifi connecting to %s\n", KN_WIFI_SSID);
  WiFi.disconnect();
  if (KN_WIFI_CHANNEL) {
    WiFi.begin(KN_WIFI_SSID, KN_WIFI_PASS, KN_WIFI_CHANNEL);
  } else {
    WiFi.begin(KN_WIFI_SSID, KN_WIFI_PASS);
  }
}

static void maintain_clock() {
  if (clock_synced || !clock_is_valid()) return;
  clock_synced = true;
  size_t n = buffer.backfill_time(epoch_ms_now(), uptime_ms());
  char at[21];
  kn::format_utc(epoch_ms_now(), at);
  Serial.printf("[kn] clock synced %s; back-filled %u earlier samples\n", at, (unsigned)n);
}

static void maintain_mqtt() {
  if (WiFi.status() != WL_CONNECTED || mqtt.connected()) return;
  if (KN_MQTT_TLS && !clock_synced) return;  // certificate dates need a valid clock
  uint64_t now = uptime_ms();
  if (last_mqtt_attempt_ms != 0 && now - last_mqtt_attempt_ms < mqtt_backoff_ms) return;
  last_mqtt_attempt_ms = now;
  char will[96];
  snprintf(will, sizeof will, "{\"state\":\"offline\",\"boot_id\":\"%s\",\"at\":null}", device.boot_id);
  mqtt.setWill(topic_status, will, true, 1);
  Serial.printf("[kn] mqtt connecting to %s:%d as %s\n", KN_MQTT_HOST, KN_MQTT_PORT, client_id);
  bool ok = strlen(KN_MQTT_USER) > 0 ? mqtt.connect(client_id, KN_MQTT_USER, KN_MQTT_PASS)
                                     : mqtt.connect(client_id);
  if (!ok) {
    Serial.printf("[kn] mqtt connect failed (lastError=%d returnCode=%d), retry in %lus\n",
                  (int)mqtt.lastError(), (int)mqtt.returnCode(), (unsigned long)(mqtt_backoff_ms / 1000));
    mqtt_backoff_ms = mqtt_backoff_ms * 2 > 60000 ? 60000 : mqtt_backoff_ms * 2;
    return;
  }
  mqtt_backoff_ms = 2000;
  mqtt.subscribe(topic_ack, 1);
  publish_status(true);
  Serial.println("[kn] mqtt connected");
}

static void send_buffered() {
  if (!mqtt.connected()) return;
  uint64_t now = uptime_ms();
  if (KN_APP_ACK) buffer.expire_waiting(now, KN_ACK_TIMEOUT_MS);
  char payload[640];
  int sent = 0;
  size_t i = 0;
  while (i < buffer.size() && sent < KN_MAX_SENDS_PER_LOOP) {
    kn::Sample &s = buffer.at(i);
    bool untimed_too_young = s.time_quality == kn::TimeQuality::UNSYNCED && now - s.uptime_ms < KN_UNSYNCED_HOLD_MS;
    if (s.state != kn::SendState::PENDING || untimed_too_young) {
      i++;
      continue;
    }
    size_t len = kn::build_telemetry(s, device, buffer.dropped(), payload, sizeof payload);
    if (len == 0 || !mqtt.publish(topic_telemetry, payload, (int)len, false, 1)) {
      Serial.printf("[kn] publish failed seq=%lu; will retry\n", (unsigned long)s.seq);
      return;
    }
    published_total++;
    sent++;
    digitalWrite(KN_LED_PIN, HIGH);
    Serial.printf("TX %s %s\n", topic_telemetry, payload);
    if (KN_APP_ACK) {
      s.state = kn::SendState::AWAIT_ACK;
      s.sent_at_ms = now;
      s.send_count++;
      i++;
    } else {
      buffer.remove_seq(s.seq);  // broker PUBACK only (stand-alone twin)
    }
  }
  digitalWrite(KN_LED_PIN, LOW);
}

static void log_health() {
  uint64_t now = uptime_ms();
  if (now - last_health_ms < 60000) return;
  last_health_ms = now;
  Serial.printf("[kn] health wifi=%d mqtt=%d clock=%d buffered=%u dropped=%lu published=%lu acked=%lu\n",
                WiFi.status() == WL_CONNECTED, mqtt.connected(), clock_synced, (unsigned)buffer.size(),
                (unsigned long)buffer.dropped(), (unsigned long)published_total, (unsigned long)acked_total);
}

void setup() {
  Serial.begin(115200);
  delay(200);
  pinMode(KN_LED_PIN, OUTPUT);

  snprintf(device.boot_id, sizeof device.boot_id, "%08lx", (unsigned long)esp_random());
  device.device_id = KN_DEVICE_ID;
  device.sensor_model = KN_SENSOR_MODEL;
  device.firmware_version = KN_FIRMWARE_VERSION;
  device.source_origin = KN_SOURCE_ORIGIN;
  device.sample_interval_s = KN_SAMPLE_INTERVAL_S;
  snprintf(topic_telemetry, sizeof topic_telemetry, "kisaniti/v1/devices/%s/telemetry", KN_DEVICE_ID);
  snprintf(topic_ack, sizeof topic_ack, "kisaniti/v1/devices/%s/ack", KN_DEVICE_ID);
  snprintf(topic_status, sizeof topic_status, "kisaniti/v1/devices/%s/status", KN_DEVICE_ID);
  snprintf(client_id, sizeof client_id, "%s-%s", KN_DEVICE_ID, device.boot_id);

  Serial.printf("[kn] KisanNiti node %s firmware %s origin %s boot_id %s interval %lus app_ack %d\n",
                KN_DEVICE_ID, KN_FIRMWARE_VERSION, KN_SOURCE_ORIGIN, device.boot_id,
                (unsigned long)KN_SAMPLE_INTERVAL_S, KN_APP_ACK);

  Wire.begin(KN_I2C_SDA, KN_I2C_SCL);
  if (sht3x_command(kn::SHT3X_CMD_SOFT_RESET)) {
    delay(kn::SHT3X_SOFT_RESET_MAX_MS);
    Serial.println("[kn] SHT30 found at 0x44");
  } else {
    Serial.println("[kn] SHT30 not answering at 0x44: check wiring; readings will report NO_ACK");
  }

  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(true);
  configTime(0, 0, KN_NTP_SERVER_1, KN_NTP_SERVER_2);
#if KN_MQTT_TLS
  net.setCACert(KN_MQTT_CA_PEM);
#endif
  mqtt.begin(KN_MQTT_HOST, KN_MQTT_PORT, net);
  mqtt.setKeepAlive(30);
  mqtt.setCleanSession(true);
  mqtt.setTimeout(5000);
  mqtt.onMessage(on_message);

  next_sample_ms = uptime_ms();
}

void loop() {
  uint64_t now = uptime_ms();
  if (now >= next_sample_ms) {
    take_sample();
    next_sample_ms += (uint64_t)KN_SAMPLE_INTERVAL_S * 1000;
    if (next_sample_ms < now) next_sample_ms = now + (uint64_t)KN_SAMPLE_INTERVAL_S * 1000;
  }
  maintain_wifi();
  maintain_clock();
  maintain_mqtt();
  mqtt.loop();
  send_buffered();
  log_health();
  delay(10);
}

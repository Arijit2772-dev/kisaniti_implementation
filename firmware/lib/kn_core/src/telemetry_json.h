// DeviceTelemetryV1 payload and DeviceAckV1 parsing (contract section 2).
#pragma once
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <time.h>
#include <ArduinoJson.h>
#include "sample_buffer.h"

namespace kn {

struct DeviceInfo {
  const char *device_id;
  char boot_id[9];  // 8 lowercase hex + NUL
  const char *sensor_model;
  const char *firmware_version;
  const char *source_origin;  // DEVICE | WOKWI_TWIN | SIMULATOR
  uint32_t sample_interval_s;
};

// Shared-contract instant (CONTRACT_SPEC_V1_shared §4): UTC, exactly milliseconds,
// e.g. 2026-10-22T10:05:00.000Z (24 characters + NUL).
constexpr size_t INSTANT_LEN = 25;

inline void format_utc(int64_t epoch_ms, char out[INSTANT_LEN]) {
  time_t secs = (time_t)(epoch_ms / 1000);
  struct tm tm_utc;
  gmtime_r(&secs, &tm_utc);
  size_t n = strftime(out, INSTANT_LEN, "%Y-%m-%dT%H:%M:%S", &tm_utc);
  snprintf(out + n, INSTANT_LEN - n, ".%03dZ", (int)(epoch_ms % 1000));
}

// Writes the JSON payload into out. Returns its length, or 0 if it did not fit.
inline size_t build_telemetry(const Sample &s, const DeviceInfo &dev, uint32_t dropped_since_boot,
                              char *out, size_t cap) {
  JsonDocument doc;
  char measured[INSTANT_LEN], t_buf[12], rh_buf[12];
  doc["schema"] = "kisaniti.device_telemetry.v1";
  doc["device_id"] = dev.device_id;
  doc["boot_id"] = dev.boot_id;
  doc["seq"] = s.seq;
  if (s.time_quality == TimeQuality::UNSYNCED) {
    doc["measured_at"] = nullptr;
  } else {
    format_utc(s.epoch_ms, measured);
    doc["measured_at"] = (const char *)measured;
  }
  doc["time_quality"] = time_quality_name(s.time_quality);
  doc["uptime_ms"] = s.uptime_ms;
  if (s.status == ReadStatus::OK) {
    snprintf(t_buf, sizeof t_buf, "%.2f", (double)s.temperature_c);
    snprintf(rh_buf, sizeof rh_buf, "%.2f", (double)s.humidity_pct);
    doc["temperature_c"] = serialized((const char *)t_buf);
    doc["humidity_pct"] = serialized((const char *)rh_buf);
  } else {
    doc["temperature_c"] = nullptr;  // never a false zero
    doc["humidity_pct"] = nullptr;
  }
  doc["read_status"] = read_status_name(s.status);
  doc["sensor_model"] = dev.sensor_model;
  doc["firmware_version"] = dev.firmware_version;
  doc["source_origin"] = dev.source_origin;
  doc["sample_interval_s"] = dev.sample_interval_s;
  doc["resent"] = s.send_count > 0;
  doc["dropped_since_boot"] = dropped_since_boot;
  if (measureJson(doc) + 1 > cap) return 0;
  return serializeJson(doc, out, cap);
}

// Parses an ack. Returns true and sets *seq only if the schema matches and the
// ack is for this boot; status is ACCEPTED, DUPLICATE or QUARANTINED.
inline bool parse_ack(const char *payload, size_t len, const char *boot_id, uint32_t *seq) {
  JsonDocument doc;
  if (deserializeJson(doc, payload, len)) return false;
  const char *schema = doc["schema"];
  const char *ack_boot = doc["boot_id"];
  const char *status = doc["status"];
  if (!schema || strcmp(schema, "kisaniti.device_ack.v1") != 0) return false;
  if (!ack_boot || strcmp(ack_boot, boot_id) != 0) return false;
  if (!status || !(strcmp(status, "ACCEPTED") == 0 || strcmp(status, "DUPLICATE") == 0 ||
                   strcmp(status, "QUARANTINED") == 0)) {
    return false;
  }
  if (!doc["seq"].is<uint32_t>()) return false;
  *seq = doc["seq"].as<uint32_t>();
  return true;
}

}  // namespace kn

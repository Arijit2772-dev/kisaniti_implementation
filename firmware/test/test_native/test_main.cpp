// Host tests for the pure firmware parts: pio test -e native
#include <unity.h>
#include <string.h>
#include "sht3x_codec.h"
#include "dht11_codec.h"
#include "dht22_codec.h"
#include "sample_buffer.h"
#include "telemetry_json.h"

using namespace kn;

void setUp() {}
void tearDown() {}

static void test_crc_datasheet_example() {
  const uint8_t beef[2] = {0xBE, 0xEF};
  TEST_ASSERT_EQUAL_HEX8(0x92, sht3x_crc8(beef, 2));  // SHT3x datasheet Table 20
}

static void test_decode_ok_and_conversion() {
  // raw T = 0x6666 (26215) -> 25.00 C ; raw RH = 0x8000 (32768) -> 50.00 %
  uint8_t f[6] = {0x66, 0x66, 0, 0x80, 0x00, 0};
  f[2] = sht3x_crc8(f, 2);
  f[5] = sht3x_crc8(f + 3, 2);
  float t, rh;
  TEST_ASSERT_EQUAL(ReadStatus::OK, sht3x_decode(f, &t, &rh));
  TEST_ASSERT_FLOAT_WITHIN(0.01, 25.00, t);
  TEST_ASSERT_FLOAT_WITHIN(0.01, 50.00, rh);
}

static void test_decode_extremes() {
  TEST_ASSERT_FLOAT_WITHIN(1e-4, -45.0, sht3x_temperature_c(0));
  TEST_ASSERT_FLOAT_WITHIN(1e-4, 130.0, sht3x_temperature_c(0xFFFF));
  TEST_ASSERT_FLOAT_WITHIN(1e-4, 100.0, sht3x_humidity_pct(0xFFFF));
}

static void test_decode_bad_crc() {
  uint8_t f[6] = {0x66, 0x66, 0x00, 0x80, 0x00, 0x00};
  float t = -1, rh = -1;
  TEST_ASSERT_EQUAL(ReadStatus::CRC_ERROR, sht3x_decode(f, &t, &rh));
}

static Sample mk(uint32_t seq, uint64_t up) {
  Sample s{};
  s.seq = seq;
  s.uptime_ms = up;
  s.time_quality = TimeQuality::UNSYNCED;
  s.status = ReadStatus::OK;
  s.temperature_c = 24.5f;
  s.humidity_pct = 61.27f;
  return s;
}

static void test_buffer_overflow_drops_oldest() {
  SampleBuffer<3> b;
  for (uint32_t i = 0; i < 5; i++) b.push(mk(i, i * 1000));
  TEST_ASSERT_EQUAL(3, b.size());
  TEST_ASSERT_EQUAL(2, b.dropped());
  TEST_ASSERT_EQUAL(2, b.at(0).seq);
  TEST_ASSERT_EQUAL(4, b.at(2).seq);
}

static void test_buffer_out_of_order_ack() {
  SampleBuffer<5> b;
  for (uint32_t i = 0; i < 4; i++) b.push(mk(i, i));
  TEST_ASSERT_TRUE(b.remove_seq(2));
  TEST_ASSERT_FALSE(b.remove_seq(2));  // duplicate ack is harmless
  TEST_ASSERT_TRUE(b.remove_seq(0));
  TEST_ASSERT_EQUAL(2, b.size());
  TEST_ASSERT_EQUAL(1, b.at(0).seq);
  TEST_ASSERT_EQUAL(3, b.at(1).seq);
}

static void test_backfill_from_uptime() {
  SampleBuffer<4> b;
  b.push(mk(0, 10000));
  b.push(mk(1, 310000));
  // clock syncs at uptime 400 s = 2026-10-22T10:00:00Z
  int64_t now = 1792663200000LL;
  TEST_ASSERT_EQUAL(2, b.backfill_time(now, 400000));
  TEST_ASSERT_EQUAL(TimeQuality::UPTIME_BACKFILLED, b.at(0).time_quality);
  TEST_ASSERT_EQUAL_INT64(now - 390000, b.at(0).epoch_ms);
  TEST_ASSERT_EQUAL_INT64(now - 90000, b.at(1).epoch_ms);
}

static void test_ack_timeout_returns_to_pending() {
  SampleBuffer<2> b;
  Sample s = mk(0, 0);
  s.state = SendState::AWAIT_ACK;
  s.sent_at_ms = 1000;
  b.push(s);
  TEST_ASSERT_EQUAL(0, b.expire_waiting(30000, 60000));
  TEST_ASSERT_EQUAL(1, b.expire_waiting(61000, 60000));
  TEST_ASSERT_EQUAL(SendState::PENDING, b.at(0).state);
}

static DeviceInfo dev() {
  DeviceInfo d{};
  d.device_id = "kn-node-001";
  strcpy(d.boot_id, "3fa9c1e2");
  d.sensor_model = "SHT30";
  d.firmware_version = "kn-fw-1.0.0";
  d.source_origin = "DEVICE";
  d.sample_interval_s = 300;
  return d;
}

static void test_payload_ok() {
  Sample s = mk(42, 12603112);
  s.time_quality = TimeQuality::NTP_SYNCED;
  s.epoch_ms = 1792663500000LL;  // 2026-10-22T10:05:00Z
  char out[640];
  size_t n = build_telemetry(s, dev(), 0, out, sizeof out);
  TEST_ASSERT_TRUE(n > 0);
  TEST_ASSERT_NOT_NULL(strstr(out, "\"measured_at\":\"2026-10-22T10:05:00.000Z\""));
  TEST_ASSERT_NOT_NULL(strstr(out, "\"temperature_c\":24.50"));
  TEST_ASSERT_NOT_NULL(strstr(out, "\"humidity_pct\":61.27"));
  TEST_ASSERT_NOT_NULL(strstr(out, "\"resent\":false"));
}

static void test_instant_has_milliseconds() {
  char out[INSTANT_LEN];
  format_utc(1792663500123LL, out);
  TEST_ASSERT_EQUAL_STRING("2026-10-22T10:05:00.123Z", out);
}

static void test_payload_failed_read_is_null_not_zero() {
  Sample s = mk(43, 1);
  s.status = ReadStatus::NO_ACK;
  char out[640];
  build_telemetry(s, dev(), 3, out, sizeof out);
  TEST_ASSERT_NOT_NULL(strstr(out, "\"temperature_c\":null"));
  TEST_ASSERT_NOT_NULL(strstr(out, "\"humidity_pct\":null"));
  TEST_ASSERT_NOT_NULL(strstr(out, "\"measured_at\":null"));
  TEST_ASSERT_NOT_NULL(strstr(out, "\"read_status\":\"NO_ACK\""));
  TEST_ASSERT_NOT_NULL(strstr(out, "\"dropped_since_boot\":3"));
}

static void test_payload_too_small_buffer() {
  char out[32];
  TEST_ASSERT_EQUAL(0, build_telemetry(mk(1, 1), dev(), 0, out, sizeof out));
}

static void test_parse_ack() {
  const char *ok = "{\"schema\":\"kisaniti.device_ack.v1\",\"boot_id\":\"3fa9c1e2\",\"seq\":42,\"status\":\"ACCEPTED\"}";
  const char *other_boot = "{\"schema\":\"kisaniti.device_ack.v1\",\"boot_id\":\"00000000\",\"seq\":42,\"status\":\"ACCEPTED\"}";
  const char *bad_status = "{\"schema\":\"kisaniti.device_ack.v1\",\"boot_id\":\"3fa9c1e2\",\"seq\":42,\"status\":\"MAYBE\"}";
  uint32_t seq = 0;
  TEST_ASSERT_TRUE(parse_ack(ok, strlen(ok), "3fa9c1e2", &seq));
  TEST_ASSERT_EQUAL(42, seq);
  TEST_ASSERT_FALSE(parse_ack(other_boot, strlen(other_boot), "3fa9c1e2", &seq));
  TEST_ASSERT_FALSE(parse_ack(bad_status, strlen(bad_status), "3fa9c1e2", &seq));
  TEST_ASSERT_FALSE(parse_ack("not json", 8, "3fa9c1e2", &seq));
}

static void test_dht11_decode_ok() {
  // 55.0 %RH, 27.3 C, checksum = 55 + 0 + 27 + 3 = 85
  const uint8_t f[5] = {55, 0, 27, 3, 85};
  float t, rh;
  TEST_ASSERT_EQUAL(ReadStatus::OK, dht11_decode(f, &t, &rh));
  TEST_ASSERT_FLOAT_WITHIN(0.01, 27.3, t);
  TEST_ASSERT_FLOAT_WITHIN(0.01, 55.0, rh);
}

static void test_dht11_negative_temperature() {
  const uint8_t f[5] = {40, 0, 2, 0x85, (uint8_t)(40 + 2 + 0x85)};  // -2.5 C
  float t, rh;
  TEST_ASSERT_EQUAL(ReadStatus::OK, dht11_decode(f, &t, &rh));
  TEST_ASSERT_FLOAT_WITHIN(0.01, -2.5, t);
}

static void test_dht11_failures_are_not_values() {
  float t = 99, rh = 99;
  const uint8_t bad_sum[5] = {55, 0, 27, 3, 86};
  TEST_ASSERT_EQUAL(ReadStatus::CRC_ERROR, dht11_decode(bad_sum, &t, &rh));
  const uint8_t zeros[5] = {0, 0, 0, 0, 0};  // checksum matches, but the line was stuck
  TEST_ASSERT_EQUAL(ReadStatus::CRC_ERROR, dht11_decode(zeros, &t, &rh));
  const uint8_t over[5] = {120, 0, 20, 0, 140};  // RH above 100 %
  TEST_ASSERT_EQUAL(ReadStatus::CRC_ERROR, dht11_decode(over, &t, &rh));
  TEST_ASSERT_FLOAT_WITHIN(0.01, 99, t);  // outputs untouched on failure
  TEST_ASSERT_FLOAT_WITHIN(0.01, 99, rh);
}

static void test_dht22_decode_ok() {
  // 65.2 %RH = 0x028C, 35.1 C = 0x015F; checksum = 0x02 + 0x8C + 0x01 + 0x5F = 0xEE (datasheet example)
  const uint8_t f[5] = {0x02, 0x8C, 0x01, 0x5F, 0xEE};
  float t, rh;
  TEST_ASSERT_EQUAL(ReadStatus::OK, dht22_decode(f, &t, &rh));
  TEST_ASSERT_FLOAT_WITHIN(0.01, 35.1, t);
  TEST_ASSERT_FLOAT_WITHIN(0.01, 65.2, rh);
}

static void test_dht22_negative_temperature() {
  const uint8_t f[5] = {0x01, 0xF4, 0x80, 0x65, (uint8_t)(0x01 + 0xF4 + 0x80 + 0x65)};  // 50.0 %RH, -10.1 C
  float t, rh;
  TEST_ASSERT_EQUAL(ReadStatus::OK, dht22_decode(f, &t, &rh));
  TEST_ASSERT_FLOAT_WITHIN(0.01, -10.1, t);
  TEST_ASSERT_FLOAT_WITHIN(0.01, 50.0, rh);
}

static void test_dht22_failures_are_not_values() {
  float t = 99, rh = 99;
  const uint8_t bad_sum[5] = {0x02, 0x8C, 0x01, 0x5F, 0xEF};
  TEST_ASSERT_EQUAL(ReadStatus::CRC_ERROR, dht22_decode(bad_sum, &t, &rh));
  const uint8_t zeros[5] = {0, 0, 0, 0, 0};
  TEST_ASSERT_EQUAL(ReadStatus::CRC_ERROR, dht22_decode(zeros, &t, &rh));
  const uint8_t over_rh[5] = {0x03, 0xF2, 0x00, 0xC8, (uint8_t)(0x03 + 0xF2 + 0x00 + 0xC8)};  // 101.0 %RH
  TEST_ASSERT_EQUAL(ReadStatus::CRC_ERROR, dht22_decode(over_rh, &t, &rh));
  const uint8_t hot[5] = {0x01, 0xF4, 0x03, 0x84, (uint8_t)(0x01 + 0xF4 + 0x03 + 0x84)};  // 90.0 C
  TEST_ASSERT_EQUAL(ReadStatus::CRC_ERROR, dht22_decode(hot, &t, &rh));
  TEST_ASSERT_FLOAT_WITHIN(0.01, 99, t);  // outputs untouched on failure
  TEST_ASSERT_FLOAT_WITHIN(0.01, 99, rh);
}

static void test_dht11_frame_is_not_read_as_dht22() {
  // A DHT11 frame (55 %RH, 27.3 C) through the DHT22 decoder would give 1408 %RH: rejected, not a value.
  const uint8_t f[5] = {55, 0, 27, 3, 85};
  float t = 99, rh = 99;
  TEST_ASSERT_EQUAL(ReadStatus::CRC_ERROR, dht22_decode(f, &t, &rh));
}

int main() {
  UNITY_BEGIN();
  RUN_TEST(test_dht11_decode_ok);
  RUN_TEST(test_dht11_negative_temperature);
  RUN_TEST(test_dht11_failures_are_not_values);
  RUN_TEST(test_dht22_decode_ok);
  RUN_TEST(test_dht22_negative_temperature);
  RUN_TEST(test_dht22_failures_are_not_values);
  RUN_TEST(test_dht11_frame_is_not_read_as_dht22);
  RUN_TEST(test_crc_datasheet_example);
  RUN_TEST(test_decode_ok_and_conversion);
  RUN_TEST(test_decode_extremes);
  RUN_TEST(test_decode_bad_crc);
  RUN_TEST(test_buffer_overflow_drops_oldest);
  RUN_TEST(test_buffer_out_of_order_ack);
  RUN_TEST(test_backfill_from_uptime);
  RUN_TEST(test_ack_timeout_returns_to_pending);
  RUN_TEST(test_payload_ok);
  RUN_TEST(test_instant_has_milliseconds);
  RUN_TEST(test_payload_failed_read_is_null_not_zero);
  RUN_TEST(test_payload_too_small_buffer);
  RUN_TEST(test_parse_ack);
  return UNITY_END();
}

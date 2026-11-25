// DHT22 (AM2302) frame decoding, independent of Arduino so it can be unit-tested on the host.
// Sensor bought 25 Nov 2026 when the SHT30 was unavailable locally (CHAT-56): build with
// -DKN_SENSOR_DHT22=1 (env esp32dev_dht22). The single-wire protocol and bit timing are the
// DHT11's; only the start pulse, the read interval and the frame encoding differ.
//
// Frame (40 bits, MSB first): RH_hi RH_lo T_hi T_lo checksum
//   RH = (RH_hi << 8 | RH_lo) / 10 %;  T = ((T_hi & 0x7F) << 8 | T_lo) / 10 C, negative if T_hi bit 7
//   checksum = (RH_hi + RH_lo + T_hi + T_lo) & 0xFF
// Explicit failure semantics: a bad checksum, an all-zero frame (line stuck low or no data),
// a humidity above 100 % and a temperature outside the sensor's -40..80 C range are reported
// as CRC_ERROR, never as a value.
#pragma once
#include <stdint.h>
#include "sht3x_codec.h"  // ReadStatus

namespace kn {

constexpr uint32_t DHT22_START_LOW_MS = 2;       // host start signal, datasheet 1-10 ms
constexpr uint32_t DHT22_MIN_INTERVAL_MS = 2100; // sensor needs >= 2 s between reads

inline ReadStatus dht22_decode(const uint8_t f[5], float *temperature_c, float *humidity_pct) {
  if ((uint8_t)(f[0] + f[1] + f[2] + f[3]) != f[4]) return ReadStatus::CRC_ERROR;
  if ((f[0] | f[1] | f[2] | f[3] | f[4]) == 0) return ReadStatus::CRC_ERROR;
  float rh = (float)(((uint16_t)f[0] << 8) | f[1]) * 0.1f;
  float t = (float)(((uint16_t)(f[2] & 0x7F) << 8) | f[3]) * 0.1f;
  if (f[2] & 0x80) t = -t;
  if (rh > 100.0f || t < -40.0f || t > 80.0f) return ReadStatus::CRC_ERROR;
  *temperature_c = t;
  *humidity_pct = rh;
  return ReadStatus::OK;
}

}  // namespace kn

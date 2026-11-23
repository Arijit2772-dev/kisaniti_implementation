// DHT11 frame decoding, independent of Arduino so it can be unit-tested on the host.
// Stop-gap sensor (decision recorded 23 Nov 2026): the SHT30 module ordered for the node
// arrived as a DHT11, so the firmware can be built with -DKN_SENSOR_DHT11=1 until the
// SHT30 arrives. The SHT30 build is unchanged and stays the default.
//
// Frame (40 bits, MSB first): RH_int RH_dec T_int T_dec checksum
//   checksum = (RH_int + RH_dec + T_int + T_dec) & 0xFF
//   bit 7 of T_dec marks a negative temperature on newer DHT11 parts.
// Explicit failure semantics: a bad checksum, an all-zero frame (line stuck low or no
// data) and a humidity above 100 % are reported as CRC_ERROR, never as a value.
#pragma once
#include <stdint.h>
#include "sht3x_codec.h"  // ReadStatus

namespace kn {

constexpr uint32_t DHT11_START_LOW_MS = 20;     // host start signal, datasheet >= 18 ms
constexpr uint32_t DHT11_MIN_INTERVAL_MS = 1100; // sensor needs >= 1 s between reads
constexpr uint32_t DHT11_BIT_ONE_US = 40;        // high pulse ~26-28 us = 0, ~70 us = 1

inline ReadStatus dht11_decode(const uint8_t f[5], float *temperature_c, float *humidity_pct) {
  if ((uint8_t)(f[0] + f[1] + f[2] + f[3]) != f[4]) return ReadStatus::CRC_ERROR;
  if ((f[0] | f[1] | f[2] | f[3] | f[4]) == 0) return ReadStatus::CRC_ERROR;
  float rh = (float)f[0] + (float)f[1] * 0.1f;
  if (rh > 100.0f) return ReadStatus::CRC_ERROR;
  float t = (float)f[2] + (float)(f[3] & 0x7F) * 0.1f;
  if (f[3] & 0x80) t = -t;
  *temperature_c = t;
  *humidity_pct = rh;
  return ReadStatus::OK;
}

}  // namespace kn

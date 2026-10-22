// SHT3x frame decoding, independent of Arduino so it can be unit-tested on the host.
// Values from the Sensirion SHT3x-DIS datasheet, Version 7 (December 2022):
//   CRC-8, polynomial 0x31, init 0xFF, CRC(0xBEEF) = 0x92   (Table 20)
//   RH = 100 * S_RH / (2^16 - 1);  T = -45 + 175 * S_T / (2^16 - 1)   (Section 4.13)
#pragma once
#include <stdint.h>
#include <stddef.h>

namespace kn {

constexpr uint8_t SHT3X_ADDR_DEFAULT = 0x44;
constexpr uint16_t SHT3X_CMD_SINGLE_HIGH_NOSTRETCH = 0x2400;  // Table 9
constexpr uint16_t SHT3X_CMD_SOFT_RESET = 0x30A2;
constexpr uint32_t SHT3X_MEAS_HIGH_MAX_MS = 16;  // datasheet max 15.5 ms, rounded up
constexpr uint32_t SHT3X_SOFT_RESET_MAX_MS = 2;  // datasheet max 1.5 ms

inline uint8_t sht3x_crc8(const uint8_t *data, size_t len) {
  uint8_t crc = 0xFF;
  for (size_t i = 0; i < len; i++) {
    crc ^= data[i];
    for (int b = 0; b < 8; b++) {
      crc = (crc & 0x80) ? (uint8_t)((crc << 1) ^ 0x31) : (uint8_t)(crc << 1);
    }
  }
  return crc;
}

enum class ReadStatus : uint8_t { OK = 0, CRC_ERROR, NO_ACK, TIMEOUT };

inline const char *read_status_name(ReadStatus s) {
  switch (s) {
    case ReadStatus::OK: return "OK";
    case ReadStatus::CRC_ERROR: return "CRC_ERROR";
    case ReadStatus::NO_ACK: return "NO_ACK";
    case ReadStatus::TIMEOUT: return "TIMEOUT";
  }
  return "NO_ACK";
}

inline float sht3x_temperature_c(uint16_t raw) { return -45.0f + 175.0f * (float)raw / 65535.0f; }
inline float sht3x_humidity_pct(uint16_t raw) { return 100.0f * (float)raw / 65535.0f; }

// Decode the 6-byte single-shot frame: T_msb T_lsb T_crc RH_msb RH_lsb RH_crc.
inline ReadStatus sht3x_decode(const uint8_t frame[6], float *temperature_c, float *humidity_pct) {
  if (sht3x_crc8(frame, 2) != frame[2] || sht3x_crc8(frame + 3, 2) != frame[5]) {
    return ReadStatus::CRC_ERROR;
  }
  *temperature_c = sht3x_temperature_c((uint16_t)((frame[0] << 8) | frame[1]));
  *humidity_pct = sht3x_humidity_pct((uint16_t)((frame[3] << 8) | frame[4]));
  return ReadStatus::OK;
}

// Two-decimal rounding for the payload (the sensor resolves 0.01 %RH / 0.01 C).
inline float round2(float v) {
  float s = v * 100.0f;
  long r = (long)(s < 0 ? s - 0.5f : s + 0.5f);
  return (float)r / 100.0f;
}

}  // namespace kn

// Wokwi custom chip emulating the I2C interface of a Sensirion SHT3x (SHT30)
// humidity/temperature sensor, enough for the KisanNiti firmware:
//   address 0x44; single-shot commands 0x2400/0x240B/0x2416 (no clock stretching)
//   and 0x2C06/0x2C0D/0x2C10; soft reset 0x30A2; status 0xF32D; clear status 0x3041.
// The read header is NACKed until the measurement time has passed, as the real
// sensor does without clock stretching. Data words carry the CRC-8 of the
// datasheet (poly 0x31, init 0xFF). Reference: SHT3x-DIS datasheet v7 (Dec 2022).
// Periodic mode, alert and heater are not emulated.
#include "wokwi-api.h"
#include <stdint.h>
#include <stdbool.h>
#include <stdlib.h>
#include <string.h>
#include <stdio.h>

#define SHT3X_ADDR 0x44
#define MEAS_HIGH_US 12500  // typical high-repeatability duration
#define MEAS_MED_US 4500
#define MEAS_LOW_US 2500

typedef struct {
  uint32_t attr_temperature;
  uint32_t attr_humidity;
  uint32_t attr_fault;
  uint8_t cmd[2];
  uint8_t cmd_len;
  uint8_t out[6];
  uint8_t out_len;
  uint8_t out_pos;
  bool measuring;
  uint64_t ready_at_ns;
  uint16_t status;
} chip_state_t;

static uint8_t crc8(const uint8_t *data, int len) {
  uint8_t crc = 0xFF;
  for (int i = 0; i < len; i++) {
    crc ^= data[i];
    for (int b = 0; b < 8; b++) crc = (crc & 0x80) ? (uint8_t)((crc << 1) ^ 0x31) : (uint8_t)(crc << 1);
  }
  return crc;
}

static void put_word(chip_state_t *c, int at, uint16_t w) {
  c->out[at] = (uint8_t)(w >> 8);
  c->out[at + 1] = (uint8_t)(w & 0xFF);
  c->out[at + 2] = crc8(&c->out[at], 2);
}

static uint16_t clamp_raw(float v) {
  if (v < 0) return 0;
  if (v > 65535.0f) return 65535;
  return (uint16_t)(v + 0.5f);
}

static void fill_measurement(chip_state_t *c) {
  float t = attr_read_float(c->attr_temperature);
  float rh = attr_read_float(c->attr_humidity);
  // Inverse of T = -45 + 175*S/65535 and RH = 100*S/65535
  put_word(c, 0, clamp_raw((t + 45.0f) * 65535.0f / 175.0f));
  put_word(c, 3, clamp_raw(rh * 65535.0f / 100.0f));
  if (attr_read(c->attr_fault) == 1) c->out[5] ^= 0x5A;  // corrupt the humidity CRC
  c->out_len = 6;
  c->out_pos = 0;
}

static void start_measurement(chip_state_t *c, uint32_t duration_us) {
  c->measuring = true;
  c->ready_at_ns = get_sim_nanos() + (uint64_t)duration_us * 1000;
  c->out_len = 0;
}

static void run_command(chip_state_t *c, uint16_t cmd) {
  switch (cmd) {
    case 0x2400: case 0x2C06: start_measurement(c, MEAS_HIGH_US); break;
    case 0x240B: case 0x2C0D: start_measurement(c, MEAS_MED_US); break;
    case 0x2416: case 0x2C10: start_measurement(c, MEAS_LOW_US); break;
    case 0x30A2:  // soft reset
      c->measuring = false;
      c->out_len = 0;
      c->status = 0x0010;  // reset detected bit
      break;
    case 0xF32D:  // read status register
      put_word(c, 0, c->status);
      c->out_len = 3;
      c->out_pos = 0;
      break;
    case 0x3041:  // clear status
      c->status = 0;
      break;
    default:
      printf("sht3x: unsupported command 0x%04X\n", cmd);
      break;
  }
}

static bool on_connect(void *user_data, uint32_t address, bool read) {
  chip_state_t *c = user_data;
  if (attr_read(c->attr_fault) == 2) return false;  // sensor not answering
  if (!read) {
    c->cmd_len = 0;
    return true;
  }
  if (c->measuring) {
    if (get_sim_nanos() < c->ready_at_ns) return false;  // NACK: data not ready yet
    c->measuring = false;
    fill_measurement(c);
  }
  return c->out_len > 0;  // NACK when there is nothing to read
}

static uint8_t on_read(void *user_data) {
  chip_state_t *c = user_data;
  if (c->out_pos >= c->out_len) return 0xFF;
  uint8_t b = c->out[c->out_pos++];
  if (c->out_pos >= c->out_len) c->out_len = 0;  // data is read once
  return b;
}

static bool on_write(void *user_data, uint8_t data) {
  chip_state_t *c = user_data;
  if (c->cmd_len < 2) c->cmd[c->cmd_len++] = data;
  if (c->cmd_len == 2) {
    run_command(c, (uint16_t)((c->cmd[0] << 8) | c->cmd[1]));
    c->cmd_len = 3;  // ignore extra bytes until the next transaction
  }
  return true;
}

static void on_disconnect(void *user_data) {
  chip_state_t *c = user_data;
  c->cmd_len = 0;
}

void chip_init(void) {
  chip_state_t *c = malloc(sizeof(chip_state_t));
  memset(c, 0, sizeof(chip_state_t));
  c->attr_temperature = attr_init_float("temperature", 28.0f);
  c->attr_humidity = attr_init_float("humidity", 62.0f);
  c->attr_fault = attr_init("fault", 0);
  c->status = 0x0010;

  const i2c_config_t i2c = {
      .user_data = c,
      .address = SHT3X_ADDR,
      .scl = pin_init("SCL", INPUT_PULLUP),
      .sda = pin_init("SDA", INPUT_PULLUP),
      .connect = on_connect,
      .read = on_read,
      .write = on_write,
      .disconnect = on_disconnect,
  };
  i2c_init(&i2c);
  printf("sht3x: chip ready at 0x%02X\n", SHT3X_ADDR);
}

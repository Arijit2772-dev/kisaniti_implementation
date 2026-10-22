// Drives the emulated SHT3x the way the firmware does and decodes with the
// firmware's own codec. Build and run: ./run_test.sh
#include <cstdio>
#include <cstdint>
#include <cmath>
#include "../../../lib/kn_core/src/sht3x_codec.h"
extern "C" {
#include "../wokwi-api.h"
extern i2c_config_t mock_i2c;
extern double mock_now_ns;
extern float mock_attr_f[8];
extern uint32_t mock_attr_i[8];
int mock_attr_id(const char *name);
}

static int failures = 0;
#define CHECK(cond, msg) do { if (!(cond)) { std::printf("FAIL: %s\n", msg); failures++; } else std::printf("ok: %s\n", msg); } while (0)

static bool write_cmd(uint16_t cmd) {
  if (!mock_i2c.connect(mock_i2c.user_data, 0x44, false)) return false;
  mock_i2c.write(mock_i2c.user_data, cmd >> 8);
  mock_i2c.write(mock_i2c.user_data, cmd & 0xFF);
  mock_i2c.disconnect(mock_i2c.user_data);
  return true;
}

static bool read_frame(uint8_t *f, int n) {
  if (!mock_i2c.connect(mock_i2c.user_data, 0x44, true)) return false;
  for (int i = 0; i < n; i++) f[i] = mock_i2c.read(mock_i2c.user_data);
  mock_i2c.disconnect(mock_i2c.user_data);
  return true;
}

int main() {
  chip_init();
  CHECK(mock_i2c.address == 0x44, "chip listens on 0x44");
  int t_id = mock_attr_id("temperature"), rh_id = mock_attr_id("humidity"), fault_id = mock_attr_id("fault");

  const float cases[][2] = {{28.0f, 62.0f}, {-10.0f, 0.0f}, {60.0f, 100.0f}, {24.5f, 65.3f}, {31.7f, 71.9f}};
  for (auto &cs : cases) {
    mock_attr_f[t_id] = cs[0];
    mock_attr_f[rh_id] = cs[1];
    uint8_t f[6];
    CHECK(write_cmd(kn::SHT3X_CMD_SINGLE_HIGH_NOSTRETCH), "single-shot command acked");
    CHECK(!read_frame(f, 6), "read header NACKed while measuring");
    mock_now_ns += 16e6;  // firmware waits 16 ms
    CHECK(read_frame(f, 6), "read header acked after measurement time");
    float t, rh;
    kn::ReadStatus st = kn::sht3x_decode(f, &t, &rh);
    char msg[96];
    std::snprintf(msg, sizeof msg, "decode %.1f C / %.1f %% -> %.3f / %.3f", cs[0], cs[1], t, rh);
    CHECK(st == kn::ReadStatus::OK && std::fabs(t - cs[0]) < 0.01f && std::fabs(rh - cs[1]) < 0.01f, msg);
    CHECK(!read_frame(f, 6), "data is read only once");
  }

  mock_attr_i[fault_id] = 1;
  uint8_t f[6];
  write_cmd(kn::SHT3X_CMD_SINGLE_HIGH_NOSTRETCH);
  mock_now_ns += 16e6;
  read_frame(f, 6);
  float t, rh;
  CHECK(kn::sht3x_decode(f, &t, &rh) == kn::ReadStatus::CRC_ERROR, "fault=1 gives CRC error");

  mock_attr_i[fault_id] = 2;
  CHECK(!write_cmd(kn::SHT3X_CMD_SINGLE_HIGH_NOSTRETCH), "fault=2 NACKs the address");

  mock_attr_i[fault_id] = 0;
  CHECK(write_cmd(kn::SHT3X_CMD_SOFT_RESET), "soft reset acked");
  CHECK(write_cmd(0xF32D), "status command acked");
  CHECK(read_frame(f, 3) && kn::sht3x_crc8(f, 2) == f[2], "status word has valid CRC");

  std::printf(failures ? "\n%d FAILED\n" : "\nALL PASSED\n", failures);
  return failures ? 1 : 0;
}

// Minimal host stand-in for the Wokwi chip API, so sht3x.chip.c can be tested natively.
#include "../wokwi-api.h"
#include <string.h>

i2c_config_t mock_i2c;
double mock_now_ns = 0;
float mock_attr_f[8];
uint32_t mock_attr_i[8];
static const char *names[8];
static int n_attrs = 0;

static int find_or_add(const char *name) {
  for (int i = 0; i < n_attrs; i++) if (strcmp(names[i], name) == 0) return i;
  names[n_attrs] = name;
  return n_attrs++;
}
pin_t pin_init(const char *name, uint32_t mode) { (void)name; (void)mode; return 0; }
uint32_t attr_init(const char *name, uint32_t def) { int i = find_or_add(name); mock_attr_i[i] = def; return i; }
uint32_t attr_init_float(const char *name, float def) { int i = find_or_add(name); mock_attr_f[i] = def; return i; }
uint32_t attr_read(uint32_t id) { return mock_attr_i[id]; }
float attr_read_float(uint32_t id) { return mock_attr_f[id]; }
i2c_dev_t i2c_init(const i2c_config_t *config) { mock_i2c = *config; return 1; }
double get_sim_nanos_d(void) { return mock_now_ns; }
int mock_attr_id(const char *name) { return find_or_add(name); }

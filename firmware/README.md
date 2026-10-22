# KisanNiti storage-node firmware (ESP32-WROOM-32 + SHT30 probe)

Contract: `../../kisaniti/docs/contracts/L3b_SENSOR_STORAGE_CONTRACT_v1.md` (W01-L3B). Schemas: `../contracts/sensor/`.

| Command | What it does | Last result (22 Oct 2026) |
|---|---|---|
| `pio test -e native` | 12 host tests: CRC (datasheet CRC(0xBEEF)=0x92), conversion, buffer, payload, ack | PASS |
| `pio run -e esp32dev` | Real node: secrets.h, MQTT over TLS :8883, 5-min samples, app ack | builds (Arduino-ESP32 2.0.17, platform espressif32 6.9.0) |
| `pio run -e wokwi` | Twin: Wokwi-GUEST, test.mosquitto.org:1883, 10-s samples, `source_origin=WOKWI_TWIN` | builds |
| `wokwi/chips/test/run_test.sh` | Emulated SHT3x chip driven with the firmware decoder | PASS |
| `wokwi/build_chip.sh` | SHT3x custom chip → wasm (Docker) | builds |
| `python3 wokwi/make_web_bundle.py` | Files for a wokwi.com project (`wokwi/web_bundle/`) | ran on wokwi.com: compiles, finds SHT30 at 0x44, reads 28.00 °C / 62.00 % |

**Build and calibrate the real node:** [BUILD_GUIDE.md](BUILD_GUIDE.md). Bench records go in `records/`.

Wokwi has no SHT3x part, so `wokwi/chips/sht3x.chip.c` emulates it (0x44, single shot, NACK while measuring, CRC, fault slider). `wokwi-api.h` is from wokwi/inverter-chip (MIT).

**Twin on wokwi.com:** open https://wokwi.com/projects/new/esp32, paste `sketch.ino` and `diagram.json` over the defaults, add `libraries.txt` through the Library Manager (or paste it once that tab exists), add new files `sht3x.chip.json` and `sht3x.chip.c` (tab menu ▸ New file…), then Start. Sign in and Save to get a shareable link (only the user can do this). Public link: _not saved yet_.

**Local twin (VS Code Wokwi extension or wokwi-cli, needs a free Wokwi licence/token):** `pio run -e wokwi`, `wokwi/build_chip.sh`, then start the simulator in this folder (`wokwi.toml`, `diagram.json`).

**Limits (v1):** RAM buffer of 288 samples, lost on power cut; MQTT/NTP connection in the twin was not yet observed end to end (the in-app browser ran the simulation at ~3% speed).

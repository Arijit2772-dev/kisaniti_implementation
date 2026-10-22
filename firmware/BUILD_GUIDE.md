# Storage node — build, calibration and test guide

For the person who builds the node (master plan steps H1–H8). Each test ends with something to **record**; put the
records in `firmware/records/` (photos, `calibration_log.csv`, the `kn-report` output) and tell the team, so the paper
uses measured values, not the assumptions below.

What the node does: an SHT30 probe sits inside a bag of grain; an ESP32 reads it every 5 minutes and sends the
reading over Wi-Fi (your phone hotspot) to a broker on your laptop using MQTT over TLS. The laptop stores each reading
once and works out a storage category with the rule STORAGE-DEMO-0.1. The node estimates **conditions** that favour
mould or insects at one point. It does **not** measure moisture %, detect spoilage or predict shelf life.

Contract: `kisaniti/docs/contracts/L3b_SENSOR_STORAGE_CONTRACT_v1.md`. Firmware commands: [README.md](README.md).
Backend: [`../backend/ingestion/README.md`](../backend/ingestion/README.md).

---

## H1. Parts and wiring

Kit list: `kisaniti/docs/HARDWARE_CURRENT.md` (ESP32-WROOM-32 DevKit, SHT30 probe with ~50 cm cable, 2 × 4.7 kΩ,
breadboards, jumpers, 4-way screw terminal, USB data cable, 5 V/2 A adapter, labels, about 1 kg dried basmati paddy
in a container you can seal).

**Before connecting anything:** read the probe's wire labels or the seller's sheet and write down which colour is
VCC, GND, SDA and SCL. Colours differ between sellers, so do not guess them from this guide. Take a photo of the label.

| Probe wire | ESP32 pin | Note |
|---|---|---|
| VCC | 3V3 | **not** 5V/VIN |
| GND | GND | |
| SDA | GPIO 21 | 4.7 kΩ from SDA to 3V3 |
| SCL | GPIO 22 | 4.7 kΩ from SCL to 3V3 |

Use the printed pin names on the board, not pin numbers from a schematic. Wire everything with the USB cable
unplugged. Bring the four probe wires into the screw terminal, then jumper to the breadboard. Keep the ESP32 outside
the grain container; only the probe goes in. Do not use this grain for food afterwards (the probe housing is not
certified food-safe).

**Record:** a photo of the wiring and of the probe label (`records/H1_wiring.jpg`).

## H2. Firmware

1. Install PlatformIO (VS Code extension, or `pip install platformio`). The versions are pinned in
   `platformio.ini` (espressif32 6.9.0 → Arduino-ESP32 2.0.17, ArduinoJson 7.2.1, MQTT 2.5.2).
2. Bench check without Wi-Fi first: build the simulation profile but flash it to the real board — it uses the same
   sensor code and prints readings even when it cannot reach Wi-Fi:

   ```bash
   cd firmware
   pio run -e wokwi -t upload
   pio device monitor -b 115200
   ```

   Expected: `[kn] SHT30 found at 0x44`, then every 10 s `[kn] sample seq=… T=…C RH=…%`. Breathe on the probe: RH
   should rise within a minute and fall again. `read_status=NO_ACK` means the sensor is not answering (check wiring,
   VCC on 3V3, pull-ups). `CRC_ERROR` usually means a loose wire or a very long cable.
   Some SHT30 boards use address 0x45 (ADDR pin high); the firmware expects 0x44. If the scan fails, tell the team.
3. Set up the laptop broker and ingestion (H2b below), then copy `include/secrets.example.h` to `include/secrets.h`
   and fill in the hotspot name and password, the laptop's IP on the hotspot, the device password you created, and
   the CA certificate text from `backend/ingestion/broker/certs/ca.crt`. `secrets.h` is git-ignored: never commit it.
4. Flash the real profile: `pio run -e esp32dev -t upload`, then `pio device monitor`. Expected lines:
   `wifi connecting…`, `clock synced …; back-filled N earlier samples`, `mqtt connected`, then `TX …` and
   `ack seq=…` every 5 minutes.

### H2b. Laptop broker and ingestion (one time)

```bash
cd backend/ingestion
uv sync
broker/gen_certs.sh <laptop-ip-on-hotspot>      # e.g. 192.168.43.10 (see: ipconfig getifaddr en0)
broker/add_user.sh ingestion
broker/add_user.sh kn-node-001
docker run -d --name kn-broker -p 8883:8883 -v "$PWD/broker":/mosquitto/config eclipse-mosquitto:2.0.20
cp devices.example.json devices.json
KN_MQTT_HOST=localhost KN_MQTT_PORT=8883 KN_MQTT_TLS_CA=broker/certs/ca.crt KN_MQTT_USER=ingestion \
  KN_MQTT_PASSWORD=<password> KN_DEVICES=devices.json KN_STORE=records.sqlite3 uv run kn-ingest
```

Keep the laptop awake and on the hotspot during tests. If the hotspot gives the laptop a new IP, the certificate
must be made again for that IP.

**Record:** the first 20 lines of the serial monitor and of the `kn-ingest` log (`records/H2_first_run.txt`).

## H3. Wokwi twin

Anyone can run the same firmware without hardware: [README.md](README.md) → "Twin on wokwi.com". Sign in to
wokwi.com, paste the five files from `wokwi/web_bundle/` (`python3 wokwi/make_web_bundle.py` makes them) and press
**Save**, then **Share** to get a public link for reviewers. Twin readings carry `source_origin = WOKWI_TWIN` and are
stored as SYNTHETIC; they are never shown as real data.

**Record:** the public Wokwi link in `firmware/README.md`.

## H4. Calibration with salt (offset) and settling time

### Salt check (about 24 h)

Over a saturated table-salt (NaCl) slurry, air in a closed jar settles at **75.47 ± 0.14 %RH at 20 °C, 75.29 ± 0.12
at 25 °C and 75.09 ± 0.11 at 30 °C** (Greenspan 1977, *J. Res. NBS* 81A(1), 89–96, Table 2).

1. Put about 3 tablespoons of non-iodised salt in a small cup and add water a few drops at a time until it looks
   like wet sand (some undissolved salt must remain).
2. Put the cup and the probe tip in a jar or zip bag, probe **not touching** the salt, and seal it around the cable.
   Keep it out of sun and away from fans, ideally where temperature is steady (indoors, 20–30 °C).
3. Leave it 24 h (at least 12 h) with the node running. Then run
   `uv run kn-report --store records.sqlite3 --device kn-node-001` and take the median RH and temperature of the
   last 2 h (the dashboard or a short `sqlite3` query on `sensor_readings` also works).
4. Offset = reference RH at that temperature − median reading. Example: reading 73.9 % at 25 °C → offset +1.4.

**Record** in `records/calibration_log.csv`: date, start/end time, median T, median RH, reference RH, offset,
photo. If |offset| > 2 %RH (beyond the SHT30's typical tolerance), repeat once; if it stays, tell the team.

Why it matters: the simulation showed that a +2 %RH error can hold the category one level too high for weeks and a
−2 %RH error can miss a fifth of the HIGH hours (`kisaniti_research_paper/layers/L3b_storage_iot/README.md`).
The current rule does **not** apply the offset yet; using it needs a small contract change (an optional
calibration offset on the device assignment), which the team will make once a real offset is measured.

### Settling time in grain (repeat 3 times)

1. Let the probe sit in room air for 1 h. Note the time.
2. Push the probe tip to the middle of the 1 kg grain (at least ~10 cm from every surface in a small container) and
   seal the container. Note the time.
3. Leave it 12 h. Settling time = time from insertion until RH stays within ±0.5 %RH of its final value
   (median of hours 10–12) for 2 h.

**Record** each run's settling time. The rule assumes 6 h; if your median differs clearly, the team will update the
settling constant through the decision register (it is not changed silently in code).

## H5. Outage tests

Run each with the node in the grain and normal 5-minute sampling. Note the exact start and end times. After each,
run `uv run kn-report --store records.sqlite3 --device kn-node-001 --since <start>`.

| Test | Do | Expected (firmware v1) |
|---|---|---|
| Wi-Fi off 30 min | turn the hotspot off | 6 samples wait in the buffer, arrive at reconnect with their original `measured_at`; no missing seq |
| Wi-Fi off 26 h | hotspot off overnight | buffer keeps the newest 288; the oldest ~24 are dropped (`dropped_since_boot` > 0, missing seq range) |
| Broker off 30 min | `docker stop kn-broker`, then `docker start kn-broker` | as Wi-Fi off; node reconnects with back-off (≤ 60 s) |
| Ingestion off 30 min | stop `kn-ingest` (Ctrl-C), restart | device gets no ack and resends after 60 s; on restart the worker receives queued and resent copies; each reading stored once (DUPLICATE acks) |
| Power cut 10 min | unplug USB, replug | new `boot_id`; samples not yet sent are lost (RAM buffer); gap visible |
| Reboot | press EN | as power cut, without the off time |
| Sensor unplugged | **power off first**, unplug probe, power on | `read_status = NO_ACK`, values null (never 0); category becomes UNKNOWN after 15 min |

**Record** a table with: test, start, end, samples expected, received, missing seq, max delay, and anything
unexpected (`records/H5_outages.md`). Any difference from the "Expected" column is a finding, not a failure to hide.

## H6. One-week grain run

1. Days 1–3 (dry): the node in about 1 kg of dried basmati paddy in a labelled, sealed container, indoors.
2. Day 4 (humidified): open the container, spray 20 ml of water evenly over the grain while mixing (roughly +1.7
   percentage points of moisture for 1 kg), reseal with the probe back at mid-depth. Note the time.
3. Days 4–7: leave it closed. Expect in-grain RH to rise over hours and the category to move towards MEDIUM/HIGH.
   Check for visible mould on day 7 and photograph it; throw the grain away afterwards.
4. Every day run `uv run kn-assess --store records.sqlite3 --assignment assignment.json` with an assignment file
   like `contracts/sensor/v1/examples/valid/assignment_in_grain.json` (your device ID, `IN_GRAIN`, `PADDY`, the
   insertion time).

**Record:** the store file (`records.sqlite3`), daily `kn-assess` outputs, `kn-report` output, photos, the times of
insertion and humidification. The research session turns these into the real-sensor plot.

## H7. One-time moisture-meter check

Take about 200 g of grain from the probed container (before humidifying, and once after) to a mandi or procurement
centre moisture meter. **Record** the meter reading, the meter type, date and time, and the node's RH and
temperature at that moment. This links the a_w bands to the moisture % farmers and buyers use (Part B5 of the
Patiala research note).

## H8. Demo set-up

Laptop (broker + `kn-ingest` + app) and node on the same phone hotspot; node in a labelled demo container; serial
monitor and `kn-report` open in a terminal. Before a demo, run H2 step 4 once to confirm acks arrive. The demo grain
is not for food.

---

### What only you can do (summary)

1. Buy the kit and check the probe's wire labels (H1).
2. Wire and flash the node; create the broker certificates and passwords on your laptop (H1–H2).
3. Save the Wokwi project under your account and share the link (H3).
4. Salt check and three settling runs (H4).
5. The outage tests (H5) and the one-week run (H6), keeping the records.
6. The mandi moisture-meter check (H7).

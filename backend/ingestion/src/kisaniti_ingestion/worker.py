"""MQTT worker: subscribe to all device telemetry, store each reading once, then acknowledge.

Run: KN_MQTT_HOST=... KN_MQTT_PORT=8883 KN_MQTT_TLS_CA=broker/certs/ca.crt KN_MQTT_USER=ingestion \
     KN_MQTT_PASSWORD=... KN_DEVICES=devices.json KN_STORE=readings.sqlite3 uv run kn-ingest
"""

from __future__ import annotations

import json
import logging
import ssl
import sys
from datetime import UTC, datetime
from typing import Any

import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion

from .config import Settings, load_registry
from .service import handle
from .store import open_store

TOPIC = "kisaniti/v1/devices/+/telemetry"
log = logging.getLogger("kn-ingest")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
    s = Settings.from_env()
    registry = load_registry(s.devices_file)
    store = open_store(s.store)
    client = mqtt.Client(CallbackAPIVersion.VERSION2, client_id="kisaniti-ingestion", clean_session=False)
    if s.mqtt_user:
        client.username_pw_set(s.mqtt_user, s.mqtt_password)
    if s.mqtt_tls_ca:
        client.tls_set(ca_certs=s.mqtt_tls_ca, tls_version=ssl.PROTOCOL_TLS_CLIENT)

    def on_connect(c: mqtt.Client, _u: Any, _f: Any, rc: Any, _p: Any) -> None:
        log.info("connected to %s:%s (%s); subscribing %s", s.mqtt_host, s.mqtt_port, rc, TOPIC)
        c.subscribe(TOPIC, qos=1)

    def on_message(c: mqtt.Client, _u: Any, msg: mqtt.MQTTMessage) -> None:
        out = handle(msg.topic, msg.payload, datetime.now(UTC), registry, store)
        log.info("%s %s -> %s", msg.topic, len(msg.payload), out.result)
        if out.ack_topic and out.ack:
            c.publish(out.ack_topic, json.dumps(out.ack, separators=(",", ":")), qos=1)

    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(s.mqtt_host, s.mqtt_port, keepalive=30)
    log.info("devices registered: %s; store: %s", ", ".join(sorted(registry)), s.store)
    client.loop_forever()


if __name__ == "__main__":
    main()

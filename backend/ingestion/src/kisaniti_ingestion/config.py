"""Settings from environment variables (no secrets in code). See backend/ingestion/README.md."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from .validate import Device


@dataclass(frozen=True, slots=True)
class Settings:
    mqtt_host: str
    mqtt_port: int
    mqtt_tls_ca: str | None
    mqtt_user: str | None
    mqtt_password: str | None
    devices_file: Path
    store: str

    @staticmethod
    def from_env() -> Settings:
        return Settings(
            mqtt_host=os.environ.get("KN_MQTT_HOST", "localhost"),
            mqtt_port=int(os.environ.get("KN_MQTT_PORT", "8883")),
            mqtt_tls_ca=os.environ.get("KN_MQTT_TLS_CA") or None,
            mqtt_user=os.environ.get("KN_MQTT_USER") or None,
            mqtt_password=os.environ.get("KN_MQTT_PASSWORD") or None,
            devices_file=Path(os.environ.get("KN_DEVICES", "devices.json")),
            store=os.environ.get("KN_STORE", "readings.sqlite3"),
        )


def load_registry(path: Path) -> dict[str, Device]:
    """devices.json: [{"device_id": "kn-node-001", "source_origin": "DEVICE"}, ...]"""
    items = json.loads(path.read_text())
    out: dict[str, Device] = {}
    for it in items:
        if it["source_origin"] not in ("DEVICE", "WOKWI_TWIN", "SIMULATOR"):
            raise ValueError(f"unknown source_origin for {it['device_id']}: {it['source_origin']}")
        out[it["device_id"]] = Device(it["device_id"], it["source_origin"])
    return out

"""Turn one MQTT telemetry message into a SensorReadingV1 document (contract §2–3).

Pure apart from reading the JSON Schemas once. The device registry, not the payload, decides the record's origin.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from kisaniti_core.timeutil import format_instant, parse_instant
from referencing import Registry, Resource

CONTRACTS = Path(__file__).resolve().parents[4] / "contracts"
TOPIC_RE = re.compile(r"^kisaniti/v1/devices/([a-z0-9][a-z0-9-]{2,31})/telemetry$")
FUTURE_TOLERANCE = timedelta(minutes=5)
TRUSTED_TIME = {"NTP_SYNCED", "UPTIME_BACKFILLED"}
T_VALID = (-10.0, 60.0)


@dataclass(frozen=True, slots=True)
class Device:
    """A registered device. source_origin is fixed at registration (DEVICE, WOKWI_TWIN or SIMULATOR)."""

    device_id: str
    source_origin: str


@dataclass(frozen=True, slots=True)
class Rejected:
    """A message that cannot become a SensorReadingV1 (kept as raw evidence)."""

    reason: str
    detail: str
    device_id: str | None
    boot_id: str | None
    seq: int | None
    raw_sha256: str


@cache
def _validator(name: str) -> Draft202012Validator:
    reg: Registry = Registry()
    for d in (CONTRACTS / "sensor" / "v1", CONTRACTS / "shared" / "v1"):
        for f in d.glob("*.schema.json"):
            doc = json.loads(f.read_text())
            reg = reg.with_resource(doc["$id"], Resource.from_contents(doc))
    sid = json.loads((CONTRACTS / "sensor" / "v1" / name).read_text())["$id"]
    return Draft202012Validator({"$ref": sid}, registry=reg)


def schema_errors(doc: Any, schema_file: str) -> list[str]:
    return [
        f"{'/'.join(str(p) for p in e.absolute_path)}: {e.message}" for e in _validator(schema_file).iter_errors(doc)
    ]


def _identity(doc: Any) -> tuple[str | None, str | None, int | None]:
    if not isinstance(doc, dict):
        return None, None, None
    dev, boot, seq = doc.get("device_id"), doc.get("boot_id"), doc.get("seq")
    return (
        dev if isinstance(dev, str) else None,
        boot if isinstance(boot, str) and re.fullmatch(r"[0-9a-f]{8}", boot) else None,
        seq if isinstance(seq, int) and not isinstance(seq, bool) and seq >= 0 else None,
    )


def _in_range(v: Any, lo: float, hi: float) -> bool:
    return isinstance(v, int | float) and not isinstance(v, bool) and lo <= v <= hi


def to_reading(
    topic: str, payload: bytes, received_at: datetime, registry: Mapping[str, Device]
) -> dict[str, Any] | Rejected:
    """Validate a message and build the stored reading, or say why it is rejected.

    Accepted and quarantined readings are both SensorReadingV1 documents; quarantined ones are kept but never used.
    """
    sha = hashlib.sha256(payload).hexdigest()
    try:
        doc = json.loads(payload)
    except (ValueError, UnicodeDecodeError) as e:
        return Rejected("NOT_JSON", str(e)[:200], None, None, None, sha)
    dev, boot, seq = _identity(doc)
    m = TOPIC_RE.match(topic)
    if not m:
        return Rejected("BAD_TOPIC", topic[:200], dev, boot, seq, sha)
    errors = schema_errors(doc, "device_telemetry.schema.json")
    if errors:
        return Rejected("SCHEMA_INVALID", "; ".join(errors)[:500], dev, boot, seq, sha)

    reason: str | None = None
    if m.group(1) != doc["device_id"]:
        reason = "TOPIC_DEVICE_MISMATCH"
    device = registry.get(doc["device_id"])
    if reason is None and device is None:
        reason = "UNREGISTERED_DEVICE"
    if reason is None and device is not None and device.source_origin != doc["source_origin"]:
        reason = "SOURCE_ORIGIN_MISMATCH"
    measured = parse_instant(doc["measured_at"]) if doc["measured_at"] is not None else None
    if reason is None and measured is not None and measured > received_at + FUTURE_TOLERANCE:
        reason = "FUTURE_TIMESTAMP"

    origin_kind = device.source_origin if device else doc["source_origin"]
    received = format_instant(received_at)
    provenance: dict[str, Any] = {
        "origin": "REAL" if origin_kind == "DEVICE" else "SYNTHETIC",
        "source_kind": "SENSOR" if origin_kind == "DEVICE" else "SIMULATOR",
        "source_ref": f"device {doc['device_id']} via {topic}",
        "acquired_at": received,
        "source_version": doc["firmware_version"],
    }
    if provenance["origin"] == "SYNTHETIC":
        provenance["generator"] = {
            "script": "Wokwi twin: firmware (env wokwi) + wokwi/chips/sht3x.chip.c"
            if origin_kind == "WOKWI_TWIN"
            else "sensor simulator",
            "version": doc["firmware_version"],
            "seed": 0,
        }
    reading = dict(doc)
    reading.update(
        {
            "schema": "kisaniti.sensor_reading.v1",
            "reading_id": f"{doc['device_id']}:{doc['boot_id']}:{doc['seq']}",
            "received_at": received,
            "measured_at_trusted": measured is not None
            and doc["time_quality"] in TRUSTED_TIME
            and measured <= received_at + FUTURE_TOLERANCE,
            "temperature_valid": _in_range(doc["temperature_c"], *T_VALID),
            "humidity_valid": _in_range(doc["humidity_pct"], 0.0, 100.0),
            "ingest_status": "QUARANTINED" if reason else "ACCEPTED",
            "quarantine_reason": reason,
            "raw_sha256": sha,
            "topic": topic,
            "provenance": provenance,
        }
    )
    return reading


def ack_for(boot_id: str, seq: int, status: str) -> dict[str, Any]:
    return {"schema": "kisaniti.device_ack.v1", "boot_id": boot_id, "seq": seq, "status": status}

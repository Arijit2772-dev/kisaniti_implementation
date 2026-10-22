"""Ingestion: validation, quarantine, de-duplication, durable store and acknowledgements (contract L3B v1 §2–3)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from kisaniti_ingestion.service import handle
from kisaniti_ingestion.store import open_store
from kisaniti_ingestion.validate import Device, schema_errors

EXAMPLES = Path(__file__).resolve().parents[3] / "contracts" / "sensor" / "v1" / "examples" / "valid"
RECEIVED = datetime(2026, 10, 22, 10, 5, 1, 204000, tzinfo=UTC)
REG = {"kn-node-001": Device("kn-node-001", "DEVICE"), "kn-twin-001": Device("kn-twin-001", "WOKWI_TWIN")}
TOPIC = "kisaniti/v1/devices/kn-node-001/telemetry"


def telemetry(**over: Any) -> dict[str, Any]:
    doc: dict[str, Any] = json.loads((EXAMPLES / "telemetry_ok.json").read_text())
    doc.update(over)
    return doc


def send(store: Any, doc: dict[str, Any] | bytes, topic: str = TOPIC, at: datetime = RECEIVED) -> Any:
    payload = doc if isinstance(doc, bytes) else json.dumps(doc).encode()
    return handle(topic, payload, at, REG, store)


@pytest.fixture
def store() -> Any:
    return open_store(":memory:")


def test_accepts_and_acks_after_store(store: Any) -> None:
    out = send(store, telemetry())
    assert out.result == "NEW"
    assert out.ack_topic == "kisaniti/v1/devices/kn-node-001/ack"
    assert out.ack == {"schema": "kisaniti.device_ack.v1", "boot_id": "3fa9c1e2", "seq": 42, "status": "ACCEPTED"}
    assert not schema_errors(out.ack, "device_ack.schema.json")
    doc = store.stored("kn-node-001:3fa9c1e2:42")
    assert doc is not None and not schema_errors(doc, "sensor_reading.schema.json")
    assert doc["provenance"]["origin"] == "REAL" and doc["measured_at_trusted"] is True
    assert doc["received_at"] == "2026-10-22T10:05:01.204Z"


def test_duplicate_and_resend_are_stored_once(store: Any) -> None:
    send(store, telemetry())
    again = send(store, telemetry(resent=True, dropped_since_boot=2), at=RECEIVED + timedelta(minutes=1))
    assert again.result == "DUPLICATE" and again.ack is not None and again.ack["status"] == "DUPLICATE"
    assert store.counts()["sensor_readings"] == 1
    assert store.counts()["sensor_reading_conflicts"] == 0


def test_same_identity_different_content_is_a_recorded_conflict(store: Any) -> None:
    send(store, telemetry())
    out = send(store, telemetry(humidity_pct=70.0))
    assert out.result == "CONFLICT"
    assert store.stored("kn-node-001:3fa9c1e2:42")["humidity_pct"] == 61.27  # first copy kept
    assert store.counts()["sensor_reading_conflicts"] == 1


def test_failed_read_is_stored_with_invalid_channels(store: Any) -> None:
    out = send(store, json.loads((EXAMPLES / "telemetry_read_failed.json").read_text()))
    doc = store.stored("kn-node-001:3fa9c1e2:43")
    assert out.ack is not None and out.ack["status"] == "ACCEPTED"
    assert doc["temperature_valid"] is False and doc["humidity_pct"] is None


def test_unsynced_reading_is_kept_but_untrusted(store: Any) -> None:
    send(store, json.loads((EXAMPLES / "telemetry_unsynced.json").read_text()))
    assert store.stored("kn-node-001:7c01d4aa:0")["measured_at_trusted"] is False


def test_out_of_plausible_range_marks_channel_invalid(store: Any) -> None:
    send(store, telemetry(temperature_c=85.0))
    doc = store.stored("kn-node-001:3fa9c1e2:42")
    assert doc["temperature_valid"] is False and doc["humidity_valid"] is True


@pytest.mark.parametrize(
    ("over", "topic", "reason", "acked"),
    [
        ({"device_id": "kn-node-001"}, "kisaniti/v1/devices/kn-twin-001/telemetry", "TOPIC_DEVICE_MISMATCH", False),
        ({"device_id": "kn-node-999"}, "kisaniti/v1/devices/kn-node-999/telemetry", "UNREGISTERED_DEVICE", False),
        ({"source_origin": "WOKWI_TWIN"}, TOPIC, "SOURCE_ORIGIN_MISMATCH", True),
        ({"measured_at": "2026-10-22T11:00:00.000Z"}, TOPIC, "FUTURE_TIMESTAMP", True),
    ],
)
def test_quarantine(store: Any, over: dict[str, Any], topic: str, reason: str, acked: bool) -> None:
    doc = telemetry(**over)
    out = send(store, doc, topic)
    stored = store.stored(f"{doc['device_id']}:3fa9c1e2:42")
    assert stored["ingest_status"] == "QUARANTINED" and stored["quarantine_reason"] == reason
    assert not schema_errors(stored, "sensor_reading.schema.json")
    assert (out.ack is not None) == acked
    if acked:
        assert out.ack["status"] == "QUARANTINED"


def test_schema_invalid_is_rejected_and_acked_so_device_stops_resending(store: Any) -> None:
    out = send(store, telemetry(temperature_c=0, humidity_pct=0, read_status="NO_ACK"))  # false zeros
    assert out.result == "REJECTED:SCHEMA_INVALID"
    assert out.ack is not None and out.ack["status"] == "QUARANTINED"
    assert store.counts() == {"sensor_readings": 0, "sensor_reading_conflicts": 0, "sensor_rejected_messages": 1}


@pytest.mark.parametrize("payload", [b"not json", b"[]", b'{"device_id": 5}'])
def test_garbage_is_rejected_without_ack(store: Any, payload: bytes) -> None:
    out = send(store, payload)
    assert out.result.startswith("REJECTED:") and out.ack is None
    assert store.counts()["sensor_rejected_messages"] == 1


def test_twin_reading_is_synthetic(store: Any) -> None:
    doc = telemetry(device_id="kn-twin-001", source_origin="WOKWI_TWIN", sample_interval_s=10)
    send(store, doc, "kisaniti/v1/devices/kn-twin-001/telemetry")
    stored = store.stored("kn-twin-001:3fa9c1e2:42")
    assert stored["provenance"]["origin"] == "SYNTHETIC" and stored["provenance"]["source_kind"] == "SIMULATOR"
    assert not schema_errors(stored, "sensor_reading.schema.json")


def test_store_survives_reopen(tmp_path: Path) -> None:
    path = str(tmp_path / "r.sqlite3")
    send(open_store(path), telemetry())
    s2 = open_store(path)
    assert s2.counts()["sensor_readings"] == 1
    assert send(s2, telemetry()).result == "DUPLICATE"

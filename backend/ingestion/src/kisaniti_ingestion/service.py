"""Message handling: validate → store → choose the acknowledgement (contract §2)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from kisaniti_core.timeutil import format_instant

from .store import ReadingStore, StoreResult
from .validate import Device, Rejected, ack_for, to_reading


@dataclass(frozen=True, slots=True)
class Outcome:
    result: str  # NEW, DUPLICATE, CONFLICT or REJECTED:<reason>
    ack_topic: str | None
    ack: dict[str, Any] | None


def handle(
    topic: str, payload: bytes, received_at: datetime, registry: Mapping[str, Device], store: ReadingStore
) -> Outcome:
    r = to_reading(topic, payload, received_at, registry)
    if isinstance(r, Rejected):
        store.reject(topic, payload, format_instant(received_at), r)
        # Acknowledge a permanently bad message (as QUARANTINED) when it can be addressed, so the device stops
        # resending it; the topic must name the same registered device to avoid acking on someone else's behalf.
        addressable = r.device_id in registry and topic == f"kisaniti/v1/devices/{r.device_id}/telemetry"
        if addressable and r.boot_id is not None and r.seq is not None:
            return Outcome(
                f"REJECTED:{r.reason}",
                f"kisaniti/v1/devices/{r.device_id}/ack",
                ack_for(r.boot_id, r.seq, "QUARANTINED"),
            )
        return Outcome(f"REJECTED:{r.reason}", None, None)
    stored = store.put(r)
    if r["quarantine_reason"] == "TOPIC_DEVICE_MISMATCH" or r["quarantine_reason"] == "UNREGISTERED_DEVICE":
        return Outcome(stored.value, None, None)  # never ack for an identity we do not trust
    status = (
        "DUPLICATE"
        if stored is not StoreResult.NEW
        else ("QUARANTINED" if r["ingest_status"] == "QUARANTINED" else "ACCEPTED")
    )
    return Outcome(stored.value, f"kisaniti/v1/devices/{r['device_id']}/ack", ack_for(r["boot_id"], r["seq"], status))

"""Conversion between the L3b contract JSON (contracts/sensor/v1) and the rule's types."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..provenance import Origin
from ..timeutil import format_instant, parse_instant
from .rule import EVIDENCE_REF, LABEL, POLICY_VERSION, WINDOW, Assessment, Assignment, Crop, Placement, Reading


def reading_from_contract(doc: Mapping[str, Any]) -> Reading:
    """A SensorReadingV1 document (as stored by ingestion) -> Reading."""
    measured = doc.get("measured_at")
    return Reading(
        measured_at=parse_instant(measured, "/measured_at") if measured is not None else None,
        temperature_c=doc["temperature_c"] if doc.get("temperature_valid") else None,
        humidity_pct=doc["humidity_pct"] if doc.get("humidity_valid") else None,
        measured_at_trusted=bool(doc["measured_at_trusted"]),
        accepted=doc["ingest_status"] == "ACCEPTED",
        origin=Origin(doc["provenance"]["origin"]),
    )


def assignment_from_contract(doc: Mapping[str, Any]) -> Assignment:
    return Assignment(
        device_id=doc["device_id"],
        placement=Placement(doc["placement"]),
        crop=Crop(doc["crop"]),
        assigned_at=parse_instant(doc["assigned_at"], "/assigned_at"),
        sample_interval_s=int(doc["sample_interval_s"]),
    )


def _r(v: float | None, digits: int) -> float | None:
    return None if v is None else round(v, digits)


def assessment_to_contract(a: Assessment) -> dict[str, Any]:
    """Assessment -> StorageAssessmentV1 document."""
    asg = a.assignment
    return {
        "schema": "kisaniti.storage_assessment.v1",
        "policy_version": POLICY_VERSION,
        "device_id": asg.device_id if asg else None,
        "crop": asg.crop.value if asg else None,
        "placement": asg.placement.value if asg else None,
        "assessed_at": format_instant(a.assessed_at),
        "window_start": format_instant(a.assessed_at - WINDOW),
        "window_end": format_instant(a.assessed_at),
        "category": a.category.value,
        "unknown_reasons": [r.value for r in a.unknown_reasons],
        "mould_level": a.mould_level.value if a.mould_level else None,
        "adjustments": [x.value for x in a.adjustments],
        "aw_median": a.aw_median,
        "rh_median_pct": _r(a.rh_median_pct, 2),
        "temperature_median_c": _r(a.temperature_median_c, 2),
        "insect_flag": a.insect_flag.value if a.insect_flag else None,
        "samples_used": a.samples_used,
        "samples_expected": a.samples_expected,
        "latest_measured_at": format_instant(a.latest_measured_at) if a.latest_measured_at else None,
        "hysteresis": {
            "level": a.state.level.value if a.state.level else None,
            "below_since": format_instant(a.state.below_since) if a.state.below_since else None,
        },
        "label": LABEL,
        "evidence_ref": EVIDENCE_REF,
        "provenance": _provenance(a),
    }


def _provenance(a: Assessment) -> dict[str, Any]:
    device = a.assignment.device_id if a.assignment else "no assignment"
    p: dict[str, Any] = {
        "origin": a.origin.value,
        "source_kind": "DERIVED",
        "source_ref": f"{POLICY_VERSION} over {a.samples_used} readings of {device}",
        "acquired_at": format_instant(a.assessed_at),
        "source_version": POLICY_VERSION,
    }
    if a.origin is Origin.SYNTHETIC:
        p["generator"] = {
            "script": "kisaniti_core.storage.rule (inputs include synthetic readings)",
            "version": POLICY_VERSION,
            "seed": 0,
        }
    return p

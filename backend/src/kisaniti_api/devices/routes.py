"""Endpoints of devices/v1 (L3B_DEVICE_REGISTRY_CONTRACT_v1.md §2). Every query runs in a tenant transaction."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, FastAPI, Query, Request, Response
from kisaniti_core.errors import ContractError, ErrorCode
from kisaniti_core.timeutil import format_instant, parse_instant
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Connection, text
from sqlalchemy.exc import IntegrityError

from ..access.deps import engine, require, tenant_scope
from ..access.errors import AccessError
from ..access.service import Principal, audit, auth_conn

router = APIRouter(prefix="/api/v1")
FUTURE_TOLERANCE = timedelta(minutes=5)
ReadData = Annotated[Principal, Depends(require("data.read"))]
WriteData = Annotated[Principal, Depends(require("data.write"))]
ManageDevices = Annotated[Principal, Depends(require("users.manage"))]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RegisterBody(Strict):
    device_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{2,31}$")
    source_origin: Literal["DEVICE", "WOKWI_TWIN", "SIMULATOR"]


class AssignBody(Strict):
    placement: Literal["IN_GRAIN", "AMBIENT"]
    crop: Literal["PADDY", "WHEAT"]
    assigned_at: str | None = None
    sample_interval_s: int = Field(default=300, ge=1, le=86400)


def assignment_json(row: Any) -> dict[str, Any]:
    return {
        "contract": "devices.assignment/v1",
        "device_id": row.device_id,
        "placement": row.placement,
        "crop": row.crop,
        "assigned_at": format_instant(row.assigned_at),
        "sample_interval_s": row.sample_interval_s,
        "created_at": format_instant(row.created_at),
    }


DEVICE_SQL = """
SELECT d.device_id, d.fpo_id, d.source_origin, d.active, d.registered_at,
       a.placement, a.crop, a.assigned_at, a.sample_interval_s, a.created_at AS a_created_at,
       (SELECT max(r.measured_at) FROM sensor_readings r
         WHERE r.device_id = d.device_id AND (r.doc->>'measured_at_trusted')::boolean) AS last_reading_at
FROM devices d
LEFT JOIN LATERAL (
    SELECT x.* FROM device_assignments x
    WHERE x.device_id = d.device_id AND x.assigned_at <= now() ORDER BY x.assigned_at DESC LIMIT 1
) a ON true
WHERE d.fpo_id = :f
"""


def device_json(row: Any) -> dict[str, Any]:
    current = None
    if row.placement is not None:
        current = {
            "contract": "devices.assignment/v1",
            "device_id": row.device_id,
            "placement": row.placement,
            "crop": row.crop,
            "assigned_at": format_instant(row.assigned_at),
            "sample_interval_s": row.sample_interval_s,
            "created_at": format_instant(row.a_created_at),
        }
    return {
        "contract": "devices.device/v1",
        "device_id": row.device_id,
        "fpo_id": row.fpo_id,
        "source_origin": row.source_origin,
        "active": row.active,
        "registered_at": format_instant(row.registered_at),
        "current_assignment": current,
        "last_reading_at": format_instant(row.last_reading_at) if row.last_reading_at else None,
    }


def reading_view(doc: dict[str, Any]) -> dict[str, Any]:
    """DeviceReadingV1: what a tenant may see of a stored reading (no payload hash, no topic, no raw data)."""
    return {
        "contract": "devices.reading/v1",
        "reading_id": doc["reading_id"],
        "measured_at": doc["measured_at"],
        "measured_at_trusted": doc["measured_at_trusted"],
        "received_at": doc["received_at"],
        "temperature_c": doc["temperature_c"],
        "humidity_pct": doc["humidity_pct"],
        "read_status": doc["read_status"],
        "ingest_status": doc["ingest_status"],
        "origin": doc["provenance"]["origin"],
    }


def _own_device(conn: Connection, p: Principal, device_id: str) -> Any:
    return conn.execute(text(DEVICE_SQL + " AND d.device_id = :d"), {"f": p.fpo_id, "d": device_id}).first()


def _missing_device(request: Request, p: Principal, device_id: str) -> AccessError:
    """404 for a missing device and for another FPO's device alike; the second case is audited."""
    with auth_conn(engine()) as conn:
        elsewhere = conn.execute(
            text("SELECT 1 FROM devices WHERE device_id = :d AND fpo_id <> :f"), {"d": device_id, "f": p.fpo_id}
        ).first()
        if elsewhere:
            audit(conn, request.state.request_id, "CROSS_TENANT_REFERENCE", actor=p.user_id, fpo_id=p.fpo_id)
    return AccessError(ErrorCode.NOT_FOUND, "device not found")


@router.get("/devices")
def list_devices(request: Request, p: ReadData) -> list[dict[str, Any]]:
    with tenant_scope(request, p) as conn:
        rows = conn.execute(text(DEVICE_SQL + " ORDER BY d.device_id"), {"f": p.fpo_id}).all()
    return [device_json(r) for r in rows]


@router.post("/devices", status_code=201)
def register_device(body: RegisterBody, request: Request, p: ManageDevices) -> dict[str, Any]:
    taken = False
    with tenant_scope(request, p) as conn:
        try:
            with conn.begin_nested():
                conn.execute(
                    text("INSERT INTO devices (device_id, fpo_id, source_origin) VALUES (:d, :f, :o)"),
                    {"d": body.device_id, "f": p.fpo_id, "o": body.source_origin},
                )
        except IntegrityError:
            taken = True
        if not taken:
            row = _own_device(conn, p, body.device_id)
    if taken:
        raise AccessError(ErrorCode.CONFLICT, "that device ID is already registered")
    return device_json(row)


@router.get("/devices/{device_id}")
def get_device(device_id: str, request: Request, p: ReadData) -> dict[str, Any]:
    with tenant_scope(request, p) as conn:
        row = _own_device(conn, p, device_id)
    if row is None:
        raise _missing_device(request, p, device_id)
    return device_json(row)


@router.post("/devices/{device_id}/assignments", status_code=201)
def assign_device(device_id: str, body: AssignBody, request: Request, p: WriteData) -> dict[str, Any]:
    try:
        when = parse_instant(body.assigned_at) if body.assigned_at is not None else datetime.now(UTC)
    except ContractError as e:
        raise AccessError(ErrorCode.VALIDATION_FAILED, e.message, "INVALID_INSTANT", "/assigned_at") from e
    if when > datetime.now(UTC) + FUTURE_TOLERANCE:
        raise AccessError(
            ErrorCode.VALIDATION_FAILED, "assigned_at lies in the future", "ASSIGNED_AT_IN_FUTURE", "/assigned_at"
        )
    outcome = "ok"
    with tenant_scope(request, p) as conn:
        dev = _own_device(conn, p, device_id)
        if dev is None:
            outcome = "missing"
        elif not dev.active:
            outcome = "inactive"
        else:
            try:
                with conn.begin_nested():
                    row = conn.execute(
                        text(
                            "INSERT INTO device_assignments (device_id, fpo_id, placement, crop, assigned_at, "
                            "sample_interval_s, created_by) VALUES (:d, :f, :pl, :c, :t, :s, :u) "
                            "RETURNING device_id, placement, crop, assigned_at, sample_interval_s, created_at"
                        ),
                        {
                            "d": device_id,
                            "f": p.fpo_id,
                            "pl": body.placement,
                            "c": body.crop,
                            "t": when,
                            "s": body.sample_interval_s,
                            "u": p.user_id,
                        },
                    ).one()
            except IntegrityError:
                outcome = "duplicate"
    if outcome == "missing":
        raise _missing_device(request, p, device_id)
    if outcome == "inactive":
        raise AccessError(ErrorCode.CONFLICT, "the device is deactivated")
    if outcome == "duplicate":
        raise AccessError(ErrorCode.CONFLICT, "this device already has an assignment at that time")
    return assignment_json(row)


@router.get("/devices/{device_id}/assignments")
def list_assignments(device_id: str, request: Request, p: ReadData) -> list[dict[str, Any]]:
    with tenant_scope(request, p) as conn:
        found = _own_device(conn, p, device_id) is not None
        rows = conn.execute(
            text(
                "SELECT device_id, placement, crop, assigned_at, sample_interval_s, created_at "
                "FROM device_assignments WHERE device_id = :d AND fpo_id = :f ORDER BY assigned_at DESC"
            ),
            {"d": device_id, "f": p.fpo_id},
        ).all()
    if not found:
        raise _missing_device(request, p, device_id)
    return [assignment_json(r) for r in rows]


@router.get("/devices/{device_id}/readings")
def device_readings(
    device_id: str, request: Request, p: ReadData, limit: Annotated[int, Query(ge=1, le=1000)] = 100
) -> list[dict[str, Any]]:
    with tenant_scope(request, p) as conn:
        found = _own_device(conn, p, device_id) is not None
        rows = conn.execute(
            text(
                "SELECT doc FROM sensor_readings WHERE device_id = :d "
                "ORDER BY measured_at DESC NULLS LAST, received_at DESC LIMIT :n"
            ),
            {"d": device_id, "n": limit},
        ).all()
    if not found:
        raise _missing_device(request, p, device_id)
    return [reading_view(r.doc) for r in rows]


@router.get("/devices/{device_id}/assessment")
def latest_assessment(device_id: str, request: Request, p: ReadData) -> dict[str, Any]:
    """The latest stored StorageAssessmentV1 (rule STORAGE-DEMO-0.1) of the FPO's own device."""
    with tenant_scope(request, p) as conn:
        found = _own_device(conn, p, device_id) is not None
        row = conn.execute(
            text("SELECT doc FROM storage_assessments WHERE device_id = :d ORDER BY assessed_at DESC LIMIT 1"),
            {"d": device_id},
        ).first()
    if not found:
        raise _missing_device(request, p, device_id)
    if row is None:
        raise AccessError(ErrorCode.NOT_FOUND, "no assessment yet")
    return dict(row.doc)


@router.get("/devices/{device_id}/assessments")
def assessments(
    device_id: str, request: Request, p: ReadData, limit: Annotated[int, Query(ge=1, le=1000)] = 48
) -> list[dict[str, Any]]:
    with tenant_scope(request, p) as conn:
        found = _own_device(conn, p, device_id) is not None
        rows = conn.execute(
            text("SELECT doc FROM storage_assessments WHERE device_id = :d ORDER BY assessed_at DESC LIMIT :n"),
            {"d": device_id, "n": limit},
        ).all()
    if not found:
        raise _missing_device(request, p, device_id)
    return [dict(r.doc) for r in rows]


@router.post("/devices/{device_id}/deactivate", status_code=204)
def deactivate_device(device_id: str, request: Request, p: ManageDevices) -> Response:
    with tenant_scope(request, p) as conn:
        found = _own_device(conn, p, device_id) is not None
        if found:
            conn.execute(
                text("UPDATE devices SET active = false WHERE device_id = :d AND fpo_id = :f"),
                {"d": device_id, "f": p.fpo_id},
            )
    if not found:
        raise _missing_device(request, p, device_id)
    return Response(status_code=204)


def install(app: FastAPI) -> None:
    app.include_router(router)

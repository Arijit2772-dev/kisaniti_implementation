"""Endpoints of org/v1 (L1_L2_ORG_AND_MARKET_CONTRACT_v1.md §A). Every query runs in a tenant transaction."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, FastAPI, Header, Query, Request
from kisaniti_core.errors import ContractError, ErrorCode
from kisaniti_core.ids import new_id
from kisaniti_core.quantity import Quantity, QuantityDelta
from kisaniti_core.timeutil import format_instant, parse_instant
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Connection, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from ..access.deps import engine, require, tenant_scope
from ..access.errors import AccessError
from ..access.service import Principal, audit, auth_conn

router = APIRouter(prefix="/api/v1")
FUTURE_TOLERANCE = timedelta(minutes=5)
Read = Annotated[Principal, Depends(require("data.read"))]
Write = Annotated[Principal, Depends(require("data.write"))]
IdemKey = Annotated[str | None, Header(alias="Idempotency-Key", min_length=8, max_length=120)]
Language = Literal["pa", "hi", "en"]
Limit = Annotated[int, Query(ge=1, le=1000)]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MemberBody(Strict):
    display_name: str = Field(min_length=1, max_length=120)
    village: str | None = Field(default=None, max_length=100)
    preferred_language: Language


class MemberPatch(Strict):
    display_name: str | None = Field(default=None, min_length=1, max_length=120)
    village: str | None = Field(default=None, max_length=100)
    preferred_language: Language | None = None
    active: bool | None = None


class LocationBody(Strict):
    name: str = Field(min_length=1, max_length=120)
    kind: Literal["COLLECTION_CENTRE", "FARM", "WAREHOUSE", "THIRD_PARTY"]
    village: str | None = Field(default=None, max_length=100)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)


class LotBody(Strict):
    member_id: str = Field(pattern=r"^mbr_[0-9A-HJKMNP-TV-Z]{26}$")
    commodity_id: str = Field(pattern=r"^cmd_[0-9A-HJKMNP-TV-Z]{26}$")
    source_variety: str | None = Field(default=None, max_length=60)
    source_grade: str | None = Field(default=None, max_length=60)
    registered_quantity_kg: str
    harvested_on: date | None = None
    location_id: str | None = Field(default=None, pattern=r"^stl_[0-9A-HJKMNP-TV-Z]{26}$")
    quantity_verified: bool = False


class PlacementBody(Strict):
    location_id: str = Field(pattern=r"^stl_[0-9A-HJKMNP-TV-Z]{26}$")
    placed_at: str | None = None


class AdjustBody(Strict):
    quantity_delta_kg: str
    reason_code: Literal["WEIGHING_CORRECTION", "SPILLAGE", "MOISTURE_LOSS", "OTHER"]


def _bad(e: ContractError, field: str) -> AccessError:
    return AccessError(ErrorCode.VALIDATION_FAILED, e.message, e.code.value, f"/{field}")


def _missing(request: Request, p: Principal, table: str, row_id: str, label: str) -> AccessError:
    """404 for a missing reference and for another FPO's, alike; the second case is audited."""
    assert table in {"members", "locations", "lots"}
    with auth_conn(engine()) as conn:
        elsewhere = conn.execute(
            text(f"SELECT 1 FROM {table} WHERE id = :i AND fpo_id <> :f"),
            {"i": row_id, "f": p.fpo_id},
        ).first()
        if elsewhere:
            audit(conn, request.state.request_id, "CROSS_TENANT_REFERENCE", actor=p.user_id, fpo_id=p.fpo_id)
    return AccessError(ErrorCode.NOT_FOUND, f"{label} not found")


def _stock_refusal(e: DBAPIError) -> AccessError | None:
    """The database refuses an event that would break a stock invariant (SQLSTATE 23514 from the trigger)."""
    orig = e.orig
    if getattr(orig, "sqlstate", None) == "23514" and "stock invariant" in str(orig):
        msg = str(orig).split("stock invariant:")[-1].strip().splitlines()[0]
        return AccessError(ErrorCode.CONFLICT, f"refused: {msg}", "STOCK_INVARIANT")
    return None


def _kg(grams: int) -> str:
    return Quantity(grams).to_json()


def member_json(r: Any) -> dict[str, Any]:
    return {
        "contract": "org.member/v1",
        "id": r.id,
        "fpo_id": r.fpo_id,
        "display_name": r.display_name,
        "village": r.village,
        "preferred_language": r.preferred_language,
        "active": r.active,
        "origin": r.origin,
        "created_at": format_instant(r.created_at),
    }


def location_json(r: Any) -> dict[str, Any]:
    return {
        "contract": "org.location/v1",
        "id": r.id,
        "fpo_id": r.fpo_id,
        "name": r.name,
        "kind": r.kind,
        "village": r.village,
        "latitude": r.latitude,
        "longitude": r.longitude,
        "created_at": format_instant(r.created_at),
    }


# clock_timestamp(), not now(): now() is the start of the transaction, and a placement stamped a moment later would look
# like it lies in the future.
LOT_SQL = """
SELECT l.id, l.fpo_id, l.member_id, l.commodity_id, l.source_variety, l.source_grade, l.registered_grams,
       l.harvested_on,
       l.verified_by, l.verified_at, l.origin, l.created_at, b.on_hand_g, b.reserved_g, b.sold_g,
       (SELECT p.location_id FROM lot_placements p WHERE p.lot_id = l.id AND p.placed_at <= clock_timestamp()
         ORDER BY p.placed_at DESC LIMIT 1) AS current_location_id
FROM lots l JOIN lot_balances b ON b.lot_id = l.id
WHERE l.fpo_id = :f
"""


def lot_json(r: Any) -> dict[str, Any]:
    return {
        "contract": "org.lot/v1",
        "id": r.id,
        "fpo_id": r.fpo_id,
        "member_id": r.member_id,
        "commodity_id": r.commodity_id,
        "source_variety": r.source_variety,
        "source_grade": r.source_grade,
        "registered_quantity_kg": _kg(r.registered_grams),
        "harvested_on": r.harvested_on.isoformat() if r.harvested_on else None,
        "current_location_id": r.current_location_id,
        "balance": {
            "on_hand_kg": _kg(r.on_hand_g),
            "reserved_kg": _kg(r.reserved_g),
            "available_kg": _kg(r.on_hand_g - r.reserved_g),
            "sold_kg": _kg(r.sold_g),
        },
        "verified_by": r.verified_by,
        "verified_at": format_instant(r.verified_at) if r.verified_at else None,
        "origin": r.origin,
        "created_at": format_instant(r.created_at),
    }


def _idempotency(
    conn: Connection, p: Principal, key: str | None, body: dict[str, Any], scope: str
) -> tuple[str | None, Any]:
    """Returns (request hash, an earlier event row for this key or None). Same key and body: replay. Else 409."""
    if key is None:
        return None, None
    sha = hashlib.sha256(json.dumps([scope, body], sort_keys=True, default=str).encode()).hexdigest()
    row = conn.execute(
        text("SELECT id, lot_id, request_sha256 FROM stock_events WHERE fpo_id = :f AND request_key = :k"),
        {"f": p.fpo_id, "k": key},
    ).first()
    if row is not None and row.request_sha256 != sha:
        raise AccessError(ErrorCode.IDEMPOTENCY_KEY_REUSED, "this Idempotency-Key was used for a different request")
    return sha, row


# ---- members -----------------------------------------------------------------------------------------------------
@router.get("/members")
def list_members(request: Request, p: Read, limit: Limit = 200) -> list[dict[str, Any]]:
    with tenant_scope(request, p) as conn:
        rows = conn.execute(
            text("SELECT * FROM members WHERE fpo_id = :f ORDER BY display_name, id LIMIT :n"),
            {"f": p.fpo_id, "n": limit},
        ).all()
    return [member_json(r) for r in rows]


@router.post("/members", status_code=201)
def create_member(body: MemberBody, request: Request, p: Write) -> dict[str, Any]:
    with tenant_scope(request, p) as conn:
        origin = conn.execute(text("SELECT origin FROM users WHERE id = :u"), {"u": p.user_id}).scalar_one()
        row = conn.execute(
            text(
                "INSERT INTO members (id, fpo_id, display_name, village, preferred_language, origin) "
                "VALUES (:i, :f, :n, :v, :l, :o) RETURNING *"
            ),
            {
                "i": new_id("mbr"),
                "f": p.fpo_id,
                "n": body.display_name,
                "v": body.village,
                "l": body.preferred_language,
                "o": origin,
            },
        ).one()
    return member_json(row)


@router.get("/members/{member_id}")
def get_member(member_id: str, request: Request, p: Read) -> dict[str, Any]:
    with tenant_scope(request, p) as conn:
        row = conn.execute(
            text("SELECT * FROM members WHERE id = :i AND fpo_id = :f"), {"i": member_id, "f": p.fpo_id}
        ).first()
    if row is None:
        raise _missing(request, p, "members", member_id, "member")
    return member_json(row)


@router.patch("/members/{member_id}")
def patch_member(member_id: str, body: MemberPatch, request: Request, p: Write) -> dict[str, Any]:
    changes = body.model_dump(exclude_unset=True)
    if not changes:
        raise AccessError(ErrorCode.VALIDATION_FAILED, "nothing to change", "EMPTY_PATCH")
    sets = ", ".join(f"{k} = :{k}" for k in changes)
    with tenant_scope(request, p) as conn:
        row = conn.execute(
            text(f"UPDATE members SET {sets} WHERE id = :i AND fpo_id = :f RETURNING *"),
            {**changes, "i": member_id, "f": p.fpo_id},
        ).first()
    if row is None:
        raise _missing(request, p, "members", member_id, "member")
    return member_json(row)


# ---- locations -----------------------------------------------------------------------------------------------------
@router.get("/locations")
def list_locations(request: Request, p: Read, limit: Limit = 200) -> list[dict[str, Any]]:
    with tenant_scope(request, p) as conn:
        rows = conn.execute(
            text("SELECT * FROM locations WHERE fpo_id = :f ORDER BY name, id LIMIT :n"), {"f": p.fpo_id, "n": limit}
        ).all()
    return [location_json(r) for r in rows]


@router.post("/locations", status_code=201)
def create_location(body: LocationBody, request: Request, p: Write) -> dict[str, Any]:
    if (body.latitude is None) != (body.longitude is None):
        raise AccessError(
            ErrorCode.VALIDATION_FAILED, "give both latitude and longitude, or neither", "COORDINATES_INCOMPLETE"
        )
    with tenant_scope(request, p) as conn:
        row = conn.execute(
            text(
                "INSERT INTO locations (id, fpo_id, name, kind, village, latitude, longitude) "
                "VALUES (:i, :f, :n, :k, :v, :la, :lo) RETURNING *"
            ),
            {
                "i": new_id("stl"),
                "f": p.fpo_id,
                "n": body.name,
                "k": body.kind,
                "v": body.village,
                "la": body.latitude,
                "lo": body.longitude,
            },
        ).one()
    return location_json(row)


@router.get("/locations/{location_id}")
def get_location(location_id: str, request: Request, p: Read) -> dict[str, Any]:
    with tenant_scope(request, p) as conn:
        row = conn.execute(
            text("SELECT * FROM locations WHERE id = :i AND fpo_id = :f"), {"i": location_id, "f": p.fpo_id}
        ).first()
    if row is None:
        raise _missing(request, p, "locations", location_id, "location")
    return location_json(row)


# ---- lots ------------------------------------------------------------------------------------------------------------
def _read_lot(conn: Connection, p: Principal, lot_id: str) -> Any:
    return conn.execute(text(LOT_SQL + " AND l.id = :i"), {"f": p.fpo_id, "i": lot_id}).first()


@router.get("/lots")
def list_lots(request: Request, p: Read, limit: Limit = 200, member_id: str | None = None) -> list[dict[str, Any]]:
    sql = LOT_SQL + (" AND l.member_id = :m" if member_id else "") + " ORDER BY l.created_at DESC, l.id LIMIT :n"
    with tenant_scope(request, p) as conn:
        rows = conn.execute(text(sql), {"f": p.fpo_id, "n": limit, **({"m": member_id} if member_id else {})}).all()
    return [lot_json(r) for r in rows]


@router.get("/lots/{lot_id}")
def get_lot(lot_id: str, request: Request, p: Read) -> dict[str, Any]:
    with tenant_scope(request, p) as conn:
        row = _read_lot(conn, p, lot_id)
    if row is None:
        raise _missing(request, p, "lots", lot_id, "lot")
    return lot_json(row)


@router.post("/lots", status_code=201)
def create_lot(body: LotBody, request: Request, p: Write, idempotency_key: IdemKey = None) -> dict[str, Any]:
    try:
        qty = Quantity.parse(body.registered_quantity_kg, "/registered_quantity_kg")
    except ContractError as e:
        raise _bad(e, "registered_quantity_kg") from e
    if qty.units <= 0:
        raise AccessError(
            ErrorCode.VALIDATION_FAILED,
            "the quantity must be positive",
            "QUANTITY_NOT_POSITIVE",
            "/registered_quantity_kg",
        )
    lot_id, refusal, missing = new_id("lot"), None, None
    with tenant_scope(request, p) as conn:
        sha, earlier = _idempotency(conn, p, idempotency_key, body.model_dump(mode="json"), "POST /lots")
        if earlier is not None:
            return lot_json(_read_lot(conn, p, earlier.lot_id))
        member = conn.execute(
            text("SELECT origin FROM members WHERE id = :i AND fpo_id = :f"), {"i": body.member_id, "f": p.fpo_id}
        ).first()
        known_cmd = conn.execute(text("SELECT 1 FROM commodities WHERE id = :i"), {"i": body.commodity_id}).first()
        place = None
        if body.location_id:
            place = conn.execute(
                text("SELECT 1 FROM locations WHERE id = :i AND fpo_id = :f"), {"i": body.location_id, "f": p.fpo_id}
            ).first()
        if member is None:
            missing = ("members", body.member_id, "member")
        elif known_cmd is None:
            raise AccessError(ErrorCode.VALIDATION_FAILED, "unknown commodity", "UNKNOWN_COMMODITY", "/commodity_id")
        elif body.location_id and place is None:
            missing = ("locations", body.location_id, "location")
        else:
            now = datetime.now(UTC)
            conn.execute(
                text(
                    "INSERT INTO lots (id, fpo_id, member_id, commodity_id, source_variety, source_grade, "
                    "registered_grams, harvested_on, verified_by, verified_at, origin) "
                    "VALUES (:i, :f, :m, :c, :v, :g, :q, :h, :vb, :va, :o)"
                ),
                {
                    "i": lot_id,
                    "f": p.fpo_id,
                    "m": body.member_id,
                    "c": body.commodity_id,
                    "v": body.source_variety,
                    "g": body.source_grade,
                    "q": qty.units,
                    "h": body.harvested_on,
                    "vb": p.user_id if body.quantity_verified else None,
                    "va": now if body.quantity_verified else None,
                    "o": member.origin,
                },
            )
            try:
                with conn.begin_nested():
                    conn.execute(
                        text(
                            "INSERT INTO stock_events (lot_id, fpo_id, kind, quantity_g, request_key, "
                            "request_sha256, created_by) "
                            "VALUES (:l, :f, 'REGISTERED', :q, :k, :s, :u)"
                        ),
                        {"l": lot_id, "f": p.fpo_id, "q": qty.units, "k": idempotency_key, "s": sha, "u": p.user_id},
                    )
            except DBAPIError as e:
                refusal = _stock_refusal(e) or e
            if body.location_id and refusal is None:
                conn.execute(
                    text(
                        "INSERT INTO lot_placements (lot_id, fpo_id, location_id, placed_at, created_by) "
                        "VALUES (:l, :f, :s, clock_timestamp(), :u)"  # the database's clock, which reads compare with
                    ),
                    {"l": lot_id, "f": p.fpo_id, "s": body.location_id, "u": p.user_id},
                )
        row = _read_lot(conn, p, lot_id) if missing is None and refusal is None else None
    if missing:
        raise _missing(request, p, *missing)
    if refusal is not None:
        raise refusal
    return lot_json(row)


# ---- placements ----------------------------------------------------------------------------------------------------
@router.post("/lots/{lot_id}/placements", status_code=201)
def place_lot(lot_id: str, body: PlacementBody, request: Request, p: Write) -> dict[str, Any]:
    try:
        when = parse_instant(body.placed_at) if body.placed_at else None  # None: the database stamps it
    except ContractError as e:
        raise _bad(e, "placed_at") from e
    if when is not None and when > datetime.now(UTC) + FUTURE_TOLERANCE:
        raise AccessError(
            ErrorCode.VALIDATION_FAILED, "placed_at lies in the future", "PLACED_AT_IN_FUTURE", "/placed_at"
        )
    missing, duplicate, row = None, False, None
    with tenant_scope(request, p) as conn:
        if (
            conn.execute(text("SELECT 1 FROM lots WHERE id = :i AND fpo_id = :f"), {"i": lot_id, "f": p.fpo_id}).first()
            is None
        ):
            missing = ("lots", lot_id, "lot")
        elif (
            conn.execute(
                text("SELECT 1 FROM locations WHERE id = :i AND fpo_id = :f"), {"i": body.location_id, "f": p.fpo_id}
            ).first()
            is None
        ):
            missing = ("locations", body.location_id, "location")
        else:
            try:
                with conn.begin_nested():
                    row = conn.execute(
                        text(
                            "INSERT INTO lot_placements (lot_id, fpo_id, location_id, placed_at, created_by) "
                            "VALUES (:l, :f, :s, COALESCE(:t, clock_timestamp()), :u) "
                            "RETURNING lot_id, location_id, placed_at, created_at"
                        ),
                        {"l": lot_id, "f": p.fpo_id, "s": body.location_id, "t": when, "u": p.user_id},
                    ).one()
            except IntegrityError:
                duplicate = True
    if missing:
        raise _missing(request, p, *missing)
    if duplicate:
        raise AccessError(ErrorCode.CONFLICT, "this lot already has a placement at that time")
    return _placement_json(row)


def _placement_json(r: Any) -> dict[str, Any]:
    return {
        "contract": "org.placement/v1",
        "lot_id": r.lot_id,
        "location_id": r.location_id,
        "placed_at": format_instant(r.placed_at),
        "created_at": format_instant(r.created_at),
    }


@router.get("/lots/{lot_id}/placements")
def lot_placements(lot_id: str, request: Request, p: Read) -> list[dict[str, Any]]:
    with tenant_scope(request, p) as conn:
        found = conn.execute(
            text("SELECT 1 FROM lots WHERE id = :i AND fpo_id = :f"), {"i": lot_id, "f": p.fpo_id}
        ).first()
        rows = conn.execute(
            text(
                "SELECT lot_id, location_id, placed_at, created_at FROM lot_placements "
                "WHERE lot_id = :l AND fpo_id = :f "
                "ORDER BY placed_at DESC"
            ),
            {"l": lot_id, "f": p.fpo_id},
        ).all()
    if not found:
        raise _missing(request, p, "lots", lot_id, "lot")
    return [_placement_json(r) for r in rows]


# ---- ledger --------------------------------------------------------------------------------------------------------
def event_json(r: Any) -> dict[str, Any]:
    adj = r.kind == "ADJUSTED"
    return {
        "contract": "org.stock_event/v1",
        "id": r.id,
        "lot_id": r.lot_id,
        "kind": r.kind,
        "quantity_kg": None if adj else _kg(r.quantity_g),
        "quantity_delta_kg": QuantityDelta(r.quantity_g).to_json() if adj else None,
        "reason_code": r.reason_code,
        "created_by": r.created_by,
        "created_at": format_instant(r.created_at),
    }


@router.post("/lots/{lot_id}/adjustments", status_code=201)
def adjust_lot(
    lot_id: str, body: AdjustBody, request: Request, p: Write, idempotency_key: IdemKey = None
) -> dict[str, Any]:
    try:
        delta = QuantityDelta.parse(body.quantity_delta_kg, "/quantity_delta_kg")
    except ContractError as e:
        raise _bad(e, "quantity_delta_kg") from e
    if delta.units == 0:
        raise AccessError(
            ErrorCode.VALIDATION_FAILED, "an adjustment cannot be zero", "ADJUSTMENT_ZERO", "/quantity_delta_kg"
        )
    missing, refusal, row = None, None, None
    with tenant_scope(request, p) as conn:
        sha, earlier = _idempotency(
            conn, p, idempotency_key, {"lot": lot_id, **body.model_dump()}, "POST /lots/adjustments"
        )
        if earlier is not None:
            return event_json(conn.execute(text("SELECT * FROM stock_events WHERE id = :i"), {"i": earlier.id}).one())
        if (
            conn.execute(text("SELECT 1 FROM lots WHERE id = :i AND fpo_id = :f"), {"i": lot_id, "f": p.fpo_id}).first()
            is None
        ):
            missing = ("lots", lot_id, "lot")
        else:
            try:
                with conn.begin_nested():
                    row = conn.execute(
                        text(
                            "INSERT INTO stock_events (lot_id, fpo_id, kind, quantity_g, reason_code, request_key, "
                            "request_sha256, "
                            "created_by) VALUES (:l, :f, 'ADJUSTED', :q, :r, :k, :s, :u) RETURNING *"
                        ),
                        {
                            "l": lot_id,
                            "f": p.fpo_id,
                            "q": delta.units,
                            "r": body.reason_code,
                            "k": idempotency_key,
                            "s": sha,
                            "u": p.user_id,
                        },
                    ).one()
            except DBAPIError as e:
                refusal = _stock_refusal(e)
                if refusal is None:
                    raise
    if missing:
        raise _missing(request, p, *missing)
    if refusal is not None:
        raise refusal
    return event_json(row)


@router.get("/lots/{lot_id}/events")
def lot_events(lot_id: str, request: Request, p: Read, limit: Limit = 200) -> list[dict[str, Any]]:
    with tenant_scope(request, p) as conn:
        found = conn.execute(
            text("SELECT 1 FROM lots WHERE id = :i AND fpo_id = :f"), {"i": lot_id, "f": p.fpo_id}
        ).first()
        rows = conn.execute(
            text("SELECT * FROM stock_events WHERE lot_id = :l AND fpo_id = :f ORDER BY id DESC LIMIT :n"),
            {"l": lot_id, "f": p.fpo_id, "n": limit},
        ).all()
    if not found:
        raise _missing(request, p, "lots", lot_id, "lot")
    return [event_json(r) for r in rows]


def install(app: FastAPI) -> None:
    app.include_router(router)

"""Endpoints of plan/v1 (L3_PLAN_CONTRACT_v1.md): forecasts, lot-to-sensor links, plan proposals.

The proposal is built by ``builder.py`` from inputs loaded here inside the caller's tenant transaction (explicit FPO
filters and PostgreSQL row-level security). The optimiser runs between two short transactions, not inside one."""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, FastAPI, Header, Query, Request
from kisaniti_core.errors import ContractError, ErrorCode
from kisaniti_core.forecast import (
    HORIZONS,
    VARIETIES,
    Basis,
    IndexPoint,
    InsufficientHistory,
    Obs,
    baseline_forecast,
    build_index,
    mandi_basis,
    milli_from_decimal,
)
from kisaniti_core.ids import new_id
from kisaniti_core.quantity import Quantity
from kisaniti_core.timeutil import format_instant
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Connection, text
from sqlalchemy.exc import IntegrityError

from ..access.deps import engine, require, tenant_scope
from ..access.errors import AccessError
from ..access.service import Principal, audit, auth_conn
from ..config import load_settings
from .builder import (
    LotInput,
    PlanRefusal,
    SensorState,
    build_plan_document,
    forecast_set_doc,
    summary_of,
)

router = APIRouter(prefix="/api/v1")
Read = Annotated[Principal, Depends(require("data.read"))]
Write = Annotated[Principal, Depends(require("data.write"))]
IdemKey = Annotated[str | None, Header(alias="Idempotency-Key", min_length=8, max_length=120)]
TRANSPORT_RE = re.compile(r"^(0|[1-9][0-9]{0,5})\.[0-9]{2}$")
IST = ZoneInfo("Asia/Kolkata")


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SensorLinkBody(Strict):
    device_id: str | None = Field(pattern=r"^[a-z0-9][a-z0-9-]{2,31}$")


class PlanMandiBody(Strict):
    mandi_id: str = Field(pattern=r"^mnd_[0-9A-HJKMNP-TV-Z]{26}$")
    transport_inr_per_quintal: str


class PlanBody(Strict):
    variety: str
    horizons_days: list[int] = Field(max_length=10)
    mandis: list[PlanMandiBody] = Field(max_length=10)
    increment_kg: str


def _refuse(issue: str, message: str, path: str | None = None) -> AccessError:
    return AccessError(ErrorCode.VALIDATION_FAILED, message, issue, path)


def _validate(body: PlanBody) -> dict[str, Any]:
    if body.variety not in VARIETIES:
        raise _refuse("VARIETY_NOT_FORECAST", f"only {', '.join(VARIETIES)} are forecast", "/variety")
    if not body.horizons_days or len(set(body.horizons_days)) != len(body.horizons_days):
        raise _refuse("HORIZON_NOT_ALLOWED", "give each horizon once, at least one", "/horizons_days")
    if any(h not in HORIZONS for h in body.horizons_days):
        raise _refuse("HORIZON_NOT_ALLOWED", f"horizons must be among {list(HORIZONS)}", "/horizons_days")
    if not body.mandis:
        raise _refuse("NO_MANDI_SELECTED", "select at least one mandi", "/mandis")
    if len({m.mandi_id for m in body.mandis}) != len(body.mandis):
        raise _refuse("MANDI_NOT_ELIGIBLE", "a mandi is listed twice", "/mandis")
    for i, m in enumerate(body.mandis):
        if not TRANSPORT_RE.match(m.transport_inr_per_quintal):
            raise _refuse(
                "TRANSPORT_INVALID", "transport: rupees with exactly two decimals, not negative", f"/mandis/{i}"
            )
    try:
        inc = Quantity.parse(body.increment_kg, "/increment_kg")
    except ContractError as e:
        raise _refuse("INCREMENT_INVALID", e.message, "/increment_kg") from e
    if inc.units <= 0:
        raise _refuse("INCREMENT_INVALID", "the increment must be positive", "/increment_kg")
    return {
        "variety": body.variety,
        "horizons_days": sorted(body.horizons_days),
        "mandis": [m.model_dump() for m in body.mandis],
        "increment_kg": body.increment_kg,
    }


# ---- loading ----
@dataclass
class Loaded:
    dataset: dict[str, Any]
    commodity_id: str
    obs: list[Obs]
    points: list[IndexPoint]
    forecasts: dict[str, list[Any]]
    basis: list[Basis]
    mandi_names: dict[str, str]


def _load(conn: Connection, variety: str) -> Loaded:
    """The newest non-rejected price dataset, the index of one variety, the baseline forecasts and the mandi basis."""
    ds = conn.execute(
        text("SELECT * FROM dataset_versions WHERE validation_status <> 'REJECTED' ORDER BY imported_at DESC LIMIT 1")
    ).first()
    if ds is None:
        raise AccessError(ErrorCode.NOT_FOUND, "no price dataset has been imported", "NO_PRICE_DATA")
    rows = conn.execute(
        text(
            "SELECT mandi_id, observed_on, modal_price, date_inferred, commodity_id FROM price_observations "
            "WHERE dataset_version_id = :d AND variety_label = :v"
        ),
        {"d": ds.id, "v": variety},
    ).all()
    if not rows:
        raise AccessError(ErrorCode.NOT_FOUND, f"the dataset has no rows for {variety}", "NO_PRICE_DATA")
    obs = [
        Obs(
            r.mandi_id,
            r.observed_on,
            None if r.modal_price is None else milli_from_decimal(r.modal_price),
            r.date_inferred,
        )
        for r in rows
    ]
    points = build_index(obs)
    try:
        forecasts = baseline_forecast(points, HORIZONS)
    except InsufficientHistory as e:
        raise _refuse("INSUFFICIENT_HISTORY", f"not enough index days to forecast: {e}") from e
    origin = max(p.day for p in points)
    basis = mandi_basis(obs, points, origin)
    names = {
        r.id: r.name
        for r in conn.execute(
            text("SELECT id, name FROM mandis WHERE id = ANY(:ids)"), {"ids": sorted({o.mandi_id for o in obs})}
        )
    }
    return Loaded(
        {
            "id": ds.id,
            "name": ds.name,
            "validation_status": ds.validation_status,
            "manifest_sha256": ds.manifest_sha256,
        },
        rows[0].commodity_id,
        obs,
        points,
        forecasts,
        basis,
        names,
    )


@router.get("/forecasts")
def forecasts(request: Request, p: Read, variety: Annotated[str, Query(max_length=60)]) -> dict[str, Any]:
    if variety not in VARIETIES:
        raise _refuse("VARIETY_NOT_FORECAST", f"only {', '.join(VARIETIES)} are forecast", "/variety")
    with tenant_scope(request, p) as conn:
        d = _load(conn, variety)
    return forecast_set_doc(
        variety=variety,
        dataset=d.dataset,
        points=d.points,
        forecasts=d.forecasts,
        basis=d.basis,
        mandi_names=d.mandi_names,
    )


# ---- sensor links ----
def _link_json(r: Any) -> dict[str, Any]:
    return {
        "contract": "plan.sensor_link/v1",
        "lot_id": r.lot_id,
        "device_id": r.device_id,
        "linked_at": format_instant(r.linked_at),
        "created_by": r.created_by,
    }


def _missing(request: Request, p: Principal, table: str, key_col: str, key: str, label: str) -> AccessError:
    """404 for a missing object and for another FPO's alike; the second case is audited."""
    assert (table, key_col) in {("lots", "id"), ("devices", "device_id"), ("plans", "id")}
    with auth_conn(engine()) as conn:
        elsewhere = conn.execute(
            text(f"SELECT 1 FROM {table} WHERE {key_col} = :k AND fpo_id <> :f"), {"k": key, "f": p.fpo_id}
        ).first()
        if elsewhere:
            audit(conn, request.state.request_id, "CROSS_TENANT_REFERENCE", actor=p.user_id, fpo_id=p.fpo_id)
    return AccessError(ErrorCode.NOT_FOUND, f"{label} not found")


@router.get("/sensor-links/current")
def current_links(request: Request, p: Read) -> list[dict[str, Any]]:
    with tenant_scope(request, p) as conn:
        rows = conn.execute(
            text(
                "SELECT DISTINCT ON (lot_id) lot_id, device_id, linked_at, created_by FROM sensor_links "
                "WHERE fpo_id = :f AND linked_at <= clock_timestamp() ORDER BY lot_id, linked_at DESC"
            ),
            {"f": p.fpo_id},
        ).all()
    return [_link_json(r) for r in rows if r.device_id is not None]


@router.get("/lots/{lot_id}/sensor-links")
def lot_links(lot_id: str, request: Request, p: Read) -> list[dict[str, Any]]:
    with tenant_scope(request, p) as conn:
        found = conn.execute(
            text("SELECT 1 FROM lots WHERE id = :i AND fpo_id = :f"), {"i": lot_id, "f": p.fpo_id}
        ).first()
        rows = conn.execute(
            text(
                "SELECT lot_id, device_id, linked_at, created_by FROM sensor_links "
                "WHERE lot_id = :l AND fpo_id = :f ORDER BY linked_at DESC"
            ),
            {"l": lot_id, "f": p.fpo_id},
        ).all()
    if not found:
        raise _missing(request, p, "lots", "id", lot_id, "lot")
    return [_link_json(r) for r in rows]


@router.post("/lots/{lot_id}/sensor-links", status_code=201)
def link_sensor(lot_id: str, body: SensorLinkBody, request: Request, p: Write) -> dict[str, Any]:
    missing: tuple[str, str, str, str] | None = None
    inactive = False
    row = None
    with tenant_scope(request, p) as conn:
        if (
            conn.execute(text("SELECT 1 FROM lots WHERE id = :i AND fpo_id = :f"), {"i": lot_id, "f": p.fpo_id}).first()
            is None
        ):
            missing = ("lots", "id", lot_id, "lot")
        elif body.device_id is not None:
            dev = conn.execute(
                text("SELECT active FROM devices WHERE device_id = :d AND fpo_id = :f"),
                {"d": body.device_id, "f": p.fpo_id},
            ).first()
            if dev is None:
                missing = ("devices", "device_id", body.device_id, "device")
            elif not dev.active:
                inactive = True
        if missing is None and not inactive:
            row = conn.execute(
                text(
                    "INSERT INTO sensor_links (lot_id, fpo_id, device_id, linked_at, created_by) "
                    "VALUES (:l, :f, :d, clock_timestamp(), :u) RETURNING lot_id, device_id, linked_at, created_by"
                ),
                {"l": lot_id, "f": p.fpo_id, "d": body.device_id, "u": p.user_id},
            ).one()
    if missing:
        raise _missing(request, p, *missing)
    if inactive:
        raise AccessError(ErrorCode.CONFLICT, "that sensor is no longer accepted", "DEVICE_INACTIVE")
    return _link_json(row)


# ---- plans ----
def _read_lots(conn: Connection, p: Principal, commodity_id: str) -> list[LotInput]:
    rows = conn.execute(
        text(
            "SELECT l.id, l.member_id, l.source_variety, b.on_hand_g, b.reserved_g, m.origin AS member_origin, "
            "(SELECT s.device_id FROM sensor_links s WHERE s.lot_id = l.id AND s.linked_at <= clock_timestamp() "
            " ORDER BY s.linked_at DESC LIMIT 1) AS device_id "
            "FROM lots l JOIN lot_balances b ON b.lot_id = l.id JOIN members m ON m.id = l.member_id "
            "WHERE l.fpo_id = :f AND l.commodity_id = :c ORDER BY l.id"
        ),
        {"f": p.fpo_id, "c": commodity_id},
    ).all()
    out = []
    for r in rows:
        sensor = SensorState(None, None, (), None)
        if r.device_id is not None:
            a = conn.execute(
                text(
                    "SELECT assessed_at, category, doc FROM storage_assessments WHERE device_id = :d "
                    "ORDER BY assessed_at DESC LIMIT 1"
                ),
                {"d": r.device_id},
            ).first()
            sensor = SensorState(
                r.device_id,
                None if a is None else a.category,
                () if a is None else tuple(a.doc.get("unknown_reasons", [])),
                None if a is None else a.assessed_at,
            )
        out.append(LotInput(r.id, r.member_id, r.member_origin, r.source_variety, r.on_hand_g - r.reserved_g, sensor))
    return out


def _stored(conn: Connection, p: Principal, key: str | None, sha: str | None) -> dict[str, Any] | None:
    if key is None:
        return None
    row = conn.execute(
        text("SELECT doc, request_sha256 FROM plans WHERE fpo_id = :f AND request_key = :k"), {"f": p.fpo_id, "k": key}
    ).first()
    if row is None:
        return None
    if row.request_sha256 != sha:
        raise AccessError(ErrorCode.IDEMPOTENCY_KEY_REUSED, "this Idempotency-Key was used for a different request")
    return dict(row.doc)


@router.post("/plans", status_code=201)
def create_plan(body: PlanBody, request: Request, p: Write, idempotency_key: IdemKey = None) -> dict[str, Any]:
    req = _validate(body)
    sha = (
        None
        if idempotency_key is None
        else hashlib.sha256(json.dumps(["POST /plans", req], sort_keys=True).encode()).hexdigest()
    )
    now = datetime.now(UTC)
    with tenant_scope(request, p) as conn:
        earlier = _stored(conn, p, idempotency_key, sha)
        if earlier is not None:
            return earlier
        d = _load(conn, req["variety"])
        known = {
            r.id
            for r in conn.execute(
                text("SELECT id FROM mandis WHERE id = ANY(:ids)"), {"ids": [m["mandi_id"] for m in req["mandis"]]}
            )
        }
        lots = _read_lots(conn, p, d.commodity_id)
    for i, m in enumerate(req["mandis"]):
        if m["mandi_id"] not in known:
            raise _refuse("MANDI_NOT_ELIGIBLE", "unknown mandi", f"/mandis/{i}")
    fset = forecast_set_doc(
        variety=req["variety"],
        dataset=d.dataset,
        points=d.points,
        forecasts=d.forecasts,
        basis=d.basis,
        mandi_names=d.mandi_names,
    )
    plan_id = new_id("pln")
    try:
        doc = build_plan_document(
            plan_id=plan_id,
            run_id=new_id("run"),
            fpo_id=p.fpo_id or "",
            commodity_id=d.commodity_id,
            created_by=p.user_id,
            created_at=now,
            today_ist=datetime.now(IST).date(),
            mode=load_settings().mode.value,
            request=req,
            dataset=d.dataset,
            forecast_set=fset,
            naive=d.forecasts["NAIVE"],
            basis={b.mandi_id: b for b in d.basis},
            mandi_names=d.mandi_names,
            lots=lots,
            time_limit_s=float(os.environ.get("KN_PLAN_TIME_LIMIT_S", "30")),
        )
    except PlanRefusal as e:
        raise _refuse(e.issue_code, e.message, e.path) from e
    duplicate = None
    with tenant_scope(request, p) as conn:
        try:
            with conn.begin_nested():
                conn.execute(
                    text(
                        "INSERT INTO plans (id, fpo_id, commodity_id, variety, status, origin, input_sha256, doc, "
                        "request_key, request_sha256, created_by, created_at) "
                        "VALUES (:i, :f, :c, :v, :s, :o, :h, CAST(:doc AS jsonb), :k, :q, :u, :t)"
                    ),
                    {
                        "i": plan_id,
                        "f": p.fpo_id,
                        "c": d.commodity_id,
                        "v": req["variety"],
                        "s": doc["status"],
                        "o": doc["origin"],
                        "h": doc["allocation_input_sha256"],
                        "doc": json.dumps(doc),
                        "k": idempotency_key,
                        "q": sha,
                        "u": p.user_id,
                        "t": now,
                    },
                )
        except IntegrityError:
            duplicate = _stored(conn, p, idempotency_key, sha)  # a concurrent request with the same key won
            if duplicate is None:
                raise
    return duplicate if duplicate is not None else doc


@router.get("/plans")
def list_plans(request: Request, p: Read, limit: Annotated[int, Query(ge=1, le=200)] = 50) -> list[dict[str, Any]]:
    with tenant_scope(request, p) as conn:
        rows = conn.execute(
            text("SELECT doc FROM plans WHERE fpo_id = :f ORDER BY created_at DESC, id DESC LIMIT :n"),
            {"f": p.fpo_id, "n": limit},
        ).all()
    return [summary_of(r.doc) for r in rows]


@router.get("/plans/{plan_id}")
def get_plan(plan_id: str, request: Request, p: Read) -> dict[str, Any]:
    with tenant_scope(request, p) as conn:
        row = conn.execute(
            text("SELECT doc FROM plans WHERE id = :i AND fpo_id = :f"), {"i": plan_id, "f": p.fpo_id}
        ).first()
    if row is None:
        raise _missing(request, p, "plans", "id", plan_id, "plan")
    return dict(row.doc)


def install(app: FastAPI) -> None:
    app.include_router(router)

"""Read-only endpoints of market/v1 (L1_L2_ORG_AND_MARKET_CONTRACT_v1.md §B). Reference data is global; the restricted
database role can read it and a rejected dataset's prices are hidden from it by row-level security."""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, FastAPI, Query, Request
from kisaniti_core.timeutil import format_instant
from sqlalchemy import text

from ..access.deps import require, tenant_scope
from ..access.service import Principal

router = APIRouter(prefix="/api/v1")
Read = Annotated[Principal, Depends(require("data.read"))]
ID_MND = r"^mnd_[0-9A-HJKMNP-TV-Z]{26}$"
ID_CMD = r"^cmd_[0-9A-HJKMNP-TV-Z]{26}$"


def dataset_json(r: Any) -> dict[str, Any]:
    return {
        "contract": "market.dataset/v1",
        "id": r.id,
        "name": r.name,
        "rows": r.rows,
        "dated_rows": r.dated_rows,
        "blank_date_rows": r.blank_date_rows,
        "flagged_rows": r.flagged_rows,
        "validation_status": r.validation_status,
        "manifest_sha256": r.manifest_sha256,
        "imported_at": format_instant(r.imported_at),
        "note": r.note,
    }


def price_json(r: Any) -> dict[str, Any]:
    def money(v: Any) -> str | None:
        return None if v is None else str(v)

    return {
        "contract": "market.price/v1",
        "mandi_id": r.mandi_id,
        "commodity_id": r.commodity_id,
        "variety_label": r.variety_label,
        "observed_on": r.observed_on.isoformat(),
        "min_inr_per_quintal": money(r.min_price),
        "max_inr_per_quintal": money(r.max_price),
        "modal_inr_per_quintal": money(r.modal_price),
        "arrivals_tonnes": money(r.arrivals_tonnes),
        "date_inferred": r.date_inferred,
        "arrivals_shared": r.arrivals_shared,
        "flags": list(r.flags),
        "dataset_version_id": r.dataset_version_id,
        "origin": "REAL",
    }


@router.get("/commodities")
def commodities(request: Request, p: Read) -> list[dict[str, Any]]:
    with tenant_scope(request, p) as conn:
        rows = conn.execute(
            text("SELECT id, source_name, crop, msp_applies FROM commodities ORDER BY source_name")
        ).all()
    return [
        {
            "contract": "market.commodity/v1",
            "id": r.id,
            "source_name": r.source_name,
            "crop": r.crop,
            "msp_applies": r.msp_applies,
        }
        for r in rows
    ]


@router.get("/mandis")
def mandis(
    request: Request,
    p: Read,
    q: Annotated[str | None, Query(max_length=60)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[dict[str, Any]]:
    with tenant_scope(request, p) as conn:
        rows = conn.execute(
            text(
                "SELECT id, name, town, state, latitude, longitude FROM mandis "
                "WHERE (CAST(:q AS text) IS NULL OR name ILIKE '%' || :q || '%') ORDER BY name LIMIT :n OFFSET :o"
            ),
            {"q": q, "n": limit, "o": offset},
        ).all()
    return [
        {
            "contract": "market.mandi/v1",
            "id": r.id,
            "name": r.name,
            "town": r.town,
            "state": r.state,
            "latitude": r.latitude,
            "longitude": r.longitude,
        }
        for r in rows
    ]


@router.get("/msp")
def msp(request: Request, p: Read) -> list[dict[str, Any]]:
    with tenant_scope(request, p) as conn:
        rows = conn.execute(text("SELECT * FROM msp_entries ORDER BY crop, marketing_season DESC, grade")).all()
    return [
        {
            "contract": "market.msp/v1",
            "crop": r.crop,
            "grade": r.grade,
            "marketing_season": r.marketing_season,
            "msp_inr_per_quintal": str(r.msp_inr_per_quintal),
            "announced_on": r.announced_on.isoformat(),
            "source_url": r.source_url,
            "verified": r.verified,
            "origin": "POLICY",
        }
        for r in rows
    ]


@router.get("/datasets")
def datasets(request: Request, p: Read) -> list[dict[str, Any]]:
    with tenant_scope(request, p) as conn:
        rows = conn.execute(text("SELECT * FROM dataset_versions ORDER BY imported_at DESC, name")).all()
    return [dataset_json(r) for r in rows]


@router.get("/prices")
def prices(
    request: Request,
    p: Read,
    mandi_id: Annotated[str | None, Query(pattern=ID_MND)] = None,
    commodity_id: Annotated[str | None, Query(pattern=ID_CMD)] = None,
    variety: Annotated[str | None, Query(max_length=60)] = None,
    date_from: Annotated[date | None, Query(alias="from")] = None,
    date_to: Annotated[date | None, Query(alias="to")] = None,
    limit: Annotated[int, Query(ge=1, le=2000)] = 200,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[dict[str, Any]]:
    with tenant_scope(request, p) as conn:
        rows = conn.execute(
            text(
                "SELECT * FROM price_observations WHERE (CAST(:m AS text) IS NULL OR mandi_id = :m) "
                "AND (CAST(:c AS text) IS NULL OR commodity_id = :c) "
                "AND (CAST(:v AS text) IS NULL OR variety_label = :v) "
                "AND (CAST(:f AS date) IS NULL OR observed_on >= :f) "
                "AND (CAST(:t AS date) IS NULL OR observed_on <= :t) "
                "ORDER BY observed_on DESC, id DESC LIMIT :n OFFSET :o"
            ),
            {"m": mandi_id, "c": commodity_id, "v": variety, "f": date_from, "t": date_to, "n": limit, "o": offset},
        ).all()
    return [price_json(r) for r in rows]


@router.get("/prices/summary")
def price_summary(request: Request, p: Read) -> dict[str, Any]:
    with tenant_scope(request, p) as conn:
        ds = conn.execute(
            text(
                "SELECT * FROM dataset_versions WHERE validation_status <> 'REJECTED' ORDER BY imported_at DESC LIMIT 1"
            )
        ).first()
        groups = (
            conn.execute(
                text(
                    "SELECT (CASE WHEN extract(month FROM observed_on) >= 9 THEN extract(year FROM observed_on) "
                    "ELSE extract(year FROM observed_on) - 1 END)::int AS y, variety_label, count(*) AS n, "
                    "min(observed_on) AS first_on, "
                    "max(observed_on) AS last_on FROM price_observations WHERE dataset_version_id = :d "
                    "GROUP BY y, variety_label ORDER BY y DESC, variety_label"
                ),
                {"d": ds.id if ds else None},
            ).all()
            if ds
            else []
        )
    newest = max((g.last_on for g in groups), default=None)
    today = datetime.now(ZoneInfo("Asia/Kolkata")).date()
    return {
        "contract": "market.price_summary/v1",
        "dataset": dataset_json(ds) if ds else None,
        "rows": sum(g.n for g in groups),
        "newest_observed_on": newest.isoformat() if newest else None,
        "days_since_newest": max(0, (today - newest).days) if newest else None,
        "by_season_variety": [
            {
                "season": f"{g.y}-{str(g.y + 1)[2:]}",
                "variety_label": g.variety_label,
                "rows": g.n,
                "first_on": g.first_on.isoformat(),
                "last_on": g.last_on.isoformat(),
            }
            for g in groups
        ],
    }


def install(app: FastAPI) -> None:
    app.include_router(router)

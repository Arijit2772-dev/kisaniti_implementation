"""Endpoints of sales/v1 (L5_SALES_CONTRACT_v1.md): actual sales, voids, execution, reports, outcomes and replies.

A sale and its ledger events are written in one transaction. The lot is locked first (an advisory lock for this module's
own checks, and the ledger trigger's row lock underneath), so two sales or a sale and a release of the same lot never
interleave. Reality is only recorded, never edited: a mistake is voided by a compensating record."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import date, datetime
from fractions import Fraction
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, FastAPI, Header, Query, Request
from kisaniti_core.errors import ContractError, ErrorCode
from kisaniti_core.ids import new_local_id
from kisaniti_core.quantity import Money, Quantity, RatePerQuintal, ratio_display
from kisaniti_core.timeutil import format_instant
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Connection, text
from sqlalchemy.exc import DBAPIError

from ..access.deps import engine, require, tenant_scope
from ..access.errors import AccessError
from ..access.service import Principal, audit, auth_conn
from ..approval.routes import _lines, _plan, _revisions, _states

router = APIRouter(prefix="/api/v1")
Read = Annotated[Principal, Depends(require("data.read"))]
Write = Annotated[Principal, Depends(require("data.write"))]
Approver = Annotated[Principal, Depends(require("plans.approve"))]
Sender = Annotated[Principal, Depends(require("messages.send"))]
IdemKey = Annotated[str | None, Header(alias="Idempotency-Key", min_length=8, max_length=120)]
IST = ZoneInfo("Asia/Kolkata")
OUTCOMES_LABEL = (
    "Plan price against realised price. Not an evaluation of the forecast method. "
    "Fictional (synthetic) rows are not evidence."
)
ID_PLN = r"^pln_[0-9A-HJKMNP-TV-Z]{26}$"


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PlanRefBody(Strict):
    plan_id: str = Field(pattern=ID_PLN)
    mandi_id: str = Field(pattern=r"^mnd_[0-9A-HJKMNP-TV-Z]{26}$")
    date: str = Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")


class ReleaseItem(Strict):
    plan_id: str = Field(pattern=ID_PLN)
    quantity_kg: str


class SaleBody(Strict):
    lot_id: str = Field(pattern=r"^lot_[0-9A-HJKMNP-TV-Z]{26}$")
    plan_ref: PlanRefBody | None
    mandi_id: str = Field(pattern=r"^mnd_[0-9A-HJKMNP-TV-Z]{26}$")
    sale_date: str = Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
    quantity_kg: str
    price_inr_per_quintal: str
    j_form_no: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9/_-]{0,39}$")
    release_reservations: list[ReleaseItem] = Field(default_factory=list, max_length=20)


class VoidBody(Strict):
    reason_code: Literal["ENTRY_ERROR", "BUYER_CANCELLED", "OTHER"]


class ReplyBody(Strict):
    text: str = Field(min_length=1, max_length=500)


def _conflict(code: str, message: str, extra: list[dict[str, str]] | None = None) -> AccessError:
    return AccessError(ErrorCode.CONFLICT, message, code, extra_issues=extra or [])


def _invalid(code: str, message: str, path: str | None = None) -> AccessError:
    return AccessError(ErrorCode.VALIDATION_FAILED, message, code, path)


def _kg(grams: int) -> str:
    return Quantity(grams).to_json()


def _missing(request: Request, p: Principal, table: str, key: str, label: str) -> AccessError:
    assert table in {"lots", "plans", "actual_sales", "instructions"}
    with auth_conn(engine()) as conn:
        elsewhere = conn.execute(
            text(f"SELECT 1 FROM {table} WHERE id = :k AND fpo_id <> :f"), {"k": key, "f": p.fpo_id}
        ).first()
        if elsewhere:
            audit(conn, request.state.request_id, "CROSS_TENANT_REFERENCE", actor=p.user_id, fpo_id=p.fpo_id)
    return AccessError(ErrorCode.NOT_FOUND, f"{label} not found")


def _lock(conn: Connection, key: str) -> None:
    conn.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 4))"), {"k": key})


def _stock_refusal(e: DBAPIError) -> AccessError | None:
    orig = e.orig
    if getattr(orig, "sqlstate", None) == "23514" and "stock invariant" in str(orig):
        return _conflict("EXCEEDS_STOCK", "the ledger refused: this would take stock below zero")
    return None


# ---- documents ----
def _sale_docs(conn: Connection, p: Principal, rows: list[Any]) -> list[dict[str, Any]]:
    ids = [r.id for r in rows]
    rel: dict[str, list[dict[str, str]]] = defaultdict(list)
    for r in conn.execute(
        text(
            "SELECT sale_id, plan_id, quantity_g FROM sale_releases WHERE fpo_id = :f AND sale_id = ANY(:ids) "
            "ORDER BY plan_id"
        ),
        {"f": p.fpo_id, "ids": ids},
    ):
        rel[r.sale_id].append({"plan_id": r.plan_id, "quantity_kg": _kg(r.quantity_g)})
    voids = {
        r.sale_id: r
        for r in conn.execute(
            text(
                "SELECT sale_id, reason_code, voided_by, voided_at FROM sale_voids WHERE fpo_id = :f AND sale_id = "
                "ANY(:ids)"
            ),
            {"f": p.fpo_id, "ids": ids},
        )
    }
    return [
        {
            "contract": "sales.actual_sale/v1",
            "id": r.id,
            "lot_id": r.lot_id,
            "member_id": r.member_id,
            "plan_ref": (
                None
                if r.plan_id is None
                else {"plan_id": r.plan_id, "mandi_id": r.plan_mandi_id, "date": r.plan_date.isoformat()}
            ),
            "mandi_id": r.mandi_id,
            "sale_date": r.sale_date.isoformat(),
            "quantity_kg": _kg(r.quantity_g),
            "price_inr_per_quintal": RatePerQuintal(r.price_paise).to_json(),
            "amount_inr": Money(r.amount_paise).to_json(),
            "j_form_no": r.j_form_no,
            "consumed_kg": _kg(r.consumed_g),
            "released_for_sale": rel.get(r.id, []),
            "origin": r.origin,
            "voided": (
                None
                if r.id not in voids
                else {
                    "reason_code": voids[r.id].reason_code,
                    "voided_by": voids[r.id].voided_by,
                    "voided_at": format_instant(voids[r.id].voided_at),
                }
            ),
            "recorded_by": r.recorded_by,
            "recorded_at": format_instant(r.recorded_at),
        }
        for r in rows
    ]


SALE_COLUMNS = (
    "id, fpo_id, lot_id, member_id, plan_id, plan_mandi_id, plan_date, mandi_id, sale_date, quantity_g, price_paise, "
    "amount_paise, j_form_no, consumed_g, origin, recorded_by, recorded_at, request_key, request_sha256"
)


def _amount_paise(quantity_g: int, price_paise: int) -> int:
    """kg x rupees-per-quintal / 100, half-even to the paisa: grams x paise / 100 000."""
    return round(Fraction(quantity_g * price_paise, 100_000))


# ---- recording a sale ----
def _plan_lot_remaining(conn: Connection, p: Principal, plan_id: str, lot_id: str) -> int | None:
    ln = [x for x in _lines(conn, p, [plan_id]).get(plan_id, []) if x["lot_id"] == lot_id]
    return None if not ln else Quantity.parse(ln[0]["remaining_kg"]).units


@router.post("/sales", status_code=201)
def create_sale(body: SaleBody, request: Request, p: Write, idempotency_key: IdemKey = None) -> dict[str, Any]:
    try:
        qty = Quantity.parse(body.quantity_kg, "/quantity_kg")
        price = RatePerQuintal.parse(body.price_inr_per_quintal, "/price_inr_per_quintal")
        day = date.fromisoformat(body.sale_date)
    except ContractError as e:
        raise _invalid(
            "QUANTITY_INVALID" if "quantity" in (e.path or "") else "PRICE_INVALID", e.message, e.path
        ) from e
    except ValueError as e:
        raise _invalid("SALE_DATE_INVALID", "not a calendar date", "/sale_date") from e
    if qty.units <= 0:
        raise _invalid("QUANTITY_INVALID", "the quantity must be positive", "/quantity_kg")
    if price.units <= 0:
        raise _invalid("PRICE_INVALID", "the price must be positive", "/price_inr_per_quintal")
    if day > datetime.now(IST).date():
        raise _invalid("SALE_DATE_IN_FUTURE", "a sale cannot be dated after today", "/sale_date")
    releases: list[tuple[str, int]] = []
    for r in body.release_reservations:
        try:
            q = Quantity.parse(r.quantity_kg, "/release_reservations")
        except ContractError as e:
            raise _invalid("RELEASE_INVALID", e.message, "/release_reservations") from e
        if q.units <= 0:
            raise _invalid("RELEASE_INVALID", "a release must be positive", "/release_reservations")
        releases.append((r.plan_id, q.units))
    if len({pid for pid, _ in releases}) != len(releases):
        raise _invalid("RELEASE_INVALID", "name each plan once", "/release_reservations")
    sha = (
        None
        if idempotency_key is None
        else hashlib.sha256(json.dumps(["POST /sales", body.model_dump()], sort_keys=True).encode()).hexdigest()
    )
    missing: tuple[str, str, str] | None = None
    with tenant_scope(request, p) as conn:
        _lock(conn, body.lot_id)
        if idempotency_key is not None:
            prior = conn.execute(
                text(f"SELECT {SALE_COLUMNS} FROM actual_sales WHERE fpo_id = :f AND request_key = :k"),
                {"f": p.fpo_id, "k": idempotency_key},
            ).first()
            if prior is not None:
                if prior.request_sha256 != sha:
                    raise AccessError(
                        ErrorCode.IDEMPOTENCY_KEY_REUSED, "this Idempotency-Key was used for a different request"
                    )
                return _sale_docs(conn, p, [prior])[0]
        lot = conn.execute(
            text(
                "SELECT l.id, l.member_id, l.harvested_on, m.origin, b.on_hand_g, b.reserved_g FROM lots l "
                "JOIN members m ON m.id = l.member_id JOIN lot_balances b ON b.lot_id = l.id WHERE l.id = :i AND "
                "l.fpo_id = :f"
            ),
            {"i": body.lot_id, "f": p.fpo_id},
        ).first()
        if lot is None:
            missing = ("lots", body.lot_id, "lot")
        else:
            if lot.harvested_on is not None and day < lot.harvested_on:
                raise _invalid("SALE_BEFORE_HARVEST", "the sale is dated before the lot's harvest", "/sale_date")
            if conn.execute(text("SELECT 1 FROM mandis WHERE id = :m"), {"m": body.mandi_id}).first() is None:
                raise _invalid("UNKNOWN_MANDI", "unknown mandi", "/mandi_id")
            if conn.execute(
                text("SELECT 1 FROM actual_sales WHERE fpo_id = :f AND j_form_no = :j AND lot_id = :l"),
                {"f": p.fpo_id, "j": body.j_form_no, "l": body.lot_id},
            ).first():
                raise _conflict("DUPLICATE_J_FORM", "this J-form number was already recorded for this lot")
            free = lot.on_hand_g - lot.reserved_g
            effects: list[tuple[str, str | None, int]] = []  # (event kind, plan id, grams)
            consumed = 0
            if body.plan_ref is not None:
                if releases:
                    raise _invalid(
                        "RELEASE_INVALID",
                        "a sale that follows a plan does not release other reservations",
                        "/release_reservations",
                    )
                plan = _plan(conn, p, body.plan_ref.plan_id)
                if plan is None:
                    missing = ("plans", body.plan_ref.plan_id, "plan")
                else:
                    state = _states(conn, p, [plan])[0]
                    if state["status"] != "APPROVED" or state["decision"] is None:
                        raise _conflict("NOT_APPROVED", "the plan is not approved")
                    rev = next(
                        r for r in _revisions(conn, p, plan) if r["revision_no"] == state["decision"]["revision_no"]
                    )
                    ref = body.plan_ref
                    if not any(
                        a["lot_id"] == body.lot_id and a["mandi_id"] == ref.mandi_id and a["date"] == ref.date
                        for a in rev["assignments"]
                    ):
                        raise _invalid("ASSIGNMENT_NOT_FOUND", "the plan has no such line for this lot", "/plan_ref")
                    remaining = _plan_lot_remaining(conn, p, ref.plan_id, body.lot_id)
                    if remaining is None or qty.units > remaining:
                        raise _conflict(
                            "EXCEEDS_RESERVATION", f"the plan still holds {_kg(remaining or 0)} kg of this lot"
                        )
                    effects = [("RESERVATION_CONSUMED", ref.plan_id, qty.units)]
                    consumed = qty.units
            else:
                if qty.units > lot.on_hand_g:
                    raise _conflict("EXCEEDS_STOCK", f"the lot holds only {_kg(lot.on_hand_g)} kg")
                shortfall = max(qty.units - free, 0)
                if shortfall == 0 and releases:
                    raise _invalid(
                        "RELEASE_INVALID",
                        "the lot has enough free stock; nothing needs releasing",
                        "/release_reservations",
                    )
                if shortfall > 0:
                    holds = conn.execute(
                        text(
                            "SELECT DISTINCT plan_id FROM plan_reservations WHERE fpo_id = :f AND lot_id = :l ORDER BY "
                            "plan_id"
                        ),
                        {"f": p.fpo_id, "l": body.lot_id},
                    ).all()
                    remain = {r.plan_id: _plan_lot_remaining(conn, p, r.plan_id, body.lot_id) or 0 for r in holds}
                    if not releases:
                        raise _conflict(
                            "RECONCILIATION_REQUIRED",
                            f"{_kg(shortfall)} kg more than the free stock: say which reservations to release",
                            [
                                {"code": "RESERVATION_HOLDS", "path": pid, "message": _kg(r), "severity": "INFO"}
                                for pid, r in remain.items()
                                if r > 0
                            ],
                        )
                    for pid, g in releases:
                        if remain.get(pid, 0) < g:
                            raise _invalid(
                                "RELEASE_INVALID",
                                f"plan {pid} holds only {_kg(remain.get(pid, 0))} kg of this lot",
                                "/release_reservations",
                            )
                    if sum(g for _, g in releases) != shortfall:
                        raise _invalid(
                            "RELEASE_INVALID", f"release exactly {_kg(shortfall)} kg in total", "/release_reservations"
                        )
                    effects = [("RESERVATION_RELEASED", pid, g) for pid, g in releases]
        if missing is None:
            assert lot is not None
            sale_id = new_local_id("sal")
            event_ids: dict[tuple[str, str | None], int] = {}
            try:
                with conn.begin_nested():
                    for kind, eff_plan, g in effects:  # consumption or releases first: available stock never dips
                        event_ids[(kind, eff_plan)] = conn.execute(
                            text(
                                "INSERT INTO stock_events (lot_id, fpo_id, kind, quantity_g, created_by) "
                                "VALUES (:l, :f, :k, :q, :u) RETURNING id"
                            ),
                            {"l": body.lot_id, "f": p.fpo_id, "k": kind, "q": g, "u": p.user_id},
                        ).scalar_one()
                    sold_event = conn.execute(
                        text(
                            "INSERT INTO stock_events (lot_id, fpo_id, kind, quantity_g, created_by) "
                            "VALUES (:l, :f, 'SOLD', :q, :u) RETURNING id"
                        ),
                        {"l": body.lot_id, "f": p.fpo_id, "q": qty.units, "u": p.user_id},
                    ).scalar_one()
            except DBAPIError as e:
                refusal = _stock_refusal(e)
                if refusal is None:
                    raise
                raise refusal from e
            link = body.plan_ref
            conn.execute(
                text(
                    "INSERT INTO actual_sales (id, fpo_id, lot_id, member_id, plan_id, plan_mandi_id, plan_date, "
                    "mandi_id, sale_date, "
                    "quantity_g, price_paise, amount_paise, j_form_no, consumed_g, origin, sold_event_id, "
                    "consumed_event_id, "
                    "recorded_by, request_id, request_key, request_sha256) "
                    "VALUES (:id, :f, :l, :m, :p, :pm, :pd, :mi, :sd, :q, :pr, :am, :j, :c, :o, :se, :ce, :u, :r, :k, "
                    ":h)"
                ),
                {
                    "id": sale_id,
                    "f": p.fpo_id,
                    "l": body.lot_id,
                    "m": lot.member_id,
                    "p": None if link is None else link.plan_id,
                    "pm": None if link is None else link.mandi_id,
                    "pd": None if link is None else date.fromisoformat(link.date),
                    "mi": body.mandi_id,
                    "sd": day,
                    "q": qty.units,
                    "pr": price.units,
                    "am": _amount_paise(qty.units, price.units),
                    "j": body.j_form_no,
                    "c": consumed,
                    "o": lot.origin,
                    "se": sold_event,
                    "ce": event_ids.get(("RESERVATION_CONSUMED", None if link is None else link.plan_id)),
                    "u": p.user_id,
                    "r": request.state.request_id,
                    "k": idempotency_key,
                    "h": sha,
                },
            )
            for pid, g in releases:
                conn.execute(
                    text(
                        "INSERT INTO sale_releases (sale_id, plan_id, lot_id, fpo_id, quantity_g, stock_event_id) "
                        "VALUES (:s, :p, :l, :f, :q, :e)"
                    ),
                    {
                        "s": sale_id,
                        "p": pid,
                        "l": body.lot_id,
                        "f": p.fpo_id,
                        "q": g,
                        "e": event_ids[("RESERVATION_RELEASED", pid)],
                    },
                )
            row = conn.execute(text(f"SELECT {SALE_COLUMNS} FROM actual_sales WHERE id = :i"), {"i": sale_id}).one()
            doc = _sale_docs(conn, p, [row])[0]
    if missing:
        raise _missing(request, p, *missing[:2], missing[2])
    return doc


@router.get("/sales")
def list_sales(
    request: Request,
    p: Read,
    lot_id: Annotated[str | None, Query(pattern=r"^lot_[0-9A-HJKMNP-TV-Z]{26}$")] = None,
    plan_id: Annotated[str | None, Query(pattern=ID_PLN)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[dict[str, Any]]:
    with tenant_scope(request, p) as conn:
        rows = conn.execute(
            text(
                f"SELECT {SALE_COLUMNS} FROM actual_sales WHERE fpo_id = :f AND (CAST(:l AS text) IS NULL OR lot_id = "
                f":l) "
                "AND (CAST(:p AS text) IS NULL OR plan_id = :p) ORDER BY recorded_at DESC, id DESC LIMIT :n"
            ),
            {"f": p.fpo_id, "l": lot_id, "p": plan_id, "n": limit},
        ).all()
        return _sale_docs(conn, p, list(rows)) if rows else []


@router.get("/sales/{sale_id}")
def get_sale(sale_id: str, request: Request, p: Read) -> dict[str, Any]:
    with tenant_scope(request, p) as conn:
        row = conn.execute(
            text(f"SELECT {SALE_COLUMNS} FROM actual_sales WHERE id = :i AND fpo_id = :f"),
            {"i": sale_id, "f": p.fpo_id},
        ).first()
        doc = None if row is None else _sale_docs(conn, p, [row])[0]
    if doc is None:
        raise _missing(request, p, "actual_sales", sale_id, "sale")
    return doc


@router.post("/sales/{sale_id}/void", status_code=201)
def void_sale(sale_id: str, body: VoidBody, request: Request, p: Approver) -> dict[str, Any]:
    doc = None
    with tenant_scope(request, p) as conn:
        row = conn.execute(
            text(f"SELECT {SALE_COLUMNS} FROM actual_sales WHERE id = :i AND fpo_id = :f"),
            {"i": sale_id, "f": p.fpo_id},
        ).first()
        if row is not None:
            _lock(conn, row.lot_id)
            if conn.execute(text("SELECT 1 FROM sale_voids WHERE sale_id = :i"), {"i": sale_id}).first():
                raise _conflict("ALREADY_VOIDED", "this sale was already voided")
            ev = conn.execute(
                text(
                    "INSERT INTO stock_events (lot_id, fpo_id, kind, quantity_g, reason_code, created_by) "
                    "VALUES (:l, :f, 'ADJUSTED', :q, 'OTHER', :u) RETURNING id"
                ),
                {"l": row.lot_id, "f": p.fpo_id, "q": row.quantity_g, "u": p.user_id},
            ).scalar_one()
            conn.execute(
                text(
                    "INSERT INTO sale_voids (sale_id, fpo_id, reason_code, restore_event_id, voided_by, request_id) "
                    "VALUES (:s, :f, :r, :e, :u, :q)"
                ),
                {
                    "s": sale_id,
                    "f": p.fpo_id,
                    "r": body.reason_code,
                    "e": ev,
                    "u": p.user_id,
                    "q": request.state.request_id,
                },
            )
            doc = _sale_docs(conn, p, [row])[0]
    if doc is None:
        raise _missing(request, p, "actual_sales", sale_id, "sale")
    return doc


# ---- execution, report, outcomes ----
def _approved(
    conn: Connection, p: Principal, plan_id: str, request: Request
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    plan = _plan(conn, p, plan_id)
    if plan is None:
        raise _missing(request, p, "plans", plan_id, "plan")
    state = _states(conn, p, [plan])[0]
    if state["status"] != "APPROVED" or state["decision"] is None:
        raise _conflict("NOT_APPROVED", "the plan is not approved")
    rev = next(r for r in _revisions(conn, p, plan) if r["revision_no"] == state["decision"]["revision_no"])
    return plan, state, rev


def _sold_by_line(conn: Connection, p: Principal, plan_id: str) -> dict[tuple[str, str, str], tuple[int, int]]:
    """(lot, planned mandi, planned date) -> (grams sold, paise received) over non-voided linked sales."""
    out: dict[tuple[str, str, str], tuple[int, int]] = {}
    for r in conn.execute(
        text(
            "SELECT s.lot_id, s.plan_mandi_id, s.plan_date, sum(s.quantity_g) AS q, sum(s.amount_paise) AS a "
            "FROM actual_sales s "
            "WHERE s.fpo_id = :f AND s.plan_id = :p AND NOT EXISTS (SELECT 1 FROM sale_voids v WHERE v.sale_id = s.id) "
            "GROUP BY s.lot_id, s.plan_mandi_id, s.plan_date"
        ),
        {"f": p.fpo_id, "p": plan_id},
    ):
        out[(r.lot_id, r.plan_mandi_id, r.plan_date.isoformat())] = (int(r.q), int(r.a))
    return out


def _line_status(sold: int, planned: int) -> str:
    return "NOT_STARTED" if sold == 0 else ("COMPLETED" if sold >= planned else "PARTIAL")


def _off_plan(conn: Connection, p: Principal, lot_ids: list[str], since: datetime) -> dict[str, int]:
    return {
        r.lot_id: int(r.q)
        for r in conn.execute(
            text(
                "SELECT s.lot_id, sum(s.quantity_g) AS q FROM actual_sales s WHERE s.fpo_id = :f AND s.lot_id = "
                "ANY(:l) "
                "AND s.plan_id IS NULL AND s.recorded_at >= :t AND NOT EXISTS (SELECT 1 FROM sale_voids v WHERE "
                "v.sale_id = s.id) "
                "GROUP BY s.lot_id"
            ),
            {"f": p.fpo_id, "l": lot_ids, "t": since},
        )
    }


@router.get("/plans/{plan_id}/execution")
def execution(plan_id: str, request: Request, p: Read) -> dict[str, Any]:
    with tenant_scope(request, p) as conn:
        _, state, rev = _approved(conn, p, plan_id, request)
        sold = _sold_by_line(conn, p, plan_id)
        lines = []
        for a in rev["assignments"]:
            planned = Quantity.parse(a["quantity_kg"]).units
            s = sold.get((a["lot_id"], a["mandi_id"], a["date"]), (0, 0))[0]
            lines.append(
                {
                    "lot_id": a["lot_id"],
                    "mandi_id": a["mandi_id"],
                    "date": a["date"],
                    "planned_kg": _kg(planned),
                    "sold_kg": _kg(s),
                    "status": _line_status(s, planned),
                    "deviation_kg": _kg(max(s - planned, 0)),
                }
            )
        lot_ids = sorted({a["lot_id"] for a in rev["assignments"]})
        consumed = {
            r.lot_id: int(r.q)
            for r in conn.execute(
                text(
                    "SELECT lot_id, sum(consumed_g) AS q FROM actual_sales WHERE fpo_id = :f AND plan_id = :p GROUP BY "
                    "lot_id"
                ),
                {"f": p.fpo_id, "p": plan_id},
            )
        }
        sale_rel = {
            r.lot_id: int(r.q)
            for r in conn.execute(
                text(
                    "SELECT lot_id, sum(quantity_g) AS q FROM sale_releases "
                    "WHERE fpo_id = :f AND plan_id = :p GROUP BY "
                    "lot_id"
                ),
                {"f": p.fpo_id, "p": plan_id},
            )
        }
        person = {
            r.lot_id: int(r.quantity_g)
            for r in conn.execute(
                text("SELECT lot_id, quantity_g FROM plan_releases WHERE fpo_id = :f AND plan_id = :p"),
                {"f": p.fpo_id, "p": plan_id},
            )
        }
        reserved = {r["lot_id"]: Quantity.parse(r["reserved_kg"]).units for r in state["reservation"]["lines"]}
        off = _off_plan(
            conn, p, lot_ids, datetime.fromisoformat(state["decision"]["decided_at"].replace("Z", "+00:00"))
        )
    lots = [
        {
            "lot_id": lid,
            "reserved_kg": _kg(reserved.get(lid, 0)),
            "consumed_kg": _kg(consumed.get(lid, 0)),
            "released_kg": _kg(person.get(lid, 0)),
            "released_for_sale_kg": _kg(sale_rel.get(lid, 0)),
            "remaining_kg": _kg(
                reserved.get(lid, 0) - consumed.get(lid, 0) - person.get(lid, 0) - sale_rel.get(lid, 0)
            ),
            "off_plan_sold_kg": _kg(off.get(lid, 0)),
        }
        for lid in lot_ids
    ]
    statuses = {ln["status"] for ln in lines}
    overall = (
        "COMPLETED" if statuses == {"COMPLETED"} else ("NOT_STARTED" if statuses <= {"NOT_STARTED"} else "PARTIAL")
    )
    return {
        "contract": "sales.execution/v1",
        "plan_id": plan_id,
        "revision_no": rev["revision_no"],
        "status": overall,
        "lines": lines,
        "lots": lots,
    }


def _planned_price(plan: dict[str, Any]) -> dict[tuple[str, str, str], int]:
    """(lot, mandi, date) -> the plan's price in paise per quintal."""
    return {
        (c["lot_id"], c["mandi_id"], c["date"]): _per_quintal_paise(c["price_inr_per_kg"])
        for c in plan["allocation_input"]["choices"]
    }


def _per_quintal_paise(per_kg: str) -> int:
    """'44.1500' rupees per kg -> 441500 paise per quintal (x100 kg, then x100 paise: the digit string is the same)."""
    whole, frac = per_kg.split(".")
    return int(whole) * 10_000 + int(frac)


def _avg_price(paise_total: int, grams: int) -> int:
    """Paise per quintal of a quantity-weighted average: amount (paise) x 100 000 / grams."""
    return round(Fraction(paise_total * 100_000, grams))


@router.get("/plans/{plan_id}/report")
def report(plan_id: str, request: Request, p: Read) -> dict[str, Any]:
    with tenant_scope(request, p) as conn:
        plan, state, rev = _approved(conn, p, plan_id, request)
        sold = _sold_by_line(conn, p, plan_id)
        off = _off_plan(
            conn,
            p,
            sorted({a["lot_id"] for a in rev["assignments"]}),
            datetime.fromisoformat(state["decision"]["decided_at"].replace("Z", "+00:00")),
        )
    price = _planned_price(plan)
    owner = {r["lot_id"]: r["member_id"] for r in plan["lots"]}
    lines = []
    tot_planned = tot_sold = tot_pv = tot_av = 0
    per_farmer: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for a in rev["assignments"]:
        key = (a["lot_id"], a["mandi_id"], a["date"])
        planned = Quantity.parse(a["quantity_kg"]).units
        s, amount = sold.get(key, (0, 0))
        pp = price[key]
        pv = round(Fraction(planned * pp, 100_000))
        avg = _avg_price(amount, s) if s else None
        lines.append(
            {
                "lot_id": a["lot_id"],
                "mandi_id": a["mandi_id"],
                "date": a["date"],
                "planned_kg": _kg(planned),
                "sold_kg": _kg(s),
                "planned_price_inr_per_quintal": RatePerQuintal(pp).to_json(),
                "actual_avg_price_inr_per_quintal": None if avg is None else RatePerQuintal(avg).to_json(),
                "price_gap_inr_per_quintal": None if avg is None else RatePerQuintal(avg - pp).to_json(),
                "planned_value_inr": Money(pv).to_json(),
                "actual_value_inr": Money(amount).to_json(),
            }
        )
        tot_planned += planned
        tot_sold += s
        tot_pv += pv
        tot_av += amount
        per_farmer[owner[a["lot_id"]]][0] += planned
        per_farmer[owner[a["lot_id"]]][1] += s
    participating: dict[str, int] = defaultdict(int)
    for lot in plan["allocation_input"]["lots"]:
        participating[lot["farmer_id"]] += Quantity.parse(lot["quantity_kg"]).units
    farmers: list[dict[str, Any]] = []
    for fid in sorted(participating):
        pk, sk = per_farmer.get(fid, [0, 0])
        den = Quantity(participating[fid])
        farmers.append(
            {
                "farmer_id": fid,
                "participating_kg": den.to_json(),
                "planned_kg": _kg(pk),
                "sold_kg": _kg(sk),
                "planned_ratio": {
                    "numerator_kg": _kg(pk),
                    "denominator_kg": den.to_json(),
                    "display": ratio_display(Quantity(pk), den),
                },
                "actual_ratio": {
                    "numerator_kg": _kg(sk),
                    "denominator_kg": den.to_json(),
                    "display": ratio_display(Quantity(sk), den),
                },
            }
        )

    def lowest(idx: str) -> str | None:
        if not farmers:
            return None
        best = min(
            farmers,
            key=lambda f: Fraction(
                Quantity.parse(f[idx]["numerator_kg"]).units, Quantity.parse(f[idx]["denominator_kg"]).units
            ),
        )
        return str(best[idx]["display"])

    return {
        "contract": "sales.report/v1",
        "plan_id": plan_id,
        "revision_no": rev["revision_no"],
        "label": "Planned and actual are separate records. Values are gross; transport costs are not recorded.",
        "lines": lines,
        "farmers": farmers,
        "totals": {
            "planned_kg": _kg(tot_planned),
            "sold_kg": _kg(tot_sold),
            "off_plan_kg": _kg(sum(off.values())),
            "planned_value_inr": Money(tot_pv).to_json(),
            "actual_value_inr": Money(tot_av).to_json(),
        },
        "lowest_planned_display": lowest("planned_ratio"),
        "lowest_actual_display": lowest("actual_ratio"),
    }


@router.get("/forecast-outcomes")
def outcomes(request: Request, p: Read) -> dict[str, Any]:
    pairs: list[dict[str, Any]] = []
    with tenant_scope(request, p) as conn:
        plans = [
            dict(r.doc)
            for r in conn.execute(
                text("SELECT doc FROM plans WHERE fpo_id = :f ORDER BY created_at, id"), {"f": p.fpo_id}
            )
        ]
        for plan in plans:
            if plan["status"] != "PROPOSED":
                continue
            state = _states(conn, p, [plan])[0]
            if state["status"] != "APPROVED" or state["decision"] is None:
                continue
            price = _planned_price(plan)
            origin_of = {
                (r.lot_id, r.plan_mandi_id, r.plan_date.isoformat()): r.origin
                for r in conn.execute(
                    text(
                        "SELECT DISTINCT lot_id, plan_mandi_id, plan_date, origin FROM actual_sales "
                        "WHERE fpo_id = :f AND "
                        "plan_id = :p"
                    ),
                    {"f": p.fpo_id, "p": plan["id"]},
                )
            }
            for (lot, mandi, day), (grams, amount) in sorted(_sold_by_line(conn, p, plan["id"]).items()):
                if grams == 0:
                    continue
                pp = price.get((lot, mandi, day))
                if pp is None:
                    continue
                avg = _avg_price(amount, grams)
                pairs.append(
                    {
                        "plan_id": plan["id"],
                        "mandi_id": mandi,
                        "date": day,
                        "origin": origin_of.get((lot, mandi, day), "SYNTHETIC"),
                        "sold_kg": _kg(grams),
                        "planned_price_inr_per_quintal": RatePerQuintal(pp).to_json(),
                        "actual_avg_price_inr_per_quintal": RatePerQuintal(avg).to_json(),
                        "error_inr_per_quintal": RatePerQuintal(avg - pp).to_json(),
                        "_e": avg - pp,
                    }
                )

    def summary(origin_is_real: bool) -> dict[str, Any]:
        sel = [x["_e"] for x in pairs if (x["origin"] != "SYNTHETIC") == origin_is_real]
        if not sel:
            return {"pairs": 0, "mean_abs_error_inr_per_quintal": None, "mean_error_inr_per_quintal": None}
        n = len(sel)
        return {
            "pairs": n,
            "mean_abs_error_inr_per_quintal": RatePerQuintal(round(Fraction(sum(abs(e) for e in sel), n))).to_json(),
            "mean_error_inr_per_quintal": RatePerQuintal(round(Fraction(sum(sel), n))).to_json(),
        }

    return {
        "contract": "sales.outcomes/v1",
        "label": "Plan price against realised price. Not an evaluation of the forecast method. Fictional "
        "(synthetic) rows are not evidence.",
        "real": summary(True),
        "synthetic": summary(False),
        "pairs": [{k: v for k, v in x.items() if k != "_e"} for x in pairs],
    }


# ---- replies ----
def _reply_doc(r: Any) -> dict[str, Any]:
    return {
        "contract": "sales.reply/v1",
        "id": r.id,
        "instruction_id": r.instruction_id,
        "member_id": r.member_id,
        "text": r.body,
        "source": r.source,
        "recorded_by": r.recorded_by,
        "recorded_at": format_instant(r.recorded_at),
    }


REPLY_COLUMNS = "id, instruction_id, member_id, body, source, recorded_by, recorded_at"


@router.post("/instructions/{instruction_id}/replies", status_code=201)
def add_reply(instruction_id: str, body: ReplyBody, request: Request, p: Sender) -> dict[str, Any]:
    doc = None
    with tenant_scope(request, p) as conn:
        ins = conn.execute(
            text("SELECT id, member_id FROM instructions WHERE id = :i AND fpo_id = :f"),
            {"i": instruction_id, "f": p.fpo_id},
        ).first()
        if ins is not None:
            row = conn.execute(
                text(
                    "INSERT INTO replies (id, fpo_id, instruction_id, member_id, body, source, recorded_by, "
                    "request_id) "
                    ""
                    f"VALUES (:id, :f, :i, :m, :b, 'STAFF_ENTERED', :u, :r) RETURNING {REPLY_COLUMNS}"
                ),
                {
                    "id": new_local_id("rpl"),
                    "f": p.fpo_id,
                    "i": instruction_id,
                    "m": ins.member_id,
                    "b": body.text,
                    "u": p.user_id,
                    "r": request.state.request_id,
                },
            ).one()
            doc = _reply_doc(row)
    if doc is None:
        raise _missing(request, p, "instructions", instruction_id, "instruction")
    return doc


@router.get("/instructions/{instruction_id}/replies")
def instruction_replies(instruction_id: str, request: Request, p: Sender) -> list[dict[str, Any]]:
    with tenant_scope(request, p) as conn:
        found = conn.execute(
            text("SELECT 1 FROM instructions WHERE id = :i AND fpo_id = :f"), {"i": instruction_id, "f": p.fpo_id}
        ).first()
        rows = conn.execute(
            text(
                f"SELECT {REPLY_COLUMNS} FROM replies WHERE instruction_id = :i AND fpo_id = :f ORDER BY recorded_at "
                f"DESC, id DESC"
            ),
            {"i": instruction_id, "f": p.fpo_id},
        ).all()
    if not found:
        raise _missing(request, p, "instructions", instruction_id, "instruction")
    return [_reply_doc(r) for r in rows]


@router.get("/replies")
def replies(request: Request, p: Sender, limit: Annotated[int, Query(ge=1, le=200)] = 100) -> list[dict[str, Any]]:
    with tenant_scope(request, p) as conn:
        rows = conn.execute(
            text(f"SELECT {REPLY_COLUMNS} FROM replies WHERE fpo_id = :f ORDER BY recorded_at DESC, id DESC LIMIT :n"),
            {"f": p.fpo_id, "n": limit},
        ).all()
    return [_reply_doc(r) for r in rows]


def install(app: FastAPI) -> None:
    app.include_router(router)

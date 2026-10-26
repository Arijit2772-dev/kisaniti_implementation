"""Endpoints of approval/v1 (L4_APPROVAL_CONTRACT_v1.md): revisions, the manager's decision, the exact reservation.

Every endpoint that changes a plan's decision state first takes a transaction-scoped advisory lock on the plan, so an
edit, an approval, a rejection and a release of one plan never interleave; the decision row's uniqueness and the
ledger trigger of org/v1 are the database's own second line. Reads and writes run in the caller's tenant transaction."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, FastAPI, Header, Query, Request
from kisaniti_core.allocation import build_result, parse_input, plan_from_json, validate_plan
from kisaniti_core.errors import ErrorCode
from kisaniti_core.ids import new_id
from kisaniti_core.quantity import Quantity
from kisaniti_core.timeutil import format_instant
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Connection, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from ..access.deps import engine, require, tenant_scope
from ..access.errors import AccessError
from ..access.service import Principal, audit, auth_conn
from ..plan.builder import resolve_storage
from ..plan.routes import _read_lots

router = APIRouter(prefix="/api/v1")
Read = Annotated[Principal, Depends(require("data.read"))]
Write = Annotated[Principal, Depends(require("data.write"))]
Approver = Annotated[Principal, Depends(require("plans.approve"))]
IdemKey = Annotated[str | None, Header(alias="Idempotency-Key", min_length=8, max_length=120)]
CODE = r"^[A-Z][A-Z0-9_]{1,63}$"


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AssignmentBody(Strict):
    lot_id: str = Field(pattern=r"^lot_[0-9A-HJKMNP-TV-Z]{26}$")
    mandi_id: str = Field(pattern=r"^mnd_[0-9A-HJKMNP-TV-Z]{26}$")
    date: str = Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
    quantity_kg: str


class RevisionBody(Strict):
    base_revision_no: int = Field(ge=1, le=10000)
    assignments: list[AssignmentBody] = Field(max_length=500)


class ApproveBody(Strict):
    revision_no: int = Field(ge=1, le=10000)
    acknowledged_warnings: list[Annotated[str, Field(pattern=CODE)]] = Field(max_length=40)


class RejectBody(Strict):
    revision_no: int = Field(ge=1, le=10000)
    reason_code: Literal["PRICE_CONCERN", "QUANTITY_CONCERN", "MARKET_CONCERN", "STORAGE_CONCERN", "OTHER"]


class ReleaseBody(Strict):
    reason_code: Literal["PLAN_CANCELLED", "STOCK_NEEDED_ELSEWHERE", "MARKET_CHANGED", "OTHER"]


def _conflict(code: str, message: str, path: str | None = None) -> AccessError:
    return AccessError(ErrorCode.CONFLICT, message, code, path)


def _invalid(code: str, message: str, path: str | None = None) -> AccessError:
    return AccessError(ErrorCode.VALIDATION_FAILED, message, code, path)


def _sha(*parts: Any) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()


def _kg(grams: int) -> str:
    return Quantity(grams).to_json()


# ---- loading ----
def _lock(conn: Connection, plan_id: str) -> None:
    conn.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:p, 0))"), {"p": plan_id})


def _missing(request: Request, p: Principal, plan_id: str) -> AccessError:
    """404 for a missing plan and for another FPO's alike; the second case is audited."""
    with auth_conn(engine()) as conn:
        elsewhere = conn.execute(
            text("SELECT 1 FROM plans WHERE id = :k AND fpo_id <> :f"), {"k": plan_id, "f": p.fpo_id}
        ).first()
        if elsewhere:
            audit(conn, request.state.request_id, "CROSS_TENANT_REFERENCE", actor=p.user_id, fpo_id=p.fpo_id)
    return AccessError(ErrorCode.NOT_FOUND, "plan not found")


def _plan(conn: Connection, p: Principal, plan_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        text("SELECT doc FROM plans WHERE id = :i AND fpo_id = :f"), {"i": plan_id, "f": p.fpo_id}
    ).first()
    return None if row is None else dict(row.doc)


def _revision_one(plan: dict[str, Any]) -> dict[str, Any]:
    """Revision 1 is the system proposal itself, read from the stored plan (never copied)."""
    return {
        "contract": "approval.revision/v1",
        "plan_id": plan["id"],
        "revision_no": 1,
        "id": None,
        "kind": "SYSTEM_PROPOSAL",
        "base_revision_no": None,
        "created_by": plan["created_by"],
        "created_at": plan["created_at"],
        "assignments": plan["allocation_result"]["assignments"],
        "changes": [],
        "allocation_result": plan["allocation_result"],
        "validation": plan["validation"],
    }


def _revisions(conn: Connection, p: Principal, plan: dict[str, Any]) -> list[dict[str, Any]]:
    rows = conn.execute(
        text("SELECT doc FROM plan_revisions WHERE plan_id = :i AND fpo_id = :f ORDER BY revision_no"),
        {"i": plan["id"], "f": p.fpo_id},
    ).all()
    return [_revision_one(plan), *[dict(r.doc) for r in rows]]


def _lines(conn: Connection, p: Principal, plan_ids: Sequence[str]) -> dict[str, list[dict[str, Any]]]:
    """Per plan and lot: reserved, released and what remains (grams to kg strings), in lot order."""
    rows = conn.execute(
        text(
            "SELECT r.plan_id, r.lot_id, r.quantity_g AS reserved_g, COALESCE(x.quantity_g, 0) AS released_g, "
            "COALESCE((SELECT sum(s.consumed_g)::bigint FROM actual_sales s WHERE s.plan_id = r.plan_id AND s.lot_id = "
            "r.lot_id), 0) "
            "AS consumed_g, "
            "COALESCE((SELECT sum(sr.quantity_g)::bigint FROM sale_releases sr WHERE sr.plan_id = r.plan_id AND "
            "sr.lot_id = r.lot_id), 0) "
            "AS sale_released_g "
            "FROM plan_reservations r LEFT JOIN plan_releases x ON x.plan_id = r.plan_id AND x.lot_id = r.lot_id "
            "WHERE r.fpo_id = :f AND r.plan_id = ANY(:ids) ORDER BY r.plan_id, r.lot_id"
        ),
        {"f": p.fpo_id, "ids": list(plan_ids)},
    ).all()
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        out[r.plan_id].append(
            {
                "lot_id": r.lot_id,
                "reserved_kg": _kg(r.reserved_g),
                "released_kg": _kg(r.released_g),
                "remaining_kg": _kg(r.reserved_g - r.released_g - r.consumed_g - r.sale_released_g),
            }
        )
    return out


def _decision_doc(row: Any, lines: list[dict[str, Any]]) -> dict[str, Any]:
    approved = row.decision == "APPROVED"
    return {
        "contract": "approval.decision/v1",
        "plan_id": row.plan_id,
        "revision_no": row.revision_no,
        "decision": row.decision,
        "reason_code": row.reason_code,
        "acknowledged_warnings": sorted(row.acknowledged),
        # as decided: nothing was released yet; the live numbers are in the plan state
        "reservation": (
            {"lines": [{**ln, "released_kg": "0.000", "remaining_kg": ln["reserved_kg"]} for ln in lines]}
            if approved
            else None
        ),
        "decided_by": row.decided_by,
        "decided_at": format_instant(row.decided_at),
    }


def _states(conn: Connection, p: Principal, plans: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ids = [pl["id"] for pl in plans]
    revs: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in conn.execute(
        text("SELECT plan_id, doc FROM plan_revisions WHERE fpo_id = :f AND plan_id = ANY(:ids) ORDER BY revision_no"),
        {"f": p.fpo_id, "ids": ids},
    ):
        revs[r.plan_id].append(dict(r.doc))
    decisions = {
        r.plan_id: r
        for r in conn.execute(
            text("SELECT * FROM plan_decisions WHERE fpo_id = :f AND plan_id = ANY(:ids)"), {"f": p.fpo_id, "ids": ids}
        )
    }
    lines = _lines(conn, p, ids)
    releases = {
        r.plan_id: r
        for r in conn.execute(
            text(
                "SELECT DISTINCT ON (plan_id) plan_id, reason_code, released_by, released_at FROM plan_releases "
                "WHERE fpo_id = :f AND plan_id = ANY(:ids) ORDER BY plan_id, released_at"
            ),
            {"f": p.fpo_id, "ids": ids},
        )
    }
    out = []
    for pl in plans:
        all_revs = [_revision_one(pl), *revs.get(pl["id"], [])]
        current = all_revs[-1]["revision_no"]
        dec = decisions.get(pl["id"])
        status = "NOT_PROPOSED" if pl["status"] == "NOT_PROPOSED" else (dec.decision if dec else "PROPOSED")
        ln = lines.get(pl["id"], [])
        remaining = sum(Quantity.parse(x["remaining_kg"]).units for x in ln)
        rel = releases.get(pl["id"])
        state = "NONE" if not ln else ("ACTIVE" if remaining > 0 else "RELEASED")
        out.append(
            {
                "contract": "approval.plan_state/v1",
                "plan_id": pl["id"],
                "status": status,
                "current_revision_no": current,
                "revisions": [
                    {
                        "revision_no": r["revision_no"],
                        "kind": r["kind"],
                        "created_at": r["created_at"],
                        "scheduled_kg": r["allocation_result"]["totals"]["scheduled_kg"],
                        "expected_net_return_inr": r["allocation_result"]["totals"]["expected_net_return_inr"],
                        "superseded": r["revision_no"] < current,
                    }
                    for r in all_revs
                ],
                "decision": None if dec is None else _decision_doc(dec, ln),
                "reservation": {
                    "state": state,
                    "lines": ln,
                    "release": (
                        None
                        if rel is None
                        else {
                            "reason_code": rel.reason_code,
                            "released_by": rel.released_by,
                            "released_at": format_instant(rel.released_at),
                        }
                    ),
                },
            }
        )
    return out


# ---- the approval context ----
def _context(
    conn: Connection, p: Principal, plan: dict[str, Any], rev: dict[str, Any], decided_status: str | None
) -> dict[str, Any]:
    per_lot: dict[str, int] = defaultdict(int)
    for a in rev["assignments"]:
        per_lot[a["lot_id"]] += Quantity.parse(a["quantity_kg"]).units
    now = datetime.now(UTC)
    current = {lot.lot_id: lot for lot in _read_lots(conn, p, plan["commodity_id"])}
    lines = []
    for lot_id in sorted(per_lot):
        avail = current[lot_id].available_g if lot_id in current else 0
        lines.append(
            {
                "lot_id": lot_id,
                "assigned_kg": _kg(per_lot[lot_id]),
                "available_kg": _kg(max(avail, 0)),
                "can_reserve": per_lot[lot_id] <= avail,
            }
        )
    warnings: list[dict[str, Any]] = []
    newest = (
        None
        if decided_status is not None
        else conn.execute(
            text(
                "SELECT id, name FROM dataset_versions WHERE validation_status <> 'REJECTED' "
                "ORDER BY imported_at DESC LIMIT 1"
            )
        ).first()
    )
    if newest is not None and newest.id != plan["dataset"]["id"]:
        warnings.append({"code": "NEWER_PRICE_DATASET", "params": {"dataset": newest.name}})
    for row in [] if decided_status is not None else plan["lots"]:  # after a decision, changes are no longer news
        lot = current.get(row["lot_id"])
        if lot is None or row["excluded_reason"] is not None:
            continue
        category, _ = resolve_storage(lot.sensor, now)
        if category != row["storage"]:
            warnings.append(
                {"code": "STORAGE_CHANGED", "params": {"lot_id": row["lot_id"], "from": row["storage"], "to": category}}
            )
        if _kg(lot.available_g) != row["available_kg"]:
            warnings.append(
                {
                    "code": "STOCK_CHANGED_SINCE_PLAN",
                    "params": {
                        "lot_id": row["lot_id"],
                        "planned_available_kg": row["available_kg"],
                        "now_available_kg": _kg(lot.available_g),
                    },
                }
            )
    required = sorted({w["code"] for w in plan["warnings"]} | {w["code"] for w in warnings})
    blockers: list[str] = []
    if plan["status"] == "NOT_PROPOSED":
        blockers.append("PLAN_NOT_PROPOSED")
    if decided_status is not None:
        blockers.append("ALREADY_DECIDED")
    if sum(per_lot.values()) == 0:
        blockers.append("NOTHING_TO_RESERVE")
    if decided_status is None and any(not ln["can_reserve"] for ln in lines):
        blockers.append("INSUFFICIENT_AVAILABLE")
    return {
        "contract": "approval.context/v1",
        "plan_id": plan["id"],
        "revision_no": rev["revision_no"],
        "status": "NOT_PROPOSED" if plan["status"] == "NOT_PROPOSED" else (decided_status or "PROPOSED"),
        "lines": lines,
        "context_warnings": warnings,
        "required_acknowledgements": required,
        "blockers": blockers,
        "can_approve": not blockers,
    }


# ---- reads ----
@router.get("/plan-states")
def plan_states(request: Request, p: Read, limit: Annotated[int, Query(ge=1, le=200)] = 50) -> list[dict[str, Any]]:
    with tenant_scope(request, p) as conn:
        plans = [
            dict(r.doc)
            for r in conn.execute(
                text("SELECT doc FROM plans WHERE fpo_id = :f ORDER BY created_at DESC, id DESC LIMIT :n"),
                {"f": p.fpo_id, "n": limit},
            )
        ]
        return _states(conn, p, plans) if plans else []


@router.get("/plans/{plan_id}/state")
def plan_state(plan_id: str, request: Request, p: Read) -> dict[str, Any]:
    with tenant_scope(request, p) as conn:
        plan = _plan(conn, p, plan_id)
        state = None if plan is None else _states(conn, p, [plan])[0]
    if state is None:
        raise _missing(request, p, plan_id)
    return state


@router.get("/plans/{plan_id}/revisions")
def list_revisions(plan_id: str, request: Request, p: Read) -> list[dict[str, Any]]:
    with tenant_scope(request, p) as conn:
        plan = _plan(conn, p, plan_id)
        revs = None if plan is None else _revisions(conn, p, plan)
    if revs is None:
        raise _missing(request, p, plan_id)
    return revs


@router.get("/plans/{plan_id}/approval-context")
def approval_context(plan_id: str, request: Request, p: Read) -> dict[str, Any]:
    with tenant_scope(request, p) as conn:
        plan = _plan(conn, p, plan_id)
        ctx = None
        if plan is not None:
            revs = _revisions(conn, p, plan)
            dec = conn.execute(
                text("SELECT decision FROM plan_decisions WHERE plan_id = :i AND fpo_id = :f"),
                {"i": plan_id, "f": p.fpo_id},
            ).first()
            ctx = _context(conn, p, plan, revs[-1], None if dec is None else dec.decision)
    if ctx is None:
        raise _missing(request, p, plan_id)
    return ctx


# ---- an edit: a new immutable revision ----
def _canon(assignments: list[dict[str, Any]]) -> list[tuple[str, str, str, int]]:
    return sorted((a["lot_id"], a["mandi_id"], a["date"], Quantity.parse(a["quantity_kg"]).units) for a in assignments)


def _changes(base: list[dict[str, Any]], new: list[dict[str, Any]]) -> list[dict[str, str]]:
    old = {(a["lot_id"], a["mandi_id"], a["date"]): Quantity.parse(a["quantity_kg"]).units for a in base}
    cur = {(a["lot_id"], a["mandi_id"], a["date"]): Quantity.parse(a["quantity_kg"]).units for a in new}
    out = []
    for k in sorted(set(old) | set(cur)):
        if old.get(k, 0) != cur.get(k, 0):
            out.append(
                {
                    "lot_id": k[0],
                    "mandi_id": k[1],
                    "date": k[2],
                    "from_kg": _kg(old.get(k, 0)),
                    "to_kg": _kg(cur.get(k, 0)),
                }
            )
    return out


@router.post("/plans/{plan_id}/revisions", status_code=201)
def create_revision(
    plan_id: str, body: RevisionBody, request: Request, p: Write, idempotency_key: IdemKey = None
) -> dict[str, Any]:
    sha = None if idempotency_key is None else _sha("POST revision", plan_id, body.model_dump())
    missing = False
    with tenant_scope(request, p) as conn:
        _lock(conn, plan_id)
        plan = _plan(conn, p, plan_id)
        if plan is None:
            missing = True
        else:
            if idempotency_key is not None:
                row = conn.execute(
                    text("SELECT doc, request_sha256 FROM plan_revisions WHERE fpo_id = :f AND request_key = :k"),
                    {"f": p.fpo_id, "k": idempotency_key},
                ).first()
                if row is not None:
                    if row.request_sha256 != sha:
                        raise AccessError(
                            ErrorCode.IDEMPOTENCY_KEY_REUSED, "this Idempotency-Key was used for a different request"
                        )
                    return dict(row.doc)
            if plan["status"] == "NOT_PROPOSED":
                raise _conflict("PLAN_NOT_PROPOSED", "a plan that was not proposed cannot be edited")
            if conn.execute(text("SELECT 1 FROM plan_decisions WHERE plan_id = :i"), {"i": plan_id}).first():
                raise _conflict("ALREADY_DECIDED", "this plan has been decided; it cannot be edited")
            revs = _revisions(conn, p, plan)
            current = revs[-1]
            if body.base_revision_no != current["revision_no"]:
                raise _conflict(
                    "STALE_REVISION", f"the current revision is {current['revision_no']}", "/base_revision_no"
                )
            inp = parse_input(plan["allocation_input"])
            items = [a.model_dump() for a in body.assignments]
            edited = plan_from_json(items)  # a malformed quantity or date is a contract error (422)
            report = validate_plan(inp, edited)
            if not report.valid:
                first, *rest = report.issues
                raise AccessError(
                    ErrorCode.VALIDATION_FAILED,
                    "the edited plan is not valid",
                    "PLAN_INVALID",
                    None,
                    extra_issues=[first, *rest],
                )
            if _canon(items) == _canon(current["assignments"]):
                raise _invalid("NO_CHANGE", "the edit does not differ from the current revision", "/assignments")
            result = build_result(inp, plan["allocation_input"], edited, "MANAGER_EDIT")
            no, rev_id, now = current["revision_no"] + 1, new_id("rev"), datetime.now(UTC)
            doc = {
                "contract": "approval.revision/v1",
                "plan_id": plan_id,
                "revision_no": no,
                "id": rev_id,
                "kind": "MANAGER_EDIT",
                "base_revision_no": current["revision_no"],
                "created_by": p.user_id,
                "created_at": format_instant(now),
                "assignments": result["assignments"],
                "changes": _changes(current["assignments"], result["assignments"]),
                "allocation_result": result,
                "validation": result["validation"],
            }
            try:
                with conn.begin_nested():
                    conn.execute(
                        text(
                            "INSERT INTO plan_revisions (id, plan_id, fpo_id, revision_no, base_revision_no, doc, "
                            "request_key, request_sha256, created_by, created_at) "
                            "VALUES (:i, :p, :f, :n, :b, CAST(:d AS jsonb), :k, :s, :u, :t)"
                        ),
                        {
                            "i": rev_id,
                            "p": plan_id,
                            "f": p.fpo_id,
                            "n": no,
                            "b": current["revision_no"],
                            "d": json.dumps(doc),
                            "k": idempotency_key,
                            "s": sha,
                            "u": p.user_id,
                            "t": now,
                        },
                    )
            except IntegrityError as e:
                raise _conflict("STALE_REVISION", "another revision was stored first") from e
    if missing:
        raise _missing(request, p, plan_id)
    return doc


# ---- approve ----
def _stock_refusal(e: DBAPIError) -> AccessError | None:
    orig = e.orig
    if getattr(orig, "sqlstate", None) == "23514" and "stock invariant" in str(orig):
        return _conflict("INSUFFICIENT_AVAILABLE", "the stock changed: not enough is available to reserve")
    return None


def _replay_or_decided(row: Any, key: str | None, sha: str | None) -> None:
    """A stored decision exists. The same key and body replays (handled by the caller); anything else is a conflict."""
    if key is not None and row.request_key == key:
        if row.request_sha256 != sha:
            raise AccessError(ErrorCode.IDEMPOTENCY_KEY_REUSED, "this Idempotency-Key was used for a different request")
        return
    raise _conflict("ALREADY_DECIDED", "this plan has already been decided")


@router.post("/plans/{plan_id}/approve", status_code=201)
def approve(
    plan_id: str, body: ApproveBody, request: Request, p: Approver, idempotency_key: IdemKey = None
) -> dict[str, Any]:
    sha = None if idempotency_key is None else _sha("POST approve", plan_id, body.model_dump())
    missing = False
    with tenant_scope(request, p) as conn:
        _lock(conn, plan_id)
        plan = _plan(conn, p, plan_id)
        if plan is None:
            missing = True
        else:
            existing = conn.execute(
                text("SELECT * FROM plan_decisions WHERE plan_id = :i AND fpo_id = :f"), {"i": plan_id, "f": p.fpo_id}
            ).first()
            if existing is not None:
                _replay_or_decided(existing, idempotency_key, sha)
                return _decision_doc(existing, _lines(conn, p, [plan_id]).get(plan_id, []))
            if plan["status"] == "NOT_PROPOSED":
                raise _conflict("PLAN_NOT_PROPOSED", "a plan that was not proposed cannot be approved")
            current = _revisions(conn, p, plan)[-1]
            if body.revision_no != current["revision_no"]:
                raise _conflict("STALE_REVISION", f"the current revision is {current['revision_no']}", "/revision_no")
            ctx = _context(conn, p, plan, current, None)
            if "NOTHING_TO_RESERVE" in ctx["blockers"]:
                raise _invalid("NOTHING_TO_RESERVE", "the revision allocates nothing")
            short = [ln["lot_id"] for ln in ctx["lines"] if not ln["can_reserve"]]
            if short:
                raise _conflict("INSUFFICIENT_AVAILABLE", "not enough stock is available for: " + ", ".join(short))
            missing_acks = sorted(set(ctx["required_acknowledgements"]) - set(body.acknowledged_warnings))
            if missing_acks:
                raise AccessError(
                    ErrorCode.VALIDATION_FAILED,
                    "every warning must be acknowledged: " + ", ".join(missing_acks),
                    "WARNINGS_NOT_ACKNOWLEDGED",
                    "/acknowledged_warnings",
                )
            decided = conn.execute(
                text(
                    "INSERT INTO plan_decisions (plan_id, fpo_id, revision_no, decision, reason_code, acknowledged, "
                    "context, decided_by, decided_at, request_id, request_key, request_sha256) "
                    "VALUES (:p, :f, :n, 'APPROVED', NULL, :a, CAST(:c AS jsonb), :u, clock_timestamp(), :r, :k, :s) "
                    "RETURNING *"
                ),
                {
                    "p": plan_id,
                    "f": p.fpo_id,
                    "n": current["revision_no"],
                    "a": sorted(set(body.acknowledged_warnings)),
                    "c": json.dumps(ctx),
                    "u": p.user_id,
                    "r": request.state.request_id,
                    "k": idempotency_key,
                    "s": sha,
                },
            ).one()
            try:
                with conn.begin_nested():
                    for ln in ctx["lines"]:
                        grams = Quantity.parse(ln["assigned_kg"]).units
                        if grams <= 0:
                            continue
                        ev = conn.execute(
                            text(
                                "INSERT INTO stock_events (lot_id, fpo_id, kind, quantity_g, created_by) "
                                "VALUES (:l, :f, 'RESERVED', :q, :u) RETURNING id"
                            ),
                            {"l": ln["lot_id"], "f": p.fpo_id, "q": grams, "u": p.user_id},
                        ).scalar_one()
                        conn.execute(
                            text(
                                "INSERT INTO plan_reservations (plan_id, fpo_id, lot_id, revision_no, quantity_g, "
                                "stock_event_id) VALUES (:p, :f, :l, :n, :q, :e)"
                            ),
                            {
                                "p": plan_id,
                                "f": p.fpo_id,
                                "l": ln["lot_id"],
                                "n": current["revision_no"],
                                "q": grams,
                                "e": ev,
                            },
                        )
            except DBAPIError as e:
                refusal = _stock_refusal(e)
                if refusal is None:
                    raise
                raise refusal from e  # leaving the transaction rolls the decision back too: all or nothing
            doc = _decision_doc(decided, _lines(conn, p, [plan_id]).get(plan_id, []))
    if missing:
        raise _missing(request, p, plan_id)
    return doc


@router.post("/plans/{plan_id}/reject", status_code=201)
def reject(
    plan_id: str, body: RejectBody, request: Request, p: Approver, idempotency_key: IdemKey = None
) -> dict[str, Any]:
    sha = None if idempotency_key is None else _sha("POST reject", plan_id, body.model_dump())
    missing = False
    with tenant_scope(request, p) as conn:
        _lock(conn, plan_id)
        plan = _plan(conn, p, plan_id)
        if plan is None:
            missing = True
        else:
            existing = conn.execute(
                text("SELECT * FROM plan_decisions WHERE plan_id = :i AND fpo_id = :f"), {"i": plan_id, "f": p.fpo_id}
            ).first()
            if existing is not None:
                _replay_or_decided(existing, idempotency_key, sha)
                return _decision_doc(existing, [])
            if plan["status"] == "NOT_PROPOSED":
                raise _conflict("PLAN_NOT_PROPOSED", "a plan that was not proposed cannot be rejected")
            current = _revisions(conn, p, plan)[-1]
            if body.revision_no != current["revision_no"]:
                raise _conflict("STALE_REVISION", f"the current revision is {current['revision_no']}", "/revision_no")
            ctx = _context(conn, p, plan, current, None)
            row = conn.execute(
                text(
                    "INSERT INTO plan_decisions (plan_id, fpo_id, revision_no, decision, reason_code, acknowledged, "
                    "context, decided_by, decided_at, request_id, request_key, request_sha256) "
                    "VALUES (:p, :f, :n, 'REJECTED', :c, '{}', CAST(:x AS jsonb), :u, clock_timestamp(), :r, :k, :s) "
                    "RETURNING *"
                ),
                {
                    "p": plan_id,
                    "f": p.fpo_id,
                    "n": current["revision_no"],
                    "c": body.reason_code,
                    "x": json.dumps(ctx),
                    "u": p.user_id,
                    "r": request.state.request_id,
                    "k": idempotency_key,
                    "s": sha,
                },
            ).one()
            doc = _decision_doc(row, [])
    if missing:
        raise _missing(request, p, plan_id)
    return doc


@router.post("/plans/{plan_id}/release", status_code=201)
def release(
    plan_id: str, body: ReleaseBody, request: Request, p: Approver, idempotency_key: IdemKey = None
) -> dict[str, Any]:
    sha = None if idempotency_key is None else _sha("POST release", plan_id, body.model_dump())
    missing = False
    with tenant_scope(request, p) as conn:
        _lock(conn, plan_id)
        plan = _plan(conn, p, plan_id)
        if plan is None:
            missing = True
        else:
            if idempotency_key is not None:
                prior = conn.execute(
                    text(
                        "SELECT request_sha256 FROM plan_releases "
                        "WHERE fpo_id = :f AND plan_id = :p AND request_key = :k LIMIT 1"
                    ),
                    {"f": p.fpo_id, "p": plan_id, "k": idempotency_key},
                ).first()
                if prior is not None:
                    if prior.request_sha256 != sha:
                        raise AccessError(
                            ErrorCode.IDEMPOTENCY_KEY_REUSED, "this Idempotency-Key was used for a different request"
                        )
                    return _states(conn, p, [plan])[0]
            dec = conn.execute(
                text("SELECT decision FROM plan_decisions WHERE plan_id = :i AND fpo_id = :f"),
                {"i": plan_id, "f": p.fpo_id},
            ).first()
            if dec is None or dec.decision != "APPROVED":
                raise _conflict("NOT_APPROVED", "only an approved plan has a reservation to release")
            todo = [
                ln for ln in _lines(conn, p, [plan_id]).get(plan_id, []) if Quantity.parse(ln["remaining_kg"]).units > 0
            ]
            if not todo:
                raise _conflict("NOTHING_TO_RELEASE", "nothing is left reserved for this plan")
            try:
                with conn.begin_nested():
                    for ln in todo:
                        grams = Quantity.parse(ln["remaining_kg"]).units
                        ev = conn.execute(
                            text(
                                "INSERT INTO stock_events (lot_id, fpo_id, kind, quantity_g, created_by) "
                                "VALUES (:l, :f, 'RESERVATION_RELEASED', :q, :u) RETURNING id"
                            ),
                            {"l": ln["lot_id"], "f": p.fpo_id, "q": grams, "u": p.user_id},
                        ).scalar_one()
                        conn.execute(
                            text(
                                "INSERT INTO plan_releases (plan_id, fpo_id, lot_id, quantity_g, stock_event_id, "
                                "reason_code, "
                                "released_by, released_at, request_id, request_key, request_sha256) "
                                "VALUES (:p, :f, :l, :q, :e, :c, :u, clock_timestamp(), :r, :k, :s)"
                            ),
                            {
                                "p": plan_id,
                                "f": p.fpo_id,
                                "l": ln["lot_id"],
                                "q": grams,
                                "e": ev,
                                "c": body.reason_code,
                                "u": p.user_id,
                                "r": request.state.request_id,
                                "k": idempotency_key,
                                "s": sha,
                            },
                        )
            except DBAPIError as e:
                refusal = _stock_refusal(e)
                if refusal is None:
                    raise
                raise _conflict("STOCK_INVARIANT", "the ledger refused the release") from e
            state = _states(conn, p, [plan])[0]
    if missing:
        raise _missing(request, p, plan_id)
    return state


def install(app: FastAPI) -> None:
    app.include_router(router)

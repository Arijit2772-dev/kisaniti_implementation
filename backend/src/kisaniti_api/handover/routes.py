"""Endpoints of handover/v1 (L5_HANDOVER_CONTRACT_v1.md): the manager sends from his own WhatsApp.

The system builds one wa.me link per eligible farmer from the stored instruction text and the frozen contact version,
shows it to a caller who may see phone numbers, and records what the manager reports. It sends nothing and cannot see
delivery. A link is never stored and never logged; each link shown writes one PHONE_LINK_ISSUED row to the access
log."""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Any, Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, FastAPI, Header, Query, Request
from kisaniti_core.errors import ErrorCode
from kisaniti_core.ids import new_local_id
from kisaniti_core.timeutil import format_instant
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Connection, text

from ..access.deps import deny, require, tenant_scope
from ..access.errors import AccessError
from ..access.service import Principal
from ..approval.routes import _lock, _plan, _states
from ..messaging.builder import ContactFacts, eligibility
from ..messaging.routes import (
    _consent_facts,
    _facts_of,
    _latest_rows,
    _log_view,
    _member_inputs,
    _missing,
)

router = APIRouter(prefix="/api/v1")
Read = Annotated[Principal, Depends(require("data.read"))]
Sender = Annotated[Principal, Depends(require("messages.send"))]
IdemKey = Annotated[str | None, Header(alias="Idempotency-Key", min_length=8, max_length=120)]
Kind = Literal["SALE_INSTRUCTION", "CANCELLATION"]
LABEL = "Sent by hand from the manager's own WhatsApp. The system cannot see whether it was delivered or read."


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateBody(Strict):
    kind: Kind
    recipient_count: int = Field(ge=1)
    acknowledge_unreviewed: bool


class OutcomeBody(Strict):
    outcome: Literal["SENT", "NOT_SENT"]


def _conflict(code: str, message: str) -> AccessError:
    return AccessError(ErrorCode.CONFLICT, message, code)


def can_link(p: Principal) -> bool:
    return "contacts.view_phone" in p.capabilities


def link_for(phone_e164: str, body: str) -> str:
    """wa.me/<digits>?text=<percent-encoded UTF-8>: the exact stored text, nothing added."""
    return f"https://wa.me/{phone_e164.lstrip('+')}?text={quote(body, safe='')}"


def _text_sha(body: str) -> str:
    return hashlib.sha256(body.encode()).hexdigest()


# ---- who is eligible now ----
def _plan_state(conn: Connection, p: Principal, plan_id: str, kind: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """The plan and its state, refusing a kind the plan cannot have yet (the same rules as preparing)."""
    plan = _plan(conn, p, plan_id)
    if plan is None:
        raise KeyError(plan_id)
    state = _states(conn, p, [plan])[0]
    approved = state["status"] == "APPROVED" and state["decision"] is not None
    if kind == "SALE_INSTRUCTION" and not (approved and state["reservation"]["state"] == "ACTIVE"):
        raise _conflict("NOT_APPROVED", "the plan is not approved with an active reservation")
    if kind == "CANCELLATION" and not (
        approved and state["reservation"]["state"] == "RELEASED" and state["reservation"]["release"] is not None
    ):
        raise _conflict("NOT_RELEASED", "the plan's reservation has not been released")
    return plan, state


def _recheck(
    conn: Connection, p: Principal, row: Any, plan_active: bool, contacts: Any, members: Any, consents: Any
) -> list[str]:
    """The checks of messaging/v1 'Re-check at send', now."""
    member = members[row.member_id]
    contact = contacts.get(row.member_id)
    consent = _consent_facts(consents.get(row.member_id, {}))
    issues = eligibility(_facts_of(member), None if contact is None else ContactFacts(contact.version), consent, True)
    if member.preferred_language != row.language:
        issues.append("LANGUAGE_CHANGED")
    if row.kind == "SALE_INSTRUCTION" and not plan_active:
        issues.append("COMMITMENT_NOT_ACTIVE")
    return issues


def _reported_sent_members(conn: Connection, p: Principal, plan_id: str, kind: str) -> set[str]:
    rows = conn.execute(
        text(
            "SELECT DISTINCT i.member_id FROM handover_items i JOIN handovers h ON h.id = i.handover_id "
            "WHERE h.fpo_id = :f AND h.plan_id = :p AND h.kind = :k AND (SELECT r.outcome FROM handover_reports r "
            "WHERE r.handover_id = i.handover_id AND r.instruction_id = i.instruction_id ORDER BY r.id DESC LIMIT 1) "
            "= 'SENT'"
        ),
        {"f": p.fpo_id, "p": plan_id, "k": kind},
    ).all()
    return {r.member_id for r in rows}


def _evaluate(conn: Connection, p: Principal, plan_id: str, kind: str) -> dict[str, Any]:
    _plan_doc, state = _plan_state(conn, p, plan_id, kind)
    rows = [r for r in _latest_rows(conn, p, plan_id) if r.kind == kind]
    members, contacts, consents = _member_inputs(conn, p, [r.member_id for r in rows])
    sent = _reported_sent_members(conn, p, plan_id, kind)
    active = state["reservation"]["state"] == "ACTIVE"
    ready: list[Any] = []
    blocked: list[tuple[Any, list[str]]] = []
    for r in rows:
        if r.member_id in sent:
            continue
        issues = list(r.issues) if r.status == "BLOCKED" else _recheck(conn, p, r, active, contacts, members, consents)
        if issues:
            blocked.append((r, issues))
        else:
            ready.append(r)
    return {
        "ready": ready,
        "blocked": blocked,
        "sent": sorted(sent),
        "members": members,
        "contacts": contacts,
        "consents": consents,
        "active": active,
    }


@router.get("/plans/{plan_id}/handover-preview")
def preview(plan_id: str, request: Request, p: Sender, kind: Annotated[Kind, Query()]) -> dict[str, Any]:
    missing = False
    with tenant_scope(request, p) as conn:
        try:
            ev = _evaluate(conn, p, plan_id, kind)
        except KeyError:
            missing = True
    if missing:
        raise _missing(request, p, "plans", plan_id, "plan")
    members = ev["members"]
    return {
        "contract": "handover.preview/v1",
        "plan_id": plan_id,
        "kind": kind,
        "recipient_count": len(ev["ready"]),
        "unreviewed_count": sum(1 for r in ev["ready"] if r.template_review != "REVIEWED"),
        "ready": [
            {
                "instruction_id": r.id,
                "member_id": r.member_id,
                "display_name": members[r.member_id].display_name,
                "language": r.language,
                "review_status": r.template_review,
            }
            for r in ev["ready"]
        ],
        "blocked": [
            {
                "instruction_id": r.id,
                "member_id": r.member_id,
                "display_name": members[r.member_id].display_name,
                "issues": issues,
            }
            for r, issues in ev["blocked"]
        ],
        "already_reported_sent": ev["sent"],
        "label": LABEL,
    }


# ---- documents ----
def _report_state(outcome: str | None) -> str:
    return {None: "NOT_REPORTED", "SENT": "MANAGER_REPORTED_SENT", "NOT_SENT": "MANAGER_REPORTED_NOT_SENT"}[outcome]


def _items(conn: Connection, request: Request, p: Principal, handover: Any, with_links: bool) -> list[dict[str, Any]]:
    """The items of one hand-over. With links: for each item not reported as sent, the checks run again and the link
    is built only if they pass; one access-log row per link shown."""
    rows = conn.execute(
        text(
            "SELECT i.instruction_id, i.member_id, i.status, i.issues, i.contact_version, i.language, i.review_status, "
            "i.text_sha256, m.display_name, ins.body, ins.kind, ins.plan_id, "
            "(SELECT r.outcome FROM handover_reports r WHERE r.handover_id = i.handover_id AND r.instruction_id = "
            "i.instruction_id ORDER BY r.id DESC LIMIT 1) AS outcome, "
            "(SELECT r.reported_by FROM handover_reports r WHERE r.handover_id = i.handover_id AND r.instruction_id = "
            "i.instruction_id ORDER BY r.id DESC LIMIT 1) AS by, "
            "(SELECT r.reported_at FROM handover_reports r WHERE r.handover_id = i.handover_id AND r.instruction_id = "
            "i.instruction_id ORDER BY r.id DESC LIMIT 1) AS at "
            "FROM handover_items i JOIN members m ON m.id = i.member_id AND m.fpo_id = i.fpo_id "
            "JOIN instructions ins ON ins.id = i.instruction_id AND ins.fpo_id = i.fpo_id "
            "WHERE i.fpo_id = :f AND i.handover_id = :h ORDER BY m.display_name, i.instruction_id"
        ),
        {"f": p.fpo_id, "h": handover.id},
    ).all()
    due = [r for r in rows if with_links and r.status == "ISSUED" and r.outcome != "SENT"]
    members, contacts, consents = _member_inputs(conn, p, [r.member_id for r in due]) if due else ({}, {}, {})
    active = False
    if due:
        plan = _plan(conn, p, handover.plan_id)
        active = plan is not None and _states(conn, p, [plan])[0]["reservation"]["state"] == "ACTIVE"
    out: list[dict[str, Any]] = []
    logged: list[str] = []
    for r in rows:
        link: str | None = None
        withheld: list[str] = []
        if r in due:
            fake = type("Row", (), {"member_id": r.member_id, "language": r.language, "kind": r.kind})()
            withheld = _recheck(conn, p, fake, active, contacts, members, consents)
            cur = contacts.get(r.member_id)
            if cur is not None and cur.version != r.contact_version:
                withheld.append("CONTACT_CHANGED")
            if not withheld:
                frozen = conn.execute(
                    text(
                        "SELECT phone_e164 FROM member_contacts WHERE fpo_id = :f AND member_id = :m AND version = :v"
                    ),
                    {"f": p.fpo_id, "m": r.member_id, "v": r.contact_version},
                ).one()
                if r.body is None or _text_sha(r.body) != r.text_sha256:
                    raise AccessError(ErrorCode.INTERNAL, "the stored text does not match its hash")
                link = link_for(frozen.phone_e164, r.body)
                logged.append(r.member_id)
        out.append(
            {
                "contract": "handover.item/v1",
                "handover_id": handover.id,
                "instruction_id": r.instruction_id,
                "member_id": r.member_id,
                "display_name": r.display_name,
                "language": r.language,
                "review_status": r.review_status,
                "status": r.status,
                "issues": list(r.issues),
                "link": link,
                "link_withheld": withheld,
                "state": _report_state(r.outcome),
                "reported_by": r.by,
                "reported_at": None if r.at is None else format_instant(r.at),
            }
        )
    if logged:
        _log_view(conn, request, p, logged, "PHONE_LINK_ISSUED")
    return out


HND_COLUMNS = "id, plan_id, kind, created_by, created_at, recipient_count, acknowledged_unreviewed, request_sha256"


def _doc(conn: Connection, request: Request, p: Principal, h: Any, with_links: bool) -> dict[str, Any]:
    return {
        "contract": "handover.handover/v1",
        "id": h.id,
        "plan_id": h.plan_id,
        "kind": h.kind,
        "created_by": h.created_by,
        "created_at": format_instant(h.created_at),
        "recipient_count": h.recipient_count,
        "acknowledged_unreviewed": h.acknowledged_unreviewed,
        "items": _items(conn, request, p, h, with_links),
        "label": LABEL,
    }


# ---- the yes ----
@router.post("/plans/{plan_id}/handovers", status_code=201)
def create(
    plan_id: str, body: CreateBody, request: Request, p: Sender, idempotency_key: IdemKey = None
) -> dict[str, Any]:
    if not can_link(p):
        raise deny(request, p, "CAPABILITY_MISSING", "requires contacts.view_phone")
    sha = (
        None
        if idempotency_key is None
        else hashlib.sha256(
            json.dumps(["POST handovers", plan_id, body.model_dump()], sort_keys=True).encode()
        ).hexdigest()
    )
    missing = False
    with tenant_scope(request, p) as conn:
        _lock(conn, plan_id)
        if idempotency_key is not None:
            prior = conn.execute(
                text(f"SELECT {HND_COLUMNS} FROM handovers WHERE fpo_id = :f AND request_key = :k"),
                {"f": p.fpo_id, "k": idempotency_key},
            ).first()
            if prior is not None:
                if prior.request_sha256 != sha:
                    raise AccessError(
                        ErrorCode.IDEMPOTENCY_KEY_REUSED, "this Idempotency-Key was used for a different request"
                    )
                return _doc(conn, request, p, prior, True)
        try:
            ev = _evaluate(conn, p, plan_id, body.kind)
        except KeyError:
            missing = True
        if not missing:
            ready = ev["ready"]
            if not ready:
                raise _conflict("NOTHING_TO_SEND", "no farmer can be sent this instruction now")
            if len(ready) != body.recipient_count:
                raise _conflict(
                    "CONFIRMATION_STALE",
                    f"the number of farmers changed to {len(ready)} since the question was shown; ask again",
                )
            if any(r.template_review != "REVIEWED" for r in ready) and not body.acknowledge_unreviewed:
                raise AccessError(
                    ErrorCode.VALIDATION_FAILED,
                    "some texts are not reviewed by a native speaker; acknowledge to continue",
                    "UNREVIEWED_NOT_ACKNOWLEDGED",
                    "/acknowledge_unreviewed",
                )
            hid = new_local_id("hnd")
            h = conn.execute(
                text(
                    "INSERT INTO handovers (id, plan_id, fpo_id, kind, created_by, recipient_count, "
                    "acknowledged_unreviewed, request_id, request_key, request_sha256) "
                    "VALUES (:id, :p, :f, :k, :u, :n, :a, :r, :rk, :h) "
                    f"RETURNING {HND_COLUMNS}"
                ),
                {
                    "id": hid,
                    "p": plan_id,
                    "f": p.fpo_id,
                    "k": body.kind,
                    "u": p.user_id,
                    "n": len(ready),
                    "a": body.acknowledge_unreviewed,
                    "r": request.state.request_id,
                    "rk": idempotency_key,
                    "h": sha,
                },
            ).one()
            for r in ready:
                contact = ev["contacts"][r.member_id]
                conn.execute(
                    text(
                        "INSERT INTO handover_items (handover_id, instruction_id, fpo_id, member_id, status, issues, "
                        "contact_version, language, review_status, text_sha256) "
                        "VALUES (:h, :i, :f, :m, 'ISSUED', '{}', :cv, :l, :rs, :t)"
                    ),
                    {
                        "h": hid,
                        "i": r.id,
                        "f": p.fpo_id,
                        "m": r.member_id,
                        "cv": contact.version,
                        "l": r.language,
                        "rs": r.template_review,
                        "t": _text_sha(r.body),
                    },
                )
            for r, issues in ev["blocked"]:
                conn.execute(
                    text(
                        "INSERT INTO handover_items (handover_id, instruction_id, fpo_id, member_id, status, issues) "
                        "VALUES (:h, :i, :f, :m, 'BLOCKED', :iss)"
                    ),
                    {"h": hid, "i": r.id, "f": p.fpo_id, "m": r.member_id, "iss": issues},
                )
            doc = _doc(conn, request, p, h, True)
    if missing:
        raise _missing(request, p, "plans", plan_id, "plan")
    return doc


# ---- reading ----
@router.get("/plans/{plan_id}/handovers")
def plan_handovers(plan_id: str, request: Request, p: Read) -> list[dict[str, Any]]:
    with tenant_scope(request, p) as conn:
        plan = _plan(conn, p, plan_id)
        rows = (
            []
            if plan is None
            else conn.execute(
                text(
                    f"SELECT {HND_COLUMNS} FROM handovers WHERE fpo_id = :f AND plan_id = :p "
                    "ORDER BY created_at DESC, id DESC"
                ),
                {"f": p.fpo_id, "p": plan_id},
            ).all()
        )
        docs = [_doc(conn, request, p, h, False) for h in rows]
    if plan is None:
        raise _missing(request, p, "plans", plan_id, "plan")
    return docs


@router.get("/handovers/{handover_id}")
def one(handover_id: str, request: Request, p: Read) -> dict[str, Any]:
    with_links = "messages.send" in p.capabilities and can_link(p)
    with tenant_scope(request, p) as conn:
        h = conn.execute(
            text(f"SELECT {HND_COLUMNS} FROM handovers WHERE id = :i AND fpo_id = :f"),
            {"i": handover_id, "f": p.fpo_id},
        ).first()
        doc = None if h is None else _doc(conn, request, p, h, with_links)
    if doc is None:
        raise _missing(request, p, "handovers", handover_id, "hand-over")
    return doc


@router.post("/handovers/{handover_id}/items/{instruction_id}/outcome", status_code=201)
def outcome(handover_id: str, instruction_id: str, body: OutcomeBody, request: Request, p: Sender) -> dict[str, Any]:
    found = True
    with tenant_scope(request, p) as conn:
        conn.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:i, 4))"), {"i": handover_id + instruction_id})
        h = conn.execute(
            text(f"SELECT {HND_COLUMNS} FROM handovers WHERE id = :i AND fpo_id = :f"),
            {"i": handover_id, "f": p.fpo_id},
        ).first()
        item = (
            None
            if h is None
            else conn.execute(
                text(
                    "SELECT status FROM handover_items WHERE handover_id = :h AND instruction_id = :i AND fpo_id = :f"
                ),
                {"h": handover_id, "i": instruction_id, "f": p.fpo_id},
            ).first()
        )
        if h is None or item is None:
            found = False
        else:
            if item.status != "ISSUED":
                raise _conflict("ITEM_NOT_ISSUED", "no link was made for this farmer, so nothing can be reported")
            last = conn.execute(
                text(
                    "SELECT outcome FROM handover_reports WHERE handover_id = :h AND instruction_id = :i "
                    "ORDER BY id DESC LIMIT 1"
                ),
                {"h": handover_id, "i": instruction_id},
            ).first()
            if last is not None and last.outcome == body.outcome:
                raise _conflict("NO_CHANGE", "this is already the reported state")
            conn.execute(
                text(
                    "INSERT INTO handover_reports (handover_id, instruction_id, fpo_id, outcome, reported_by, "
                    "request_id) VALUES (:h, :i, :f, :o, :u, :r)"
                ),
                {
                    "h": handover_id,
                    "i": instruction_id,
                    "f": p.fpo_id,
                    "o": body.outcome,
                    "u": p.user_id,
                    "r": request.state.request_id,
                },
            )
            items = _items(conn, request, p, h, False)
            doc = next(i for i in items if i["instruction_id"] == instruction_id)
    if not found:
        raise _missing(request, p, "handovers", handover_id, "hand-over")
    return doc


def install(app: FastAPI) -> None:
    app.include_router(router)

"""Endpoints of messaging/v1 (L5_MESSAGING_CONTRACT_v1.md): contacts, consent, templates, instructions, the preview
channel and delivery records. Phone numbers are shown masked unless the caller holds `contacts.view_phone`; each
unmasked view is written to the access log; no phone number is ever logged or stored outside `member_contacts`."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import UTC, date, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, FastAPI, Header, Request
from kisaniti_core.errors import ErrorCode
from kisaniti_core.ids import new_local_id
from kisaniti_core.messaging import PhoneInvalid, mask_phone, normalise_phone
from kisaniti_core.timeutil import format_instant
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Connection, text

from ..access.deps import engine, require, tenant_scope
from ..access.errors import AccessError
from ..access.service import Principal, audit, auth_conn
from ..approval.routes import _lock, _plan, _revisions, _states
from .builder import ConsentFacts, ContactFacts, LineFacts, MemberFacts, build_instruction, eligibility
from .channels import CHANNELS
from .templates import all_templates, latest

router = APIRouter(prefix="/api/v1")
Read = Annotated[Principal, Depends(require("data.read"))]
Write = Annotated[Principal, Depends(require("data.write"))]
Sender = Annotated[Principal, Depends(require("messages.send"))]
IdemKey = Annotated[str | None, Header(alias="Idempotency-Key", min_length=8, max_length=120)]
KINDS = ("ADVICE", "WHATSAPP", "RESEARCH")
SENT_STATUSES = ("QUEUED", "SENT", "DELIVERED", "READ")
REACHED = ("PREVIEWED", "SENT", "DELIVERED", "READ")


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ContactBody(Strict):
    phone: str = Field(min_length=5, max_length=30)


class ConsentBody(Strict):
    kind: Literal["ADVICE", "WHATSAPP", "RESEARCH"]
    action: Literal["GRANTED", "WITHDRAWN"]
    notice_version: str = Field(pattern=r"^[A-Z0-9][A-Z0-9._-]{2,40}$")
    method: Literal["STAFF_ASSISTED_ORAL", "PAPER_FORM", "DIGITAL_FORM"]


class PrepareBody(Strict):
    kind: Literal["SALE_INSTRUCTION", "CANCELLATION"]


class SendBody(Strict):
    channel: Literal["PREVIEW", "WHATSAPP"]


def _conflict(code: str, message: str) -> AccessError:
    return AccessError(ErrorCode.CONFLICT, message, code)


def _invalid(code: str, message: str, path: str | None = None) -> AccessError:
    return AccessError(ErrorCode.VALIDATION_FAILED, message, code, path)


def _missing(request: Request, p: Principal, table: str, key: str, label: str) -> AccessError:
    """404 for a missing object and for another FPO's alike; the second case is audited."""
    assert table in {"members", "plans", "instructions"}
    with auth_conn(engine()) as conn:
        elsewhere = conn.execute(
            text(f"SELECT 1 FROM {table} WHERE id = :k AND fpo_id <> :f"), {"k": key, "f": p.fpo_id}
        ).first()
        if elsewhere:
            audit(conn, request.state.request_id, "CROSS_TENANT_REFERENCE", actor=p.user_id, fpo_id=p.fpo_id)
    return AccessError(ErrorCode.NOT_FOUND, f"{label} not found")


def _can_see_phone(p: Principal) -> bool:
    return "contacts.view_phone" in p.capabilities


def _log_view(conn: Connection, request: Request, p: Principal, member_ids: list[str], what: str) -> None:
    for m in member_ids:
        conn.execute(
            text(
                "INSERT INTO personal_data_access_log (fpo_id, actor_user_id, member_id, what, request_id) "
                "VALUES (:f, :u, :m, :w, :r)"
            ),
            {"f": p.fpo_id, "u": p.user_id, "m": m, "w": what, "r": request.state.request_id},
        )


# ---- contacts and consent ----
def _contact_doc(row: Any, show: bool) -> dict[str, Any]:
    return {
        "contract": "messaging.contact/v1",
        "member_id": row.member_id,
        "version": row.version,
        "phone": row.phone_e164 if show else mask_phone(row.phone_e164),
        "phone_masked": not show,
        "source": "STAFF_ENTERED",
        "recorded_by": row.recorded_by,
        "recorded_at": format_instant(row.recorded_at),
    }


def _current_contacts(conn: Connection, p: Principal, member_ids: list[str]) -> dict[str, Any]:
    rows = conn.execute(
        text(
            "SELECT DISTINCT ON (member_id) member_id, version, phone_e164, recorded_by, recorded_at FROM "
            "member_contacts "
            "WHERE fpo_id = :f AND member_id = ANY(:ids) ORDER BY member_id, version DESC"
        ),
        {"f": p.fpo_id, "ids": member_ids},
    ).all()
    return {r.member_id: r for r in rows}


def _current_consents(conn: Connection, p: Principal, member_ids: list[str]) -> dict[str, dict[str, Any]]:
    rows = conn.execute(
        text(
            "SELECT DISTINCT ON (member_id, kind) member_id, kind, action, event_no, notice_version, method, "
            "recorded_at "
            "FROM member_consents WHERE fpo_id = :f AND member_id = ANY(:ids) ORDER BY member_id, kind, event_no DESC"
        ),
        {"f": p.fpo_id, "ids": member_ids},
    ).all()
    out: dict[str, dict[str, Any]] = defaultdict(dict)
    for r in rows:
        out[r.member_id][r.kind] = r
    return out


def _member(conn: Connection, p: Principal, member_id: str) -> Any:
    return conn.execute(
        text("SELECT id, display_name, preferred_language, active FROM members WHERE id = :i AND fpo_id = :f"),
        {"i": member_id, "f": p.fpo_id},
    ).first()


@router.get("/contacts")
def contacts(request: Request, p: Read) -> list[dict[str, Any]]:
    show = _can_see_phone(p)
    with tenant_scope(request, p) as conn:
        members = conn.execute(
            text(
                "SELECT id, display_name, preferred_language, active FROM members WHERE fpo_id = :f ORDER BY "
                "display_name, id"
            ),
            {"f": p.fpo_id},
        ).all()
        ids = [m.id for m in members]
        cur = _current_contacts(conn, p, ids)
        cons = _current_consents(conn, p, ids)
        if show and cur:
            _log_view(conn, request, p, sorted(cur), "PHONE_VIEWED")
    return [
        {
            "contract": "messaging.contact_summary/v1",
            "member_id": m.id,
            "display_name": m.display_name,
            "language": m.preferred_language,
            "active": m.active,
            "contact": _contact_doc(cur[m.id], show) if m.id in cur else None,
            "consents": {k: cons[m.id][k].action if k in cons.get(m.id, {}) else "NONE" for k in KINDS},
        }
        for m in members
    ]


@router.post("/members/{member_id}/contact", status_code=201)
def add_contact(member_id: str, body: ContactBody, request: Request, p: Write) -> dict[str, Any]:
    try:
        phone = normalise_phone(body.phone)
    except PhoneInvalid as e:
        raise _invalid("PHONE_INVALID", "enter a valid mobile number", "/phone") from e
    show = _can_see_phone(p)
    missing = False
    with tenant_scope(request, p) as conn:
        conn.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:m, 1))"), {"m": member_id})
        if _member(conn, p, member_id) is None:
            missing = True
        else:
            now = _current_contacts(conn, p, [member_id]).get(member_id)
            if now is not None and now.phone_e164 == phone:
                raise _conflict("NO_CHANGE", "this is already the current number")
            row = conn.execute(
                text(
                    "INSERT INTO member_contacts (member_id, fpo_id, version, phone_e164, recorded_by, request_id) "
                    "VALUES (:m, :f, :v, :ph, :u, :r) RETURNING member_id, version, phone_e164, recorded_by, "
                    "recorded_at"
                ),
                {
                    "m": member_id,
                    "f": p.fpo_id,
                    "v": 1 if now is None else now.version + 1,
                    "ph": phone,
                    "u": p.user_id,
                    "r": request.state.request_id,
                },
            ).one()
            if show:
                _log_view(conn, request, p, [member_id], "PHONE_VIEWED")
    if missing:
        raise _missing(request, p, "members", member_id, "member")
    return _contact_doc(row, show)


@router.get("/members/{member_id}/contact/history")
def contact_history(
    member_id: str, request: Request, p: Annotated[Principal, Depends(require("contacts.view_phone"))]
) -> list[dict[str, Any]]:
    with tenant_scope(request, p) as conn:
        found = _member(conn, p, member_id) is not None
        rows = conn.execute(
            text(
                "SELECT member_id, version, phone_e164, recorded_by, recorded_at FROM member_contacts "
                "WHERE member_id = :m AND fpo_id = :f ORDER BY version DESC"
            ),
            {"m": member_id, "f": p.fpo_id},
        ).all()
        if found and rows:
            _log_view(conn, request, p, [member_id], "PHONE_HISTORY_VIEWED")
    if not found:
        raise _missing(request, p, "members", member_id, "member")
    return [_contact_doc(r, True) for r in rows]


def _consent_doc(r: Any) -> dict[str, Any]:
    return {
        "contract": "messaging.consent_event/v1",
        "member_id": r.member_id,
        "event_no": r.event_no,
        "kind": r.kind,
        "action": r.action,
        "notice_version": r.notice_version,
        "method": r.method,
        "recorded_by": r.recorded_by,
        "recorded_at": format_instant(r.recorded_at),
    }


@router.post("/members/{member_id}/consents", status_code=201)
def add_consent(member_id: str, body: ConsentBody, request: Request, p: Write) -> dict[str, Any]:
    missing = False
    with tenant_scope(request, p) as conn:
        conn.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:m, 2))"), {"m": member_id})
        if _member(conn, p, member_id) is None:
            missing = True
        else:
            now = _current_consents(conn, p, [member_id]).get(member_id, {}).get(body.kind)
            state = "NONE" if now is None else now.action
            if state == body.action or (state == "NONE" and body.action == "WITHDRAWN"):
                raise _conflict("NO_CHANGE", f"this consent is already {state}")
            n = conn.execute(
                text("SELECT COALESCE(max(event_no), 0) + 1 FROM member_consents WHERE member_id = :m"),
                {"m": member_id},
            ).scalar_one()
            row = conn.execute(
                text(
                    "INSERT INTO member_consents (member_id, fpo_id, event_no, kind, action, notice_version, method, "
                    "recorded_by, request_id) VALUES (:m, :f, :n, :k, :a, :nv, :me, :u, :r) "
                    "RETURNING member_id, event_no, kind, action, notice_version, method, recorded_by, recorded_at"
                ),
                {
                    "m": member_id,
                    "f": p.fpo_id,
                    "n": n,
                    "k": body.kind,
                    "a": body.action,
                    "nv": body.notice_version,
                    "me": body.method,
                    "u": p.user_id,
                    "r": request.state.request_id,
                },
            ).one()
    if missing:
        raise _missing(request, p, "members", member_id, "member")
    return _consent_doc(row)


@router.get("/members/{member_id}/consents")
def get_consents(member_id: str, request: Request, p: Read) -> dict[str, Any]:
    with tenant_scope(request, p) as conn:
        found = _member(conn, p, member_id) is not None
        rows = conn.execute(
            text(
                "SELECT member_id, event_no, kind, action, notice_version, method, recorded_by, recorded_at "
                "FROM member_consents WHERE member_id = :m AND fpo_id = :f ORDER BY event_no"
            ),
            {"m": member_id, "f": p.fpo_id},
        ).all()
    if not found:
        raise _missing(request, p, "members", member_id, "member")
    latest_by = {r.kind: r for r in rows}
    return {
        "contract": "messaging.consent_state/v1",
        "member_id": member_id,
        "current": [
            {
                "kind": k,
                "state": latest_by[k].action if k in latest_by else "NONE",
                "since": format_instant(latest_by[k].recorded_at) if k in latest_by else None,
                "notice_version": latest_by[k].notice_version if k in latest_by else None,
                "method": latest_by[k].method if k in latest_by else None,
            }
            for k in KINDS
        ],
        "history": [_consent_doc(r) for r in rows],
    }


# ---- templates ----
@router.get("/message-templates")
def message_templates(p: Read) -> list[dict[str, Any]]:
    return [
        {
            "contract": "messaging.template/v1",
            "id": t.id,
            "version": t.version,
            "languages": [
                {
                    "language": lang,
                    "review_status": v.review_status,
                    "reviewer": v.reviewer,
                    "reviewed_at": v.reviewed_at,
                    "text_sha256": v.text_sha256,
                }
                for lang, v in sorted(t.variants.items())
            ],
        }
        for t in all_templates()
    ]


# ---- instructions ----
INS_COLUMNS = (
    "id, plan_id, revision_no, member_id, kind, version, status, issues, language, template_id, template_version, "
    "template_review, template_sha256, body, facts, contact_version, consent_event_no, supersedes, "
    "prepared_by, prepared_at"
)


def _deliveries(conn: Connection, p: Principal, ids: list[str]) -> dict[str, dict[str, Any]]:
    rows = conn.execute(
        text(
            "SELECT instruction_id, count(*) AS n, max(attempted_at) AS last_at, "
            "(array_agg(status ORDER BY attempted_at DESC, id DESC))[1] AS last_status "
            "FROM delivery_attempts WHERE fpo_id = :f AND instruction_id = ANY(:ids) GROUP BY instruction_id"
        ),
        {"f": p.fpo_id, "ids": ids},
    ).all()
    return {
        r.instruction_id: {"state": r.last_status, "attempts": r.n, "last_attempt_at": format_instant(r.last_at)}
        for r in rows
    }


def _ins_doc(r: Any, delivery: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "contract": "messaging.instruction/v1",
        "id": r.id,
        "plan_id": r.plan_id,
        "revision_no": r.revision_no,
        "member_id": r.member_id,
        "kind": r.kind,
        "version": r.version,
        "status": r.status,
        "issues": list(r.issues),
        "language": r.language,
        "template": (
            None
            if r.template_id is None
            else {
                "id": r.template_id,
                "version": r.template_version,
                "review_status": r.template_review,
                "text_sha256": r.template_sha256,
            }
        ),
        "text": r.body,
        "facts": r.facts,
        "contact_version": r.contact_version,
        "consent_event_no": r.consent_event_no,
        "supersedes": r.supersedes,
        "prepared_by": r.prepared_by,
        "prepared_at": format_instant(r.prepared_at),
        "delivery": delivery or {"state": "NOT_SENT", "attempts": 0, "last_attempt_at": None},
    }


def _latest_rows(conn: Connection, p: Principal, plan_id: str | None) -> list[Any]:
    """The newest version per (plan, member, kind)."""
    return list(
        conn.execute(
            text(
                f"SELECT DISTINCT ON (plan_id, member_id, kind) {INS_COLUMNS} FROM instructions "
                "WHERE fpo_id = :f AND (CAST(:p AS text) IS NULL OR plan_id = :p) "
                "ORDER BY plan_id, member_id, kind, version DESC"
            ),
            {"f": p.fpo_id, "p": plan_id},
        )
    )


def _docs(conn: Connection, p: Principal, rows: list[Any]) -> list[dict[str, Any]]:
    d = _deliveries(conn, p, [r.id for r in rows]) if rows else {}
    return [_ins_doc(r, d.get(r.id)) for r in rows]


def _member_inputs(
    conn: Connection, p: Principal, member_ids: list[str]
) -> tuple[dict[str, Any], dict[str, Any], dict[str, dict[str, Any]]]:
    members = {
        r.id: r
        for r in conn.execute(
            text(
                "SELECT id, display_name, preferred_language, active FROM members WHERE fpo_id = :f AND id = ANY(:ids)"
            ),
            {"f": p.fpo_id, "ids": member_ids},
        )
    }
    return members, _current_contacts(conn, p, member_ids), _current_consents(conn, p, member_ids)


def _facts_of(m: Any) -> MemberFacts:
    return MemberFacts(m.id, m.display_name, m.active, m.preferred_language)


def _consent_facts(per_kind: dict[str, Any]) -> ConsentFacts:
    c = per_kind.get("WHATSAPP")
    return ConsentFacts("NONE" if c is None else c.action, None if c is None else c.event_no)


@router.post("/plans/{plan_id}/instructions", status_code=201)
def prepare(plan_id: str, body: PrepareBody, request: Request, p: Sender) -> list[dict[str, Any]]:
    missing = False
    now = datetime.now(UTC)
    with tenant_scope(request, p) as conn:
        _lock(conn, plan_id)
        plan = _plan(conn, p, plan_id)
        if plan is None:
            missing = True
        else:
            state = _states(conn, p, [plan])[0]
            approved = state["status"] == "APPROVED" and state["decision"] is not None
            if body.kind == "SALE_INSTRUCTION" and not (approved and state["reservation"]["state"] == "ACTIVE"):
                raise _conflict("NOT_APPROVED", "the plan is not approved with an active reservation")
            if body.kind == "CANCELLATION" and not (approved and state["reservation"]["state"] == "RELEASED"):
                raise _conflict("NOT_RELEASED", "the plan's reservation has not been released")
            decision_no = state["decision"]["revision_no"]
            revision = next(r for r in _revisions(conn, p, plan) if r["revision_no"] == decision_no)
            mandi_names = {m["mandi_id"]: m["name"] for m in plan["mandis"]}
            owner = {r["lot_id"]: r["member_id"] for r in plan["lots"]}
            lines: dict[str, list[LineFacts]] = defaultdict(list)
            supersedes_of: dict[str, str] = {}
            if body.kind == "SALE_INSTRUCTION":
                for a in revision["assignments"]:
                    lines[owner[a["lot_id"]]].append(
                        LineFacts(
                            a["lot_id"],
                            a["mandi_id"],
                            mandi_names.get(a["mandi_id"], a["mandi_id"]),
                            date.fromisoformat(a["date"]),
                            a["quantity_kg"],
                        )
                    )
            else:
                reached = conn.execute(
                    text(
                        "SELECT DISTINCT ON (i.member_id) i.id, i.member_id, i.facts FROM instructions i "
                        "WHERE i.fpo_id = :f AND i.plan_id = :p AND i.kind = 'SALE_INSTRUCTION' "
                        "AND i.status = 'PREPARED' "
                        "AND EXISTS (SELECT 1 FROM delivery_attempts a WHERE a.instruction_id = i.id AND a.status = "
                        "ANY(:ok)) "
                        "ORDER BY i.member_id, i.version DESC"
                    ),
                    {"f": p.fpo_id, "p": plan_id, "ok": list(REACHED)},
                ).all()
                if not reached:
                    raise _invalid(
                        "NOTHING_TO_CANCEL", "no farmer has received (or previewed) the instruction of this plan"
                    )
                for r in reached:
                    supersedes_of[r.member_id] = r.id
                    for ln in r.facts["lines"]:
                        lines[r.member_id].append(
                            LineFacts(
                                ln["lot_id"],
                                ln["mandi_id"],
                                ln["mandi_name"],
                                date.fromisoformat(ln["date"]),
                                ln["quantity_kg"],
                            )
                        )
            member_ids = sorted(lines)
            members, cons, consents = _member_inputs(conn, p, member_ids)
            template = latest(body.kind)
            existing = {(r.member_id): r for r in _latest_rows(conn, p, plan_id) if r.kind == body.kind}
            for mid in member_ids:
                mem = _facts_of(members[mid])
                contact = cons.get(mid)
                contact_facts = None if contact is None else ContactFacts(contact.version)
                consent = _consent_facts(consents.get(mid, {}))
                prior = existing.get(mid)
                if prior is not None and prior.status == "PREPARED" and prior.language == mem.language:
                    continue  # one instruction per farmer; it is replaced only if the recorded language has changed
                if prior is not None and prior.status == "BLOCKED":
                    still = eligibility(
                        mem, contact_facts, consent, template is not None and mem.language in template.variants
                    )
                    if still == list(prior.issues):
                        continue
                lines[mid].sort(key=lambda x: (x.day, x.mandi_name, x.lot_id))
                doc = build_instruction(
                    instruction_id=new_local_id("ins"),
                    plan_id=plan_id,
                    revision_no=decision_no,
                    kind=body.kind,
                    version=1 if prior is None else prior.version + 1,
                    supersedes=(prior.id if prior is not None else supersedes_of.get(mid)),
                    member=mem,
                    contact=contact_facts,
                    consent=consent,
                    variety=plan["variety"],
                    lines=lines[mid],
                    template=template,
                    prepared_by=p.user_id,
                    now=now,
                )
                conn.execute(
                    text(
                        "INSERT INTO instructions (id, plan_id, fpo_id, member_id, revision_no, kind, version, status, "
                        "issues, "
                        "language, template_id, template_version, template_review, template_sha256, body, facts, "
                        "contact_version, "
                        "consent_event_no, supersedes, prepared_by, prepared_at, request_id) "
                        "VALUES (:id, :p, :f, :m, :rn, :k, :v, :s, :iss, :lang, :ti, :tv, :tr, :th, :b, CAST(:facts AS "
                        "jsonb), :cv, :ce, :sup, :u, :t, :r)"
                    ),
                    {
                        "id": doc["id"],
                        "p": plan_id,
                        "f": p.fpo_id,
                        "m": mid,
                        "rn": decision_no,
                        "k": body.kind,
                        "v": doc["version"],
                        "s": doc["status"],
                        "iss": doc["issues"],
                        "lang": doc["language"],
                        "ti": doc["template"]["id"] if doc["template"] else None,
                        "tv": doc["template"]["version"] if doc["template"] else None,
                        "tr": doc["template"]["review_status"] if doc["template"] else None,
                        "th": doc["template"]["text_sha256"] if doc["template"] else None,
                        "b": doc["text"],
                        "facts": json.dumps(doc["facts"]),
                        "cv": doc["contact_version"],
                        "ce": doc["consent_event_no"],
                        "sup": doc["supersedes"],
                        "u": p.user_id,
                        "t": now,
                        "r": request.state.request_id,
                    },
                )
            out = [r for r in _latest_rows(conn, p, plan_id) if r.kind == body.kind]
            result = _docs(conn, p, out)
    if missing:
        raise _missing(request, p, "plans", plan_id, "plan")
    return result


@router.get("/plans/{plan_id}/instructions")
def plan_instructions(plan_id: str, request: Request, p: Read) -> list[dict[str, Any]]:
    with tenant_scope(request, p) as conn:
        plan = _plan(conn, p, plan_id)
        docs = None if plan is None else _docs(conn, p, _latest_rows(conn, p, plan_id))
    if docs is None:
        raise _missing(request, p, "plans", plan_id, "plan")
    return docs


@router.get("/instructions/{instruction_id}")
def get_instruction(instruction_id: str, request: Request, p: Read) -> dict[str, Any]:
    with tenant_scope(request, p) as conn:
        row = conn.execute(
            text(f"SELECT {INS_COLUMNS} FROM instructions WHERE id = :i AND fpo_id = :f"),
            {"i": instruction_id, "f": p.fpo_id},
        ).first()
        doc = None if row is None else _docs(conn, p, [row])[0]
    if doc is None:
        raise _missing(request, p, "instructions", instruction_id, "instruction")
    return doc


def _attempt_doc(r: Any) -> dict[str, Any]:
    return {
        "contract": "messaging.delivery_attempt/v1",
        "id": r.id,
        "instruction_id": r.instruction_id,
        "channel": r.channel,
        "status": r.status,
        "issues": list(r.issues),
        "provider_message_id": r.provider_message_id,
        "simulated": r.simulated,
        "preview": r.preview,
        "attempted_by": r.attempted_by,
        "attempted_at": format_instant(r.attempted_at),
    }


ATT_COLUMNS = (
    "id, instruction_id, channel, status, issues, provider_message_id, simulated, preview, attempted_by, "
    "attempted_at, request_key, request_sha256"
)


@router.get("/instructions/{instruction_id}/attempts")
def instruction_attempts(instruction_id: str, request: Request, p: Read) -> list[dict[str, Any]]:
    with tenant_scope(request, p) as conn:
        found = conn.execute(
            text("SELECT 1 FROM instructions WHERE id = :i AND fpo_id = :f"), {"i": instruction_id, "f": p.fpo_id}
        ).first()
        rows = conn.execute(
            text(
                f"SELECT {ATT_COLUMNS} FROM delivery_attempts WHERE instruction_id = :i AND fpo_id = :f ORDER BY "
                f"attempted_at DESC, id DESC"
            ),
            {"i": instruction_id, "f": p.fpo_id},
        ).all()
    if not found:
        raise _missing(request, p, "instructions", instruction_id, "instruction")
    return [_attempt_doc(r) for r in rows]


@router.post("/instructions/{instruction_id}/send", status_code=201)
def send(
    instruction_id: str, body: SendBody, request: Request, p: Sender, idempotency_key: IdemKey = None
) -> dict[str, Any]:
    if body.channel not in CHANNELS:
        raise _invalid(
            "CHANNEL_NOT_AVAILABLE", f"the {body.channel} channel is not available in this version", "/channel"
        )
    sha = (
        None
        if idempotency_key is None
        else hashlib.sha256(
            json.dumps(["POST send", instruction_id, body.model_dump()], sort_keys=True).encode()
        ).hexdigest()
    )
    missing = False
    with tenant_scope(request, p) as conn:
        conn.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:i, 3))"), {"i": instruction_id})
        row = conn.execute(
            text(f"SELECT {INS_COLUMNS} FROM instructions WHERE id = :i AND fpo_id = :f"),
            {"i": instruction_id, "f": p.fpo_id},
        ).first()
        if row is None:
            missing = True
        else:
            if idempotency_key is not None:
                prior = conn.execute(
                    text(f"SELECT {ATT_COLUMNS} FROM delivery_attempts WHERE fpo_id = :f AND request_key = :k"),
                    {"f": p.fpo_id, "k": idempotency_key},
                ).first()
                if prior is not None:
                    if prior.request_sha256 != sha:
                        raise AccessError(
                            ErrorCode.IDEMPOTENCY_KEY_REUSED, "this Idempotency-Key was used for a different request"
                        )
                    return _attempt_doc(prior)
            if row.status == "BLOCKED":
                raise _conflict(
                    "INSTRUCTION_BLOCKED", "this instruction is blocked; prepare it again once the blockers are fixed"
                )
            last = conn.execute(
                text(
                    "SELECT status FROM delivery_attempts WHERE instruction_id = :i AND channel = :c ORDER BY "
                    "attempted_at DESC, id DESC LIMIT 1"
                ),
                {"i": instruction_id, "c": body.channel},
            ).first()
            if last is not None and last.status in SENT_STATUSES:
                raise _conflict("ALREADY_SENT", "this instruction was already sent on this channel")
            # the same checks as at preparation, now, plus the two that can only change afterwards
            member = _member(conn, p, row.member_id)
            contact = _current_contacts(conn, p, [row.member_id]).get(row.member_id)
            consent = _consent_facts(_current_consents(conn, p, [row.member_id]).get(row.member_id, {}))
            issues = eligibility(
                _facts_of(member), None if contact is None else ContactFacts(contact.version), consent, True
            )
            if member.preferred_language != row.language:
                issues.append("LANGUAGE_CHANGED")
            if row.kind == "SALE_INSTRUCTION":
                plan = _plan(conn, p, row.plan_id)
                if plan is None or _states(conn, p, [plan])[0]["reservation"]["state"] != "ACTIVE":
                    issues.append("COMMITMENT_NOT_ACTIVE")
            result = None
            if not issues:
                assert contact is not None and row.body is not None
                result = CHANNELS[body.channel].deliver(
                    text=row.body, language=row.language, recipient_phone=contact.phone_e164
                )
            att = conn.execute(
                text(
                    "INSERT INTO delivery_attempts (id, instruction_id, fpo_id, channel, status, issues, "
                    "provider_message_id, simulated, "
                    "preview, contact_version, attempted_by, request_id, request_key, request_sha256) "
                    "VALUES (:id, :i, :f, :c, :s, :iss, :pm, :sim, CAST(:pv AS jsonb), :cv, :u, :r, :k, :h) "
                    f"RETURNING {ATT_COLUMNS}"
                ),
                {
                    "id": new_local_id("att"),
                    "i": instruction_id,
                    "f": p.fpo_id,
                    "c": body.channel,
                    "s": "BLOCKED" if result is None else result.status,
                    "iss": issues,
                    "pm": None if result is None else result.provider_message_id,
                    "sim": True if result is None else result.simulated,
                    "pv": json.dumps(None if result is None else result.preview),
                    "cv": None if contact is None else contact.version,
                    "u": p.user_id,
                    "r": request.state.request_id,
                    "k": idempotency_key,
                    "h": sha,
                },
            ).one()
    if missing:
        raise _missing(request, p, "instructions", instruction_id, "instruction")
    return _attempt_doc(att)


@router.get("/instruction-queue")
def queue(request: Request, p: Read) -> list[dict[str, Any]]:
    """Instructions that need a person: blocked, or whose last attempt was blocked or failed."""
    with tenant_scope(request, p) as conn:
        docs = _docs(conn, p, _latest_rows(conn, p, None))
    return [d for d in docs if d["status"] == "BLOCKED" or d["delivery"]["state"] in ("BLOCKED", "FAILED")]


def install(app: FastAPI) -> None:
    app.include_router(router)

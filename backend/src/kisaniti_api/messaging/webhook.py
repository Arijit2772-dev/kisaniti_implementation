"""WhatsApp Cloud API status webhook (messaging/v1 amendment A1.1).

GET answers Meta's verification. POST is accepted only with a valid X-Hub-Signature-256 (HMAC-SHA256 of the raw
body with the app secret). Each status becomes an append-only delivery event of the attempt with that provider message
ID, in the FPO that owns it. The recipient number in the event is never stored or logged. A farmer's text message
(A1.3) becomes a reply on the instruction it answers; one from a number we never sent to is only counted (D3) and its
number is never stored or logged. Settings come from the server's environment, never from git:
KN_WA_VERIFY_TOKEN and KN_WA_APP_SECRET; without them both endpoints answer 503 CHANNEL_NOT_AVAILABLE.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
from collections import Counter
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from kisaniti_core.ids import new_local_id
from sqlalchemy import text

from ..access.deps import engine
from ..access.service import auth_conn
from .status import PROVIDER, effective

router = APIRouter(prefix="/api/v1/webhooks")
log = logging.getLogger("kisaniti_api.whatsapp_webhook")


def _settings() -> tuple[str, str] | None:
    token, secret = os.environ.get("KN_WA_VERIFY_TOKEN", ""), os.environ.get("KN_WA_APP_SECRET", "")
    return (token, secret) if token and secret else None


def _unavailable() -> JSONResponse:
    return JSONResponse(
        {"code": "CHANNEL_NOT_AVAILABLE", "detail": "the WhatsApp channel is not configured on this server"}, 503
    )


@router.get("/whatsapp", response_model=None)
def verify(request: Request) -> PlainTextResponse | JSONResponse:
    s = _settings()
    if s is None:
        return _unavailable()
    q = request.query_params
    if q.get("hub.mode") == "subscribe" and hmac.compare_digest(q.get("hub.verify_token", ""), s[0]):
        return PlainTextResponse(q.get("hub.challenge", ""))
    return JSONResponse({"code": "FORBIDDEN", "detail": "verification refused"}, 403)


@router.post("/whatsapp", response_model=None)
async def receive(request: Request) -> JSONResponse:
    s = _settings()
    if s is None:
        return _unavailable()
    raw = await request.body()
    want = "sha256=" + hmac.new(s[1].encode(), raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(request.headers.get("x-hub-signature-256", ""), want):
        log.warning("webhook signature refused", extra={"request_id": request.state.request_id})
        return JSONResponse({"code": "UNAUTHENTICATED", "detail": "bad or missing signature"}, 401)
    try:
        doc = json.loads(raw)
        values = [
            c["value"] for e in doc.get("entry", []) for c in e.get("changes", []) if c.get("field") == "messages"
        ]
    except (ValueError, TypeError, KeyError, AttributeError):
        return JSONResponse({"code": "INVALID_REQUEST", "detail": "not a WhatsApp webhook body"}, 400)
    body_sha = hashlib.sha256(raw).hexdigest()
    outcomes: Counter[str] = Counter()
    with auth_conn(engine()) as conn:  # finds the attempt across FPOs; each event is written with its owner's fpo_id
        for v in values:
            for msg in v.get("messages") or []:
                outcomes[_reply(conn, msg, request.state.request_id)] += 1
            for st in v.get("statuses") or []:
                outcomes[_record(conn, st, body_sha, request.state.request_id)] += 1
    log.info("webhook processed", extra={"request_id": request.state.request_id})
    return JSONResponse({"outcomes": dict(sorted(outcomes.items()))}, 200)


def _record(conn: Any, st: dict[str, Any], body_sha: str, request_id: str) -> str:
    status = PROVIDER.get(str(st.get("status", "")))
    if status is None:
        return "UNRECOGNISED_STATUS"
    try:
        at = datetime.fromtimestamp(int(st["timestamp"]), UTC)
        codes = [int(e["code"]) for e in st.get("errors") or []]
        provider_id = str(st["id"])
    except (KeyError, TypeError, ValueError):
        return "UNRECOGNISED_STATUS"
    att = conn.execute(
        text("SELECT id, fpo_id FROM delivery_attempts WHERE channel = 'WHATSAPP' AND provider_message_id = :m"),
        {"m": provider_id},
    ).first()
    if att is None:
        return "UNKNOWN_MESSAGE"
    own = conn.execute(text("SELECT status FROM delivery_attempts WHERE id = :a"), {"a": att.id}).scalar_one()
    seen = [
        r.status for r in conn.execute(text("SELECT status FROM delivery_events WHERE attempt_id = :a"), {"a": att.id})
    ]
    before = effective(own, seen)
    row = conn.execute(
        text(
            "INSERT INTO delivery_events (attempt_id, fpo_id, status, provider_at, error_codes, body_sha256, "
            "request_id) VALUES (:a, :f, :s, :t, :c, :h, :r) ON CONFLICT (attempt_id, status) DO NOTHING RETURNING id"
        ),
        {"a": att.id, "f": att.fpo_id, "s": status, "t": at, "c": codes, "h": body_sha, "r": request_id},
    ).first()
    if row is None:
        return "DUPLICATE"
    return "RECORDED" if effective(before, [status]) != before else "IGNORED_BACKWARDS"


MAX_REPLY = 500


def _reply(conn: Any, msg: dict[str, Any], request_id: str) -> str:
    """A1.3. The sender must be a number one of our WhatsApp messages went to. With Meta's reply context, the reply
    belongs to that message; without it, to the newest message sent to that number. A number that more than one FPO
    has sent to, with no context, is ambiguous and is not stored."""
    try:
        sender = "+" + str(msg["from"])
        provider_id = str(msg["id"])
        kind = str(msg.get("type", ""))
    except (KeyError, TypeError):
        return "REPLY_UNSUPPORTED"
    if not 1 <= len(provider_id) <= 200:
        return "REPLY_UNSUPPORTED"
    sent = conn.execute(
        text(
            "SELECT a.fpo_id, a.instruction_id, i.member_id, a.provider_message_id FROM delivery_attempts a "
            "JOIN instructions i ON i.id = a.instruction_id AND i.fpo_id = a.fpo_id "
            "JOIN member_contacts c ON c.member_id = i.member_id AND c.fpo_id = a.fpo_id "
            "AND c.version = a.contact_version "
            "WHERE a.channel = 'WHATSAPP' AND a.provider_message_id IS NOT NULL AND c.phone_e164 = :p "
            "ORDER BY a.attempted_at DESC, a.id DESC"
        ),
        {"p": sender},
    ).all()
    if not sent:
        return "REPLY_UNMATCHED"  # D3: counted only; the stranger's number goes nowhere
    context = (msg.get("context") or {}).get("id") if isinstance(msg.get("context"), dict) else None
    target = next((r for r in sent if context and r.provider_message_id == context), None)
    if target is None:
        if len({r.fpo_id for r in sent}) > 1:
            return "REPLY_AMBIGUOUS"
        target = sent[0]
    if kind != "text" or not isinstance(msg.get("text"), dict):
        return "REPLY_UNSUPPORTED"  # media, buttons, locations: excluded by the amendment
    body = str(msg["text"].get("body", ""))
    if not body.strip():
        return "REPLY_UNSUPPORTED"
    if len(body) > MAX_REPLY:
        return "REPLY_TOO_LONG"
    row = conn.execute(
        text(
            "INSERT INTO replies (id, fpo_id, instruction_id, member_id, body, source, recorded_by, request_id, "
            "provider_message_id) VALUES (:id, :f, :i, :m, :b, 'WEBHOOK', NULL, :r, :pm) "
            "ON CONFLICT (provider_message_id) WHERE provider_message_id IS NOT NULL DO NOTHING RETURNING id"
        ),
        {
            "id": new_local_id("rpl"),
            "f": target.fpo_id,
            "i": target.instruction_id,
            "m": target.member_id,
            "b": body,
            "r": request_id,
            "pm": provider_id,
        },
    ).first()
    return "REPLY_RECORDED" if row is not None else "REPLY_DUPLICATE"


def install(app: FastAPI) -> None:
    app.include_router(router)

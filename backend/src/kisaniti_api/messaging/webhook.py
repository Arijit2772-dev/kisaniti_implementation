"""WhatsApp Cloud API status webhook (messaging/v1 amendment A1.1).

GET answers Meta's verification. POST is accepted only with a valid X-Hub-Signature-256 (HMAC-SHA256 of the raw
body with the app secret). Each status becomes an append-only delivery event of the attempt with that provider message
ID, in the FPO that owns it. The recipient number in the event is never stored or logged. Inbound farmer messages
(A1.3) are counted but not stored until decision D3. Settings come from the server's environment, never from git:
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
            if v.get("messages"):
                outcomes["INBOUND_NOT_STORED"] += len(v["messages"])  # A1.3 waits for decision D3
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


def install(app: FastAPI) -> None:
    app.include_router(router)

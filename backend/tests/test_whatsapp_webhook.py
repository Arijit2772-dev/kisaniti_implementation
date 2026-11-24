# ruff: noqa: F811, F401  (fixtures imported from other test modules by name: pytest uses them as arguments)
"""messaging/v1 amendment A1.1: the WhatsApp status webhook and the effective delivery status."""

from __future__ import annotations

import hashlib
import hmac
import itertools
import json
import logging
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from kisaniti_core.ids import new_local_id
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

from kisaniti_api.main import app
from kisaniti_api.messaging.status import apply, effective
from tests.test_access import Fpo, access_env, clean_up_access_data, two
from tests.test_messaging import FULL, PHONE_A, approved_plan, prepare, ready
from tests.test_org import call
from tests.test_plan import fresh_market, market

SECRET, TOKEN = "fictional-app-secret-for-tests", "fictional-verify-token"
URL = "/api/v1/webhooks/whatsapp"


@pytest.fixture()
def configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KN_WA_APP_SECRET", SECRET)
    monkeypatch.setenv("KN_WA_VERIFY_TOKEN", TOKEN)


def post(body: Any, secret: str = SECRET, sign: bool = True) -> Any:
    raw = json.dumps(body).encode()
    headers = {"Content-Type": "application/json"}
    if sign:
        headers["X-Hub-Signature-256"] = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return TestClient(app, raise_server_exceptions=False).post(URL, content=raw, headers=headers)


def statuses(*items: tuple[str, str], recipient: str = "919000000001") -> dict[str, Any]:
    """A Cloud API webhook body in Meta's shape, with fictional IDs."""
    sts = [{"id": m, "status": s, "timestamp": "1764000000", "recipient_id": recipient} for m, s in items]
    value = {"messaging_product": "whatsapp", "metadata": {"phone_number_id": "000"}, "statuses": sts}
    return {
        "object": "whatsapp_business_account",
        "entry": [{"id": "0", "changes": [{"field": "messages", "value": value}]}],
    }


def whatsapp_attempt(engine: Engine, a: Fpo, market: dict[str, Any]) -> tuple[str, str]:
    """An instruction and a QUEUED WhatsApp attempt for it, as the send path (A1.2) will write it."""
    plan, lots = approved_plan(a, market, ("1250.000",))
    ready(a, lots[0]["member_id"], PHONE_A)
    ins = prepare(a.admin, plan["id"]).json()[0]
    wamid = f"wamid.fictional.{uuid.uuid4().hex}"
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO delivery_attempts (id, instruction_id, fpo_id, channel, status, provider_message_id, "
                "simulated, contact_version, attempted_by, request_id) VALUES (:id, :i, :f, 'WHATSAPP', 'QUEUED', :m, "
                "false, 1, :u, 'test-request-0001')"
            ),
            {"id": new_local_id("att"), "i": ins["id"], "f": a.fpo_id, "m": wamid, "u": a.admin_id},
        )
    return ins["id"], wamid


# ---- the rule, on its own ----
LIFECYCLE = ["SENT", "DELIVERED", "READ", "FAILED"]


def test_status_never_moves_backwards_and_failed_only_before_delivered() -> None:
    assert apply("QUEUED", "SENT") == "SENT" and apply("SENT", "SENT") == "SENT"
    assert apply("DELIVERED", "SENT") == "DELIVERED" and apply("READ", "DELIVERED") == "READ"
    assert apply("SENT", "FAILED") == "FAILED" and apply("DELIVERED", "FAILED") == "DELIVERED"
    assert apply("PREVIEWED", "READ") == "PREVIEWED" and apply("BLOCKED", "SENT") == "BLOCKED"


@pytest.mark.parametrize(
    "events",
    [list(p) for n in range(1, 5) for c in itertools.combinations(LIFECYCLE, n) for p in itertools.permutations(c)],
)
def test_the_effective_status_does_not_depend_on_the_order_events_arrive(events: list[str]) -> None:
    assert len({effective("QUEUED", list(p)) for p in itertools.permutations(events)}) == 1
    assert effective("QUEUED", events + events) == effective("QUEUED", events)  # repeats change nothing


# ---- the endpoint ----
def test_without_settings_the_webhook_is_not_available(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KN_WA_APP_SECRET", raising=False)
    monkeypatch.delenv("KN_WA_VERIFY_TOKEN", raising=False)
    c = TestClient(app, raise_server_exceptions=False)
    assert (
        c.get(URL, params={"hub.mode": "subscribe", "hub.verify_token": "x", "hub.challenge": "1"}).status_code == 503
    )
    assert post(statuses(("x", "sent"))).status_code == 503


def test_meta_verification_answers_the_challenge_only_with_the_right_token(configured: None) -> None:
    c = TestClient(app, raise_server_exceptions=False)
    ok = c.get(URL, params={"hub.mode": "subscribe", "hub.verify_token": TOKEN, "hub.challenge": "8231"})
    assert ok.status_code == 200 and ok.text == "8231"
    assert (
        c.get(URL, params={"hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "8231"}).status_code
        == 403
    )


def test_a_bad_or_missing_signature_stores_nothing(
    configured: None, two: tuple[Fpo, Fpo], market: dict[str, Any], engine: Engine
) -> None:
    a, _ = two
    _, wamid = whatsapp_attempt(engine, a, market)
    assert post(statuses((wamid, "delivered")), secret="another-secret").status_code == 401
    assert post(statuses((wamid, "delivered")), sign=False).status_code == 401
    with engine.connect() as conn:
        assert (
            conn.execute(
                text(
                    "SELECT count(*) FROM delivery_events e "
                    "JOIN delivery_attempts d ON d.id = e.attempt_id WHERE d.provider_message_id = :m"
                ),
                {"m": wamid},
            ).scalar_one()
            == 0
        )


def test_events_raise_the_status_repeats_and_backwards_events_change_nothing(
    configured: None, two: tuple[Fpo, Fpo], market: dict[str, Any], engine: Engine
) -> None:
    a, _ = two
    ins_id, wamid = whatsapp_attempt(engine, a, market)

    def state() -> str:
        return str(call(a.admin, "GET", f"/instructions/{ins_id}").json()["delivery"]["state"])

    assert state() == "QUEUED"
    r = post(statuses((wamid, "sent"), (wamid, "delivered")))
    assert r.status_code == 200 and r.json()["outcomes"] == {"RECORDED": 2}
    assert state() == "DELIVERED"
    r = post(
        statuses((wamid, "delivered"), (wamid, "failed"), (wamid, "read"), ("wamid.nobody", "read"), (wamid, "deleted"))
    )
    assert r.json()["outcomes"] == {
        "DUPLICATE": 1,
        "IGNORED_BACKWARDS": 1,
        "RECORDED": 1,
        "UNKNOWN_MESSAGE": 1,
        "UNRECOGNISED_STATUS": 1,
    }
    assert state() == "READ"
    att = call(a.admin, "GET", f"/instructions/{ins_id}/attempts").json()
    assert [x["status"] for x in att] == ["READ"]


def test_an_event_reaches_only_the_fpo_that_owns_the_message_and_no_number_is_stored_or_logged(
    configured: None, two: tuple[Fpo, Fpo], market: dict[str, Any], engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    a, b = two
    ins_id, wamid = whatsapp_attempt(engine, a, market)
    log = logging.getLogger("kisaniti_api")
    log.addHandler(caplog.handler)
    try:
        assert post(statuses((wamid, "delivered"), recipient=FULL[PHONE_A].lstrip("+"))).status_code == 200
    finally:
        log.removeHandler(caplog.handler)
    assert call(b.admin, "GET", f"/instructions/{ins_id}").status_code == 404
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT e.fpo_id, to_jsonb(e)::text AS doc FROM delivery_events e "
                "JOIN delivery_attempts d ON d.id = e.attempt_id WHERE d.provider_message_id = :m"
            ),
            {"m": wamid},
        ).one()
    assert row.fpo_id == a.fpo_id and PHONE_A not in row.doc
    assert not any(PHONE_A in r.getMessage() or PHONE_A in str(r.__dict__) for r in caplog.records)


def test_delivery_events_are_append_only(
    configured: None, two: tuple[Fpo, Fpo], market: dict[str, Any], engine: Engine
) -> None:
    a, _ = two
    _, wamid = whatsapp_attempt(engine, a, market)
    post(statuses((wamid, "sent")))
    for sql in ("UPDATE delivery_events SET status = 'READ'", "DELETE FROM delivery_events"):
        with pytest.raises(DBAPIError, match="append-only"), engine.begin() as conn:
            conn.execute(text(sql))

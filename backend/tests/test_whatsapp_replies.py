# ruff: noqa: F811, F401  (fixtures imported from other test modules by name: pytest uses them as arguments)
"""messaging/v1 amendment A1.3 with decision D3: farmers' WhatsApp replies arrive through the webhook.

Messages are first sent through the real send path, with Meta replaced by a fake (tests/test_whatsapp_send.py)."""

from __future__ import annotations

import logging
from typing import Any

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

from tests.test_access import Fpo, access_env, clean_up_access_data, two
from tests.test_messaging import FULL, PHONE_A, send
from tests.test_org import call
from tests.test_plan import fresh_market, market, valid
from tests.test_whatsapp_send import FakeMeta, meta, reviewed_v2, sendable
from tests.test_whatsapp_webhook import post

FARMER = FULL[PHONE_A].lstrip("+")
STRANGER = "919999999999"  # fictional; must never reach the database or the log


def inbound(
    msg_id: str, sender: str = FARMER, body: Any = "Theek hai ji", context: str | None = None, kind: str = "text"
) -> dict[str, Any]:
    """A Cloud API webhook body with one inbound message, in Meta's shape, fictional values."""
    m: dict[str, Any] = {"from": sender, "id": msg_id, "timestamp": "1764000000", "type": kind}
    if kind == "text":
        m["text"] = {"body": body}
    if context:
        m["context"] = {"from": "15550000000", "id": context}
    value = {
        "messaging_product": "whatsapp",
        "metadata": {"phone_number_id": "100000000000001"},
        "contacts": [{"profile": {"name": "fictional"}, "wa_id": sender}],
        "messages": [m],
    }
    return {
        "object": "whatsapp_business_account",
        "entry": [{"id": "0", "changes": [{"field": "messages", "value": value}]}],
    }


def sent(a: Fpo, market: dict[str, Any]) -> tuple[str, str]:
    ins = sendable(a, market)
    att = send(a.admin, ins["id"], channel="WHATSAPP").json()
    assert att["status"] == "QUEUED"
    return ins["id"], att["provider_message_id"]


def replies_of(a: Fpo, ins_id: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = call(a.admin, "GET", f"/instructions/{ins_id}/replies").json()
    return out


def test_a_farmer_s_reply_is_stored_once_on_the_message_it_answers(
    two: tuple[Fpo, Fpo], market: dict[str, Any], meta: FakeMeta, reviewed_v2: None
) -> None:
    a, _ = two
    ins_id, wamid = sent(a, market)
    assert post(inbound("wamid.IN.0001", context=wamid)).json()["outcomes"] == {"REPLY_RECORDED": 1}
    assert post(inbound("wamid.IN.0001", context=wamid)).json()["outcomes"] == {"REPLY_DUPLICATE": 1}  # Meta retries
    (r,) = replies_of(a, ins_id)
    valid("sales", "reply", r)
    assert (r["text"], r["source"], r["recorded_by"]) == ("Theek hai ji", "WEBHOOK", None)
    assert post(inbound("wamid.IN.0002", body="ਠੀਕ ਹੈ")).json()["outcomes"] == {"REPLY_RECORDED": 1}  # no context
    assert [x["text"] for x in replies_of(a, ins_id)] == ["ਠੀਕ ਹੈ", "Theek hai ji"]  # newest first


def test_a_stranger_is_only_counted_and_the_number_goes_nowhere(
    two: tuple[Fpo, Fpo],
    market: dict[str, Any],
    meta: FakeMeta,
    reviewed_v2: None,
    engine: Engine,
    caplog: pytest.LogCaptureFixture,
) -> None:
    a, _ = two
    ins_id, wamid = sent(a, market)
    log = logging.getLogger("kisaniti_api")
    log.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.DEBUG, logger="kisaniti_api"):
            out = post(inbound("wamid.IN.0003", sender=STRANGER, context=wamid)).json()
    finally:
        log.removeHandler(caplog.handler)
    assert out["outcomes"] == {"REPLY_UNMATCHED": 1}, "a reply context alone never makes a stranger a farmer"
    assert replies_of(a, ins_id) == []
    assert all(STRANGER not in rec.getMessage() for rec in caplog.records)
    with engine.connect() as c:
        assert (
            c.execute(text("SELECT count(*) FROM replies WHERE body LIKE '%' || :n || '%'"), {"n": STRANGER}).scalar()
            == 0
        )


@pytest.mark.parametrize(
    ("kwargs", "outcome"),
    [
        ({"kind": "image"}, "REPLY_UNSUPPORTED"),
        ({"body": "   "}, "REPLY_UNSUPPORTED"),
        ({"body": "x" * 501}, "REPLY_TOO_LONG"),
        ({"body": "x" * 500}, "REPLY_RECORDED"),
    ],
)
def test_only_text_of_at_most_500_characters_is_stored(
    two: tuple[Fpo, Fpo], market: dict[str, Any], meta: FakeMeta, reviewed_v2: None, kwargs: Any, outcome: str
) -> None:
    a, _ = two
    ins_id, _ = sent(a, market)
    assert post(inbound("wamid.IN.0004", **kwargs)).json()["outcomes"] == {outcome: 1}
    assert len(replies_of(a, ins_id)) == (1 if outcome == "REPLY_RECORDED" else 0)


def test_a_number_two_fpos_wrote_to_needs_the_reply_context_and_stays_in_its_own_fpo(
    two: tuple[Fpo, Fpo], market: dict[str, Any], meta: FakeMeta, reviewed_v2: None
) -> None:
    a, b = two
    a_ins, a_wamid = sent(a, market)
    b_ins, _ = sent(b, market)  # the same (fictional) number is a member's number in both FPOs
    assert post(inbound("wamid.IN.0005")).json()["outcomes"] == {"REPLY_AMBIGUOUS": 1}
    assert post(inbound("wamid.IN.0006", context=a_wamid)).json()["outcomes"] == {"REPLY_RECORDED": 1}
    assert [r["text"] for r in replies_of(a, a_ins)] == ["Theek hai ji"]
    assert replies_of(b, b_ins) == [] and call(b.admin, "GET", "/replies").json() == []
    assert call(b.admin, "GET", f"/instructions/{a_ins}/replies").status_code == 404


def test_the_database_requires_meta_s_id_exactly_on_webhook_replies(
    two: tuple[Fpo, Fpo], market: dict[str, Any], meta: FakeMeta, reviewed_v2: None, engine: Engine
) -> None:
    a, _ = two
    ins_id, _ = sent(a, market)
    with engine.connect() as c:
        member = c.execute(text("SELECT member_id FROM instructions WHERE id = :i"), {"i": ins_id}).scalar_one()
    for source, pm, by in (
        ("WEBHOOK", None, None),
        ("STAFF_ENTERED", "wamid.X", a.admin_id),
        ("WEBHOOK", "wamid.Y", a.admin_id),
    ):
        with pytest.raises(DBAPIError), engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO replies (id, fpo_id, instruction_id, member_id, body, source, recorded_by, "
                    "request_id, provider_message_id) VALUES ('rpl_01M5VFB9H0D4ZR6JVN7Y7ZQWJ9', :f, :i, :m, 'x', "
                    ":s, :u, 'test-request-0003', :pm)"
                ),
                {"f": a.fpo_id, "i": ins_id, "m": member, "s": source, "u": by, "pm": pm},
            )

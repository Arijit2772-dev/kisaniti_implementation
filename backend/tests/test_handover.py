# ruff: noqa: F811, F401  (fixtures imported from test_access by name: pytest uses them as arguments)
"""Handover slice (contract handover/v1) on real PostgreSQL: the manager's own WhatsApp, one wa.me link per farmer.
Fictional people and numbers only. The system sends nothing; what these tests pin down is who gets a link, that the
link carries exactly the stored text and number, what is logged, and that the manager's report is never delivery."""

from __future__ import annotations

import logging
import re
from dataclasses import replace
from typing import Any
from urllib.parse import unquote

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

from kisaniti_api.handover.routes import LABEL, link_for
from kisaniti_api.messaging import templates
from kisaniti_api.messaging.templates import Template
from tests.test_access import Fpo, Session, access_env, add_user, as_app, clean_up_access_data, two
from tests.test_approval import make_plan
from tests.test_messaging import (
    PHONE_A,
    PHONE_B,
    PHONE_C,
    approved_plan,
    by_member,
    consent,
    contact,
    prepare,
    ready,
    send,
    set_language,
)
from tests.test_org import call
from tests.test_plan import fresh_market, market, valid


@pytest.fixture(autouse=True)
def translations_unreviewed(monkeypatch: pytest.MonkeyPatch) -> None:
    """The shipped texts are all REVIEWED (by independent reviewers, 28 Oct 2026). These tests pin down the path a
    manager takes when a text is not: the preview counts it and the yes needs the acknowledgement. The reviewed state
    itself is tested in test_handover_reviewed.py."""
    real = templates._all()
    fake = {
        k: Template(
            t.id,
            t.version,
            {
                lang: replace(v, review_status="UNREVIEWED", reviewer=None, reviewed_at=None)
                for lang, v in t.variants.items()
            },
        )
        for k, t in real.items()
    }
    monkeypatch.setattr(templates, "_all", lambda: fake)


DIGITS = {PHONE_A: "919000000001", PHONE_B: "919000000002", PHONE_C: "919000000003"}
LINK = re.compile(r"^https://wa\.me/(?P<digits>[0-9]{8,15})\?text=(?P<text>[A-Za-z0-9%._~-]+)$")


def preview(s: Session, plan_id: str, kind: str = "SALE_INSTRUCTION") -> Any:
    return call(s, "GET", f"/plans/{plan_id}/handover-preview?kind={kind}")


def handover(
    s: Session, plan_id: str, count: int, kind: str = "SALE_INSTRUCTION", ack: bool = True, key: str | None = None
) -> Any:
    body = {"kind": kind, "recipient_count": count, "acknowledge_unreviewed": ack}
    return call(s, "POST", f"/plans/{plan_id}/handovers", body, key)


def report(s: Session, hid: str, ins_id: str, outcome: str) -> Any:
    return call(s, "POST", f"/handovers/{hid}/items/{ins_id}/outcome", {"outcome": outcome})


def parse(link: str) -> tuple[str, str]:
    m = LINK.match(link)
    assert m, link
    return m["digits"], unquote(m["text"])


def three_farmers(a: Fpo, market: dict[str, Any]) -> tuple[dict[str, Any], list[str], dict[str, dict[str, Any]]]:
    """An approved plan with three farmers (pa, hi, en). The first two have a contact and consent; the third has not."""
    plan, lots = approved_plan(a, market)
    mids = [lots[i]["member_id"] for i in range(3)]
    for mid, lang in zip(mids, ("pa", "hi", "en"), strict=True):
        set_language(a, mid, lang)
    ready(a, mids[0], PHONE_A)
    ready(a, mids[1], PHONE_B)
    docs = by_member(prepare(a.admin, plan["id"]).json())
    return plan, mids, docs


def access_rows(engine: Engine, a: Fpo, what: str = "PHONE_LINK_ISSUED") -> list[Any]:
    return as_app(engine, a.fpo_id, f"SELECT member_id FROM personal_data_access_log WHERE what = '{what}'")


# ---- the link itself ----
def test_a_link_carries_the_digits_and_the_exact_text_in_every_script() -> None:
    body = "ਸਤਿ ਸ੍ਰੀ ਅਕਾਲ Chaman ਜੀ।\n• 1200.000 ਕਿਲੋ, A&B Mandi (x) #1 100%?, 5 ਅਕਤੂਬਰ 2026 ਨੂੰ\nनमस्ते जी। 😀"
    link = link_for("+919000000001", body)
    digits, decoded = parse(link)
    assert digits == "919000000001" and decoded == body
    assert "+" not in link.split("?")[0] and " " not in link and "\n" not in link
    assert link_for("+14155550100", "x") == "https://wa.me/14155550100?text=x"


# ---- preview: who would get a message, with no numbers ----
def test_the_preview_lists_ready_blocked_and_unreviewed_and_shows_no_number(
    two: tuple[Fpo, Fpo], market: dict[str, Any]
) -> None:
    a, _ = two
    plan, mids, _docs = three_farmers(a, market)
    r = preview(a.admin, plan["id"])
    assert r.status_code == 200
    pv = r.json()
    valid("handover", "preview", pv)
    assert pv["recipient_count"] == 2 and pv["unreviewed_count"] == 2 and pv["label"] == LABEL
    assert [(x["member_id"], x["language"], x["review_status"]) for x in pv["ready"]] == [
        (mids[0], "pa", "UNREVIEWED"),
        (mids[1], "hi", "UNREVIEWED"),
    ] or sorted((x["member_id"], x["language"]) for x in pv["ready"]) == sorted([(mids[0], "pa"), (mids[1], "hi")])
    assert [(x["member_id"], x["issues"]) for x in pv["blocked"]] == [(mids[2], ["NO_CONTACT", "NO_WHATSAPP_CONSENT"])]
    assert pv["already_reported_sent"] == []
    assert not re.search(r"9000000\d{3}", r.text)  # a preview never contains a number
    early = call(a.admin, "GET", f"/plans/{plan['id']}/handover-preview?kind=CANCELLATION")
    assert early.status_code == 409 and early.json()["issues"][0]["code"] == "NOT_RELEASED"
    assert call(a.admin, "GET", f"/plans/{plan['id']}/handover-preview?kind=NOPE").status_code == 422


def test_nothing_can_be_handed_over_before_approval(two: tuple[Fpo, Fpo], market: dict[str, Any]) -> None:
    a, _ = two
    plan, _lots = make_plan(a, market, ("1250.000",))  # proposed, not approved
    r = preview(a.admin, plan["id"])
    assert r.status_code == 409 and r.json()["issues"][0]["code"] == "NOT_APPROVED"
    assert handover(a.admin, plan["id"], 1).status_code == 409


# ---- the yes ----
def test_the_yes_makes_one_exact_link_per_eligible_farmer_and_logs_each_number_shown(
    two: tuple[Fpo, Fpo], market: dict[str, Any], engine: Engine
) -> None:
    a, _ = two
    plan, mids, docs = three_farmers(a, market)
    stale = handover(a.admin, plan["id"], 3)  # the manager was shown 3, the system finds 2
    assert stale.status_code == 409 and stale.json()["issues"][0]["code"] == "CONFIRMATION_STALE"
    assert call(a.admin, "GET", f"/plans/{plan['id']}/handovers").json() == []
    noack = handover(a.admin, plan["id"], 2, ack=False)
    assert noack.status_code == 422 and noack.json()["issues"][0]["code"] == "UNREVIEWED_NOT_ACKNOWLEDGED"
    assert call(a.admin, "GET", f"/plans/{plan['id']}/handovers").json() == []
    assert access_rows(engine, a) == []
    r = handover(a.admin, plan["id"], 2)
    assert r.status_code == 201, r.text
    h = r.json()
    valid("handover", "handover", h)
    assert h["kind"] == "SALE_INSTRUCTION" and h["recipient_count"] == 2 and h["acknowledged_unreviewed"] is True
    assert h["created_by"] == a.admin_id and h["label"] == LABEL
    items = {i["member_id"]: i for i in h["items"]}
    assert set(items) == set(mids)
    for mid, phone in zip(mids[:2], (PHONE_A, PHONE_B), strict=True):
        i = items[mid]
        valid("handover", "item", i)
        assert i["status"] == "ISSUED" and i["state"] == "NOT_REPORTED" and i["link_withheld"] == []
        digits, decoded = parse(i["link"])
        assert digits == DIGITS[phone]
        assert decoded == docs[mid]["text"]  # byte for byte the stored instruction
        assert i["language"] == docs[mid]["language"] and i["review_status"] == "UNREVIEWED"
    assert "ਸਤਿ ਸ੍ਰੀ ਅਕਾਲ" in parse(items[mids[0]]["link"])[1] and "नमस्ते" in parse(items[mids[1]]["link"])[1]
    third = items[mids[2]]
    assert (third["status"], third["link"], third["issues"]) == ("BLOCKED", None, ["NO_CONTACT", "NO_WHATSAPP_CONSENT"])
    assert sorted(x.member_id for x in access_rows(engine, a)) == sorted(mids[:2])  # one row per number shown
    # viewing again shows the links again and logs again; the plan's list never shows a link
    again = call(a.admin, "GET", f"/handovers/{h['id']}").json()
    valid("handover", "handover", again)
    assert {i["member_id"]: i["link"] for i in again["items"]} == {m: items[m]["link"] for m in mids}
    assert len(access_rows(engine, a)) == 4
    listing = call(a.admin, "GET", f"/plans/{plan['id']}/handovers")
    assert (
        [x["id"] for x in listing.json()] == [h["id"]]
        and "wa.me" not in listing.text
        and len(access_rows(engine, a)) == 4
    )


def test_a_second_press_with_the_same_key_returns_the_same_handover_and_a_different_body_is_refused(
    two: tuple[Fpo, Fpo], market: dict[str, Any]
) -> None:
    a, _ = two
    plan, _mids, _docs = three_farmers(a, market)
    first = handover(a.admin, plan["id"], 2, key="press-key-0001")
    again = handover(a.admin, plan["id"], 2, key="press-key-0001")
    assert first.status_code == again.status_code == 201 and first.json()["id"] == again.json()["id"]
    assert len(call(a.admin, "GET", f"/plans/{plan['id']}/handovers").json()) == 1
    other = handover(a.admin, plan["id"], 2, ack=True, key="press-key-0001", kind="SALE_INSTRUCTION")
    assert other.status_code == 201  # identical body: still the stored one
    clash = call(
        a.admin,
        "POST",
        f"/plans/{plan['id']}/handovers",
        {"kind": "SALE_INSTRUCTION", "recipient_count": 1, "acknowledge_unreviewed": True},
        "press-key-0001",
    )
    assert clash.status_code == 409 and clash.json()["code"] == "IDEMPOTENCY_KEY_REUSED"


# ---- what the manager reports ----
def test_a_report_is_a_statement_by_the_manager_never_a_delivery(
    two: tuple[Fpo, Fpo], market: dict[str, Any], engine: Engine
) -> None:
    a, _ = two
    plan, mids, docs = three_farmers(a, market)
    h = handover(a.admin, plan["id"], 2).json()
    i0, _i1, i2 = (docs[m]["id"] for m in mids)
    r = report(a.admin, h["id"], i0, "SENT")
    assert r.status_code == 201
    item = r.json()
    valid("handover", "item", item)
    assert item["state"] == "MANAGER_REPORTED_SENT" and item["reported_by"] == a.admin_id and item["link"] is None
    assert report(a.admin, h["id"], i0, "SENT").json()["issues"][0]["code"] == "NO_CHANGE"
    after = {i["member_id"]: i for i in call(a.admin, "GET", f"/handovers/{h['id']}").json()["items"]}
    assert after[mids[0]]["link"] is None and after[mids[1]]["link"] is not None  # no link once reported as sent
    assert report(a.admin, h["id"], i0, "NOT_SENT").json()["state"] == "MANAGER_REPORTED_NOT_SENT"  # a correction
    back = {i["member_id"]: i for i in call(a.admin, "GET", f"/handovers/{h['id']}").json()["items"]}
    assert back[mids[0]]["link"] is not None  # not sent: the link is still due
    assert report(a.admin, h["id"], i0, "SENT").json()["state"] == "MANAGER_REPORTED_SENT"
    rows = as_app(engine, a.fpo_id, "SELECT outcome FROM handover_reports ORDER BY id")
    assert [r.outcome for r in rows] == ["SENT", "NOT_SENT", "SENT"]  # three events, none rewritten
    blocked = report(a.admin, h["id"], i2, "SENT")
    assert blocked.status_code == 409 and blocked.json()["issues"][0]["code"] == "ITEM_NOT_ISSUED"
    assert report(a.admin, h["id"], f"ins_{'0' * 26}", "SENT").status_code == 404
    assert report(a.admin, f"hnd_{'0' * 26}", i0, "SENT").status_code == 404
    assert (
        call(a.admin, "POST", f"/handovers/{h['id']}/items/{i0}/outcome", {"outcome": "DELIVERED"}).status_code == 422
    )
    # nothing of messaging/v1 moved: no delivery record exists, the instruction still says NOT_SENT
    assert call(a.admin, "GET", f"/instructions/{i0}/attempts").json() == []
    assert call(a.admin, "GET", f"/instructions/{i0}").json()["delivery"]["state"] == "NOT_SENT"
    assert call(a.admin, "GET", f"/plans/{plan['id']}/state").json()["status"] == "APPROVED"


def test_a_second_handover_includes_only_farmers_not_yet_reported_as_sent(
    two: tuple[Fpo, Fpo], market: dict[str, Any]
) -> None:
    a, _ = two
    plan, mids, docs = three_farmers(a, market)
    h1 = handover(a.admin, plan["id"], 2).json()
    assert report(a.admin, h1["id"], docs[mids[0]]["id"], "SENT").status_code == 201
    assert report(a.admin, h1["id"], docs[mids[1]]["id"], "NOT_SENT").status_code == 201
    ready(a, mids[2], PHONE_C)  # the third farmer is fixed
    fixed = by_member(prepare(a.admin, plan["id"]).json())[mids[2]]
    assert fixed["status"] == "PREPARED" and fixed["version"] == 2
    pv = preview(a.admin, plan["id"]).json()
    assert sorted(x["member_id"] for x in pv["ready"]) == sorted(mids[1:]) and pv["already_reported_sent"] == [mids[0]]
    assert pv["blocked"] == []
    h2 = handover(a.admin, plan["id"], 2).json()
    assert sorted(i["member_id"] for i in h2["items"]) == sorted(mids[1:])  # farmer 1 is not sent a second time
    assert parse(next(i for i in h2["items"] if i["member_id"] == mids[2])["link"])[0] == DIGITS[PHONE_C]
    assert len(call(a.admin, "GET", f"/plans/{plan['id']}/handovers").json()) == 2
    for m in mids[1:]:
        report(a.admin, h2["id"], next(i for i in h2["items"] if i["member_id"] == m)["instruction_id"], "SENT")
    done = handover(a.admin, plan["id"], 1)
    assert done.status_code == 409 and done.json()["issues"][0]["code"] == "NOTHING_TO_SEND"


# ---- checks repeated after the yes ----
def test_a_link_is_withheld_when_the_contact_changed_the_consent_was_withdrawn_or_the_plan_was_released(
    two: tuple[Fpo, Fpo], market: dict[str, Any]
) -> None:
    a, _ = two
    plan, mids, _docs = three_farmers(a, market)
    h = handover(a.admin, plan["id"], 2).json()

    def state() -> dict[str, dict[str, Any]]:
        return {i["member_id"]: i for i in call(a.admin, "GET", f"/handovers/{h['id']}").json()["items"]}

    assert contact(a.admin, mids[0], "9000000009").status_code == 201  # a new number: the old link goes elsewhere
    s = state()
    assert s[mids[0]]["link"] is None and s[mids[0]]["link_withheld"] == ["CONTACT_CHANGED"]
    assert s[mids[1]]["link"] is not None
    assert consent(a.admin, mids[1], "WITHDRAWN").status_code == 201
    s = state()
    assert s[mids[1]]["link"] is None and s[mids[1]]["link_withheld"] == ["CONSENT_WITHDRAWN"]
    assert call(a.admin, "POST", f"/plans/{plan['id']}/release", {"reason_code": "MARKET_CHANGED"}).status_code == 201
    s = state()
    assert s[mids[0]]["link_withheld"] == ["COMMITMENT_NOT_ACTIVE", "CONTACT_CHANGED"]
    assert s[mids[1]]["link_withheld"] == ["CONSENT_WITHDRAWN", "COMMITMENT_NOT_ACTIVE"]
    for i in s.values():
        valid("handover", "item", i)
    # and a new press now finds nothing to send for the sale instruction
    assert preview(a.admin, plan["id"]).status_code == 409


def test_consent_withdrawn_before_the_yes_keeps_that_farmer_out(two: tuple[Fpo, Fpo], market: dict[str, Any]) -> None:
    a, _ = two
    plan, mids, _docs = three_farmers(a, market)
    assert consent(a.admin, mids[1], "WITHDRAWN").status_code == 201
    pv = preview(a.admin, plan["id"]).json()
    assert [x["member_id"] for x in pv["ready"]] == [mids[0]]
    assert (mids[1], ["CONSENT_WITHDRAWN"]) in [(x["member_id"], x["issues"]) for x in pv["blocked"]]
    assert handover(a.admin, plan["id"], 2).json()["issues"][0]["code"] == "CONFIRMATION_STALE"
    h = handover(a.admin, plan["id"], 1).json()
    assert [i["status"] for i in sorted(h["items"], key=lambda i: i["member_id"])].count("ISSUED") == 1


# ---- cancellation ----
def test_a_cancellation_reaches_farmers_the_manager_reported_as_sent(
    two: tuple[Fpo, Fpo], market: dict[str, Any]
) -> None:
    a, _ = two
    plan, mids, docs = three_farmers(a, market)
    h = handover(a.admin, plan["id"], 2).json()
    assert report(a.admin, h["id"], docs[mids[0]]["id"], "SENT").status_code == 201
    assert report(a.admin, h["id"], docs[mids[1]]["id"], "NOT_SENT").status_code == 201  # never sent: nothing to cancel
    assert call(a.admin, "POST", f"/plans/{plan['id']}/release", {"reason_code": "PLAN_CANCELLED"}).status_code == 201
    cancel = by_member(prepare(a.admin, plan["id"], "CANCELLATION").json())
    assert list(cancel) == [mids[0]] and cancel[mids[0]]["supersedes"] == docs[mids[0]]["id"]
    pv = preview(a.admin, plan["id"], "CANCELLATION").json()
    assert [x["member_id"] for x in pv["ready"]] == [mids[0]] and pv["already_reported_sent"] == []
    c = handover(a.admin, plan["id"], 1, kind="CANCELLATION").json()
    valid("handover", "handover", c)
    link = c["items"][0]["link"]
    assert parse(link) == (DIGITS[PHONE_A], cancel[mids[0]]["text"])
    assert "रद्द" in cancel[mids[0]]["text"] or "ਰੱਦ" in cancel[mids[0]]["text"]


# ---- who may ----
def test_only_a_holder_of_both_capabilities_gets_links_and_a_viewer_gets_none(
    two: tuple[Fpo, Fpo], market: dict[str, Any], engine: Engine
) -> None:
    a, _ = two
    plan, mids, docs = three_farmers(a, market)
    _, _, sender = add_user(a, "STAFF", ["messages.send"])  # may send, may not see numbers
    _, _, viewer = add_user(a, "VIEWER")
    assert preview(sender, plan["id"]).status_code == 200
    denied = handover(sender, plan["id"], 2)
    assert denied.status_code == 403 and not re.search(r"9000000\d{3}", denied.text)
    assert access_rows(engine, a) == []
    assert preview(viewer, plan["id"]).status_code == 403 and handover(viewer, plan["id"], 2).status_code == 403
    h = handover(a.admin, plan["id"], 2).json()
    for s in (sender, viewer):
        r = call(s, "GET", f"/handovers/{h['id']}")
        assert r.status_code == 200
        assert all(i["link"] is None for i in r.json()["items"]) and "wa.me" not in r.text
        assert not re.search(r"9000000\d{3}", r.text)
    assert len(access_rows(engine, a)) == 2  # only the admin's own response showed links
    assert report(viewer, h["id"], docs[mids[0]]["id"], "SENT").status_code == 403
    assert report(sender, h["id"], docs[mids[0]]["id"], "SENT").status_code == 201  # messages.send is enough to report


# ---- tenant scope ----
def test_another_fpo_sees_and_touches_nothing(two: tuple[Fpo, Fpo], market: dict[str, Any]) -> None:
    a, b = two
    plan, mids, docs = three_farmers(a, market)
    h = handover(a.admin, plan["id"], 2).json()
    i0 = docs[mids[0]]["id"]
    for method, path, body in (
        ("GET", f"/plans/{plan['id']}/handover-preview?kind=SALE_INSTRUCTION", None),
        (
            "POST",
            f"/plans/{plan['id']}/handovers",
            {"kind": "SALE_INSTRUCTION", "recipient_count": 2, "acknowledge_unreviewed": True},
        ),
        ("GET", f"/plans/{plan['id']}/handovers", None),
        ("GET", f"/handovers/{h['id']}", None),
        ("POST", f"/handovers/{h['id']}/items/{i0}/outcome", {"outcome": "SENT"}),
    ):
        r = call(b.admin, method, path, body)
        missing = call(
            b.admin, method, path.replace(h["id"], f"hnd_{'0' * 26}").replace(plan["id"], f"pln_{'0' * 26}"), body
        )
        assert r.status_code == missing.status_code == 404
        assert r.json()["code"] == missing.json()["code"] == "NOT_FOUND"
        assert "wa.me" not in r.text
    assert report(a.admin, h["id"], i0, "SENT").status_code == 201  # ours is untouched by the attempts above
    assert call(a.admin, "GET", f"/plans/{plan['id']}/handovers").json()[0]["items"] is not None


# ---- records ----
def test_handover_records_are_append_only_for_everyone(
    two: tuple[Fpo, Fpo], market: dict[str, Any], engine: Engine
) -> None:
    a, _ = two
    plan, mids, docs = three_farmers(a, market)
    h = handover(a.admin, plan["id"], 2).json()
    report(a.admin, h["id"], docs[mids[0]]["id"], "SENT")
    for t in ("handovers", "handover_items", "handover_reports"):
        for sql in (f"UPDATE {t} SET fpo_id = fpo_id", f"DELETE FROM {t}"):
            with pytest.raises(DBAPIError):
                as_app(engine, a.fpo_id, sql)
            with pytest.raises(DBAPIError), engine.begin() as c:
                c.execute(text(sql))


def test_no_link_and_no_number_is_stored_or_logged(
    two: tuple[Fpo, Fpo], market: dict[str, Any], engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    a, _ = two
    plan, mids, docs = three_farmers(a, market)
    h = handover(a.admin, plan["id"], 2).json()
    call(a.admin, "GET", f"/handovers/{h['id']}")
    report(a.admin, h["id"], docs[mids[0]]["id"], "SENT")
    for needle in ("9000000001", "9000000002", "wa.me", "%E0%A8"):
        assert needle not in caplog.text, needle
    with engine.connect() as c:
        for table, col in (
            ("handovers", "request_id"),
            ("handover_items", "issues::text || text_sha256"),
            ("handover_reports", "request_id"),
            ("audit_log", "event"),
        ):
            n = c.execute(
                text(f"SELECT count(*) FROM {table} WHERE {col} LIKE '%9000000001%' OR {col} LIKE '%wa.me%'")
            ).scalar_one()
            assert n == 0, (table, col)


def test_unauthenticated_requests_are_refused(two: tuple[Fpo, Fpo]) -> None:
    from fastapi.testclient import TestClient

    from kisaniti_api.main import app

    c = TestClient(app)
    pid, hid = f"pln_{'0' * 26}", f"hnd_{'0' * 26}"
    assert c.get(f"/api/v1/plans/{pid}/handover-preview?kind=SALE_INSTRUCTION").status_code == 401
    assert c.get(f"/api/v1/plans/{pid}/handovers").status_code == 401
    assert c.get(f"/api/v1/handovers/{hid}").status_code == 401
    body = {"kind": "SALE_INSTRUCTION", "recipient_count": 1, "acknowledge_unreviewed": True}
    assert c.post(f"/api/v1/plans/{pid}/handovers", json=body).status_code == 401
    assert c.post(f"/api/v1/handovers/{hid}/items/ins_{'0' * 26}/outcome", json={"outcome": "SENT"}).status_code == 401

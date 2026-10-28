# ruff: noqa: F811, F401  (fixtures imported from test_access by name: pytest uses them as arguments)
"""Messaging slice (contract messaging/v1) on real PostgreSQL: contacts, consent, templates, instructions, the
preview channel and delivery records. Fictional people and numbers only. Expected message texts are written out by
hand here (with their own month names), not produced by the rendering code."""

from __future__ import annotations

import logging
import re
from typing import Any

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

from kisaniti_api.messaging import channels
from kisaniti_api.messaging.templates import all_templates, latest
from tests.test_access import Fpo, Session, access_env, add_user, as_app, audit_events, clean_up_access_data, two
from tests.test_approval import acks, approve, make_plan
from tests.test_org import call
from tests.test_plan import fresh_market, market, valid

PHONE_A, PHONE_B, PHONE_C = "9000000001", "9000000002", "9000000003"
FULL = {PHONE_A: "+919000000001", PHONE_B: "+919000000002", PHONE_C: "+919000000003"}
NOTICE = "NOTICE-DEMO-0.1"
MONTHS = {
    "en": {10: "October", 11: "November"},
    "pa": {10: "ਅਕਤੂਬਰ", 11: "ਨਵੰਬਰ"},
    "hi": {10: "अक्टूबर", 11: "नवंबर"},
}
OPENING = {
    "en": "Namaste {n}. Your FPO manager has approved this plan for your 1121 basmati paddy:\n",
    "pa": "ਸਤਿ ਸ੍ਰੀ ਅਕਾਲ {n} ਜੀ। ਤੁਹਾਡੇ FPO ਮੈਨੇਜਰ ਨੇ ਤੁਹਾਡੇ 1121 ਬਾਸਮਤੀ ਝੋਨੇ ਲਈ ਇਹ ਯੋਜਨਾ ਮਨਜ਼ੂਰ ਕੀਤੀ ਹੈ:\n",
    "hi": "नमस्ते {n} जी। आपके FPO मैनेजर ने आपके 1121 बासमती धान के लिए यह योजना मंज़ूर की है:\n",
}
LINE = {
    "en": "• {q} kg at {m} on {d}\n",
    "pa": "• {q} ਕਿਲੋ, {m} ਵਿੱਚ, {d} ਨੂੰ\n",
    "hi": "• {q} किलो, {m} में, {d} को\n",
}
CLOSING = {
    "en": "If anything looks wrong, please contact your FPO office.",
    "pa": "ਜੇ ਕੁਝ ਗ਼ਲਤ ਲੱਗੇ ਤਾਂ ਕਿਰਪਾ ਕਰਕੇ ਆਪਣੇ FPO ਦਫ਼ਤਰ ਨਾਲ ਸੰਪਰਕ ਕਰੋ।",
    "hi": "अगर कुछ गलत लगे तो कृपया अपने FPO कार्यालय से संपर्क करें।",
}


def expected_text(lang: str, name: str, lines: list[tuple[str, str, str]]) -> str:
    """lines: (kg, mandi, ISO date)."""
    body = OPENING[lang].format(n=name)
    for kg, mandi, iso in lines:
        y, m, d = iso.split("-")
        body += LINE[lang].format(q=kg, m=mandi, d=f"{int(d)} {MONTHS[lang][int(m)]} {y}")
    return body + CLOSING[lang]


def set_language(a: Fpo, member_id: str, lang: str) -> None:
    r = call(a.admin, "PATCH", f"/members/{member_id}", {"preferred_language": lang})
    assert r.status_code == 200, r.text


def contact(s: Session, member_id: str, phone: str) -> Any:
    return call(s, "POST", f"/members/{member_id}/contact", {"phone": phone})


def consent(s: Session, member_id: str, action: str = "GRANTED", kind: str = "WHATSAPP") -> Any:
    return call(
        s,
        "POST",
        f"/members/{member_id}/consents",
        {"kind": kind, "action": action, "notice_version": NOTICE, "method": "STAFF_ASSISTED_ORAL"},
    )


def ready(a: Fpo, member_id: str, phone: str) -> None:
    assert contact(a.admin, member_id, phone).status_code == 201
    assert consent(a.admin, member_id).status_code == 201


def approved_plan(
    a: Fpo, market: dict[str, Any], kgs: tuple[str, ...] = ("1250.000", "800.000", "500.000")
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    plan, lots = make_plan(a, market, kgs)
    assert approve(a.admin, plan["id"]).status_code == 201
    return plan, lots


def prepare(s: Session, plan_id: str, kind: str = "SALE_INSTRUCTION") -> Any:
    return call(s, "POST", f"/plans/{plan_id}/instructions", {"kind": kind})


def by_member(docs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {d["member_id"]: d for d in docs}


def send(s: Session, ins_id: str, key: str | None = None, channel: str = "PREVIEW") -> Any:
    return call(s, "POST", f"/instructions/{ins_id}/send", {"channel": channel}, key)


# ---- contacts: masking, versions, the access log ----
def test_a_number_is_normalised_masked_for_most_roles_and_every_full_view_is_logged(
    two: tuple[Fpo, Fpo], market: dict[str, Any], engine: Engine
) -> None:
    a, _ = two
    _, lots = make_plan(a, market, ("1000.000",))
    mid = lots[0]["member_id"]
    _, _, staff = add_user(a, "STAFF")
    r = contact(staff, mid, "90000 00001")  # staff may record but may not see numbers
    assert r.status_code == 201
    doc = r.json()
    valid("messaging", "contact", doc)
    assert doc["phone"] == "+91•••••••001" and doc["phone_masked"] is True and doc["version"] == 1
    assert (
        as_app(engine, a.fpo_id, "SELECT count(*) FROM personal_data_access_log")[0][0] == 0
    )  # nothing unmasked was shown
    listed = call(staff, "GET", "/contacts").json()
    row = next(x for x in listed if x["member_id"] == mid)
    valid("messaging", "contact_summary", row)
    assert row["contact"]["phone"] == "+91•••••••001"
    assert FULL[PHONE_A] not in staff.client.get("/api/v1/contacts").text
    full = call(a.admin, "GET", "/contacts").json()
    assert next(x for x in full if x["member_id"] == mid)["contact"]["phone"] == FULL[PHONE_A]
    log = as_app(engine, a.fpo_id, "SELECT actor_user_id, member_id, what FROM personal_data_access_log")
    assert [(r.actor_user_id, r.member_id, r.what) for r in log] == [(a.admin_id, mid, "PHONE_VIEWED")]
    # a second number is a new version; the old one stays in the history
    assert contact(a.admin, mid, "+91 90000-00009").json()["version"] == 2
    hist = call(a.admin, "GET", f"/members/{mid}/contact/history").json()
    assert [(h["version"], h["phone"]) for h in hist] == [(2, "+919000000009"), (1, FULL[PHONE_A])]
    assert call(staff, "GET", f"/members/{mid}/contact/history").status_code == 403
    assert "PHONE_HISTORY_VIEWED" in [
        r.what for r in as_app(engine, a.fpo_id, "SELECT what FROM personal_data_access_log")
    ]


def test_a_bad_or_unchanged_number_and_any_extra_field_are_refused(
    two: tuple[Fpo, Fpo], market: dict[str, Any]
) -> None:
    a, _ = two
    _, lots = make_plan(a, market, ("1000.000",))
    mid = lots[0]["member_id"]
    for bad in ("12345", "abcdefghij", "5000000001", "+0123456789"):
        r = contact(a.admin, mid, bad)
        assert r.status_code == 422 and r.json()["issues"][0]["code"] == "PHONE_INVALID"
        assert bad not in r.text  # the refusal never echoes what was typed
    assert contact(a.admin, mid, PHONE_A).status_code == 201
    again = contact(a.admin, mid, "+91 90000 00001")
    assert again.status_code == 409 and again.json()["issues"][0]["code"] == "NO_CHANGE"
    for extra in ("aadhaar", "bank_account", "caste", "religion", "land_record", "income", "member_id"):
        assert call(a.admin, "POST", f"/members/{mid}/contact", {"phone": PHONE_B, extra: "1"}).status_code == 422
    _, _, viewer = add_user(a, "VIEWER")
    assert contact(viewer, mid, PHONE_B).status_code == 403
    assert len(call(a.admin, "GET", f"/members/{mid}/contact/history").json()) == 1


def test_no_column_for_the_data_the_privacy_contract_forbids(engine: Engine) -> None:
    with engine.connect() as c:
        cols = [
            r[0]
            for r in c.execute(
                text(
                    "SELECT table_name || '.' || column_name FROM information_schema.columns "
                    "WHERE table_schema = 'public'"
                )
            )
        ]
    assert not [
        c
        for c in cols
        if re.search(r"aadhaar|aadhar|bank|account_no|caste|religion|income|land_record|khasra", c, re.I)
    ]


# ---- consent ----
def test_consent_is_per_kind_append_only_and_can_be_withdrawn_and_given_again(
    two: tuple[Fpo, Fpo], market: dict[str, Any], engine: Engine
) -> None:
    a, _ = two
    _, lots = make_plan(a, market, ("1000.000",))
    mid = lots[0]["member_id"]
    st = call(a.admin, "GET", f"/members/{mid}/consents").json()
    valid("messaging", "consent_state", st)
    assert [(c["kind"], c["state"]) for c in st["current"]] == [
        ("ADVICE", "NONE"),
        ("WHATSAPP", "NONE"),
        ("RESEARCH", "NONE"),
    ] and st["history"] == []
    assert consent(a.admin, mid, "WITHDRAWN").status_code == 409  # nothing to withdraw
    ev1 = consent(a.admin, mid, "GRANTED")
    assert ev1.status_code == 201
    valid("messaging", "consent_event", ev1.json())
    assert ev1.json()["event_no"] == 1 and consent(a.admin, mid, "GRANTED").json()["issues"][0]["code"] == "NO_CHANGE"
    assert (
        consent(a.admin, mid, "GRANTED", "ADVICE").json()["event_no"] == 2
    )  # kinds are independent, numbering is per member
    assert consent(a.admin, mid, "WITHDRAWN").json()["event_no"] == 3
    assert consent(a.admin, mid, "GRANTED").json()["event_no"] == 4
    st = call(a.admin, "GET", f"/members/{mid}/consents").json()
    valid("messaging", "consent_state", st)
    assert {c["kind"]: c["state"] for c in st["current"]} == {
        "ADVICE": "GRANTED",
        "WHATSAPP": "GRANTED",
        "RESEARCH": "NONE",
    }
    assert [(h["event_no"], h["kind"], h["action"]) for h in st["history"]] == [
        (1, "WHATSAPP", "GRANTED"),
        (2, "ADVICE", "GRANTED"),
        (3, "WHATSAPP", "WITHDRAWN"),
        (4, "WHATSAPP", "GRANTED"),
    ]
    bad = call(
        a.admin,
        "POST",
        f"/members/{mid}/consents",
        {"kind": "MARKETING", "action": "GRANTED", "notice_version": NOTICE, "method": "PAPER_FORM"},
    )
    assert bad.status_code == 422
    _, _, staff = add_user(a, "STAFF")
    _, _, viewer = add_user(a, "VIEWER")
    assert consent(staff, mid, "WITHDRAWN").status_code == 201  # staff assist the farmer and record it
    assert consent(viewer, mid, "GRANTED").status_code == 403
    with pytest.raises(DBAPIError):
        as_app(engine, a.fpo_id, "UPDATE member_consents SET action = 'GRANTED'")


def test_templates_are_versioned_have_three_languages_and_are_all_reviewed(two: tuple[Fpo, Fpo]) -> None:
    a, _ = two
    docs = call(a.admin, "GET", "/message-templates").json()
    assert sorted(d["id"] for d in docs) == ["CANCELLATION", "SALE_INSTRUCTION"]
    for d in docs:
        valid("messaging", "template_info", d)
        assert [x["language"] for x in d["languages"]] == ["en", "hi", "pa"]
        for x in d["languages"]:  # read by independent reviewers, recorded on 28 Oct 2026 (no names are stored)
            assert x["review_status"] == "REVIEWED" and x["reviewer"].startswith("independent reviewers")
            assert x["reviewed_at"] == "2026-10-28"
    for t in all_templates():  # the variants say the same thing: same placeholders in every language
        assert len({tuple(sorted(re.findall(r"\{(\w+)\}", v.body + v.line))) for v in t.variants.values()}) == 1
    assert latest("SALE_INSTRUCTION") is not None and latest("NOPE") is None


# ---- preparing instructions ----
def test_nothing_is_prepared_before_approval_and_blockers_are_listed_when_contact_and_consent_are_missing(
    two: tuple[Fpo, Fpo], market: dict[str, Any]
) -> None:
    a, _ = two
    plan, _ = make_plan(a, market, ("1250.000", "800.000"))
    early = prepare(a.admin, plan["id"])
    assert early.status_code == 409 and early.json()["issues"][0]["code"] == "NOT_APPROVED"
    assert approve(a.admin, plan["id"]).status_code == 201
    docs = prepare(a.admin, plan["id"])
    assert docs.status_code == 201
    got = docs.json()
    assert len(got) == 2
    for d in got:
        valid("messaging", "instruction", d)
        assert d["status"] == "BLOCKED" and d["text"] is None and d["language"] is None and d["template"] is None
        assert d["issues"] == ["NO_CONTACT", "NO_WHATSAPP_CONSENT"] and d["delivery"]["state"] == "NOT_SENT"
    q = call(a.admin, "GET", "/instruction-queue").json()
    assert sorted(x["id"] for x in q) == sorted(d["id"] for d in got)
    r = send(a.admin, got[0]["id"])
    assert r.status_code == 409 and r.json()["issues"][0]["code"] == "INSTRUCTION_BLOCKED"


def test_three_farmers_each_get_the_exact_text_in_their_own_language(
    two: tuple[Fpo, Fpo], market: dict[str, Any]
) -> None:
    a, _ = two
    plan, lots = approved_plan(a, market)
    langs = {lots[0]["member_id"]: "pa", lots[1]["member_id"]: "hi", lots[2]["member_id"]: "en"}
    phones = [PHONE_A, PHONE_B, PHONE_C]
    for (mid, lang), phone in zip(langs.items(), phones, strict=True):
        set_language(a, mid, lang)
        ready(a, mid, phone)
    assert call(a.admin, "PUT", "/auth/language", {"language": "en"}).status_code == 200  # the manager reads English
    docs = by_member(prepare(a.admin, plan["id"]).json())
    d1 = plan["allocation_input"]["dates"][0]
    alpha = "Alpha APMC"
    want = {
        lots[0]["member_id"]: ("1200.000", "Farmer0 (fictional)"),
        lots[1]["member_id"]: ("800.000", "Farmer1 (fictional)"),
        lots[2]["member_id"]: ("500.000", "Farmer2 (fictional)"),
    }
    for mid, lang in langs.items():
        d = docs[mid]
        valid("messaging", "instruction", d)
        kg, name = want[mid]
        assert d["status"] == "PREPARED" and d["issues"] == [] and d["language"] == lang
        assert d["text"] == expected_text(
            lang, name, [(kg, alpha, d1)]
        )  # WA01, WA02, WA05: facts, amounts and dates exact
        assert d["template"] == {
            "id": "SALE_INSTRUCTION",
            "version": 1,
            "review_status": "REVIEWED",
            "text_sha256": d["template"]["text_sha256"],
        }
        assert d["facts"]["lines"][0]["quantity_kg"] == kg and d["facts"]["variety"] == "1121"
        assert d["contact_version"] == 1 and d["consent_event_no"] == 1 and d["revision_no"] == 1 and d["version"] == 1
        assert "price" not in d["text"].lower() and "₹" not in d["text"]  # no price, no promise
    assert len({d["template"]["text_sha256"] for d in docs.values()}) == 3  # each language has its own fingerprint


def test_preparing_again_creates_no_duplicates_and_a_blocked_one_is_replaced_once_fixed(
    two: tuple[Fpo, Fpo], market: dict[str, Any], engine: Engine
) -> None:
    a, _ = two
    plan, lots = approved_plan(a, market, ("1000.000", "800.000"))
    ready(a, lots[0]["member_id"], PHONE_A)
    first = by_member(prepare(a.admin, plan["id"]).json())
    again = by_member(prepare(a.admin, plan["id"]).json())
    assert first == again
    assert as_app(engine, a.fpo_id, "SELECT count(*) FROM instructions")[0][0] == 2
    blocked = first[lots[1]["member_id"]]
    assert blocked["status"] == "BLOCKED" and first[lots[0]["member_id"]]["status"] == "PREPARED"
    ready(a, lots[1]["member_id"], PHONE_B)
    fixed = by_member(prepare(a.admin, plan["id"]).json())[lots[1]["member_id"]]
    assert fixed["status"] == "PREPARED" and fixed["version"] == 2 and fixed["supersedes"] == blocked["id"]
    assert as_app(engine, a.fpo_id, "SELECT count(*) FROM instructions")[0][0] == 3
    assert call(a.admin, "GET", "/instruction-queue").json() == []  # the blocked v1 is no longer the latest
    listed = call(a.admin, "GET", f"/plans/{plan['id']}/instructions").json()
    assert sorted(d["version"] for d in listed) == [1, 2]


def test_a_withdrawn_consent_and_an_inactive_member_block_at_preparation(
    two: tuple[Fpo, Fpo], market: dict[str, Any]
) -> None:
    a, _ = two
    plan, lots = approved_plan(a, market, ("1000.000", "800.000", "500.000"))
    m0, m1, m2 = (lot["member_id"] for lot in lots)
    ready(a, m0, PHONE_A)
    consent(a.admin, m0, "WITHDRAWN")
    ready(a, m1, PHONE_B)
    assert call(a.admin, "PATCH", f"/members/{m1}", {"active": False}).status_code == 200
    got = by_member(prepare(a.admin, plan["id"]).json())
    assert (
        got[m0]["issues"] == ["CONSENT_WITHDRAWN"]
        and got[m1]["issues"] == ["MEMBER_INACTIVE"]
        and got[m2]["issues"] == ["NO_CONTACT", "NO_WHATSAPP_CONSENT"]
    )


# ---- the preview channel ----
def test_a_preview_shows_the_exact_text_masks_the_number_and_is_never_a_delivery(
    two: tuple[Fpo, Fpo], market: dict[str, Any], engine: Engine
) -> None:
    a, _ = two
    plan, lots = approved_plan(a, market, ("1250.000",))
    mid = lots[0]["member_id"]
    ready(a, mid, PHONE_A)
    ins = prepare(a.admin, plan["id"]).json()[0]
    r = send(a.admin, ins["id"], key="preview-key-0001")
    assert r.status_code == 201, r.text
    att = r.json()
    valid("messaging", "delivery_attempt", att)
    assert (
        att["channel"] == "PREVIEW"
        and att["status"] == "PREVIEWED"
        and att["simulated"] is True
        and att["provider_message_id"] is None
    )
    assert att["preview"]["text"] == ins["text"] and att["preview"]["language"] == "pa"
    assert att["preview"]["label"] == "PREVIEW — nothing was sent" and att["preview"]["recipient"] == "+91•••••••001"
    assert FULL[PHONE_A] not in r.text  # even the admin, who may see numbers elsewhere, gets the masked recipient here
    cur = call(a.admin, "GET", f"/instructions/{ins['id']}").json()
    valid("messaging", "instruction", cur)
    assert cur["delivery"]["state"] == "PREVIEWED" and cur["delivery"]["attempts"] == 1 and cur["text"] == ins["text"]
    assert cur["delivery"]["state"] not in ("SENT", "DELIVERED", "READ")
    replay = send(a.admin, ins["id"], key="preview-key-0001")
    assert replay.status_code == 201 and replay.json() == att
    assert as_app(engine, a.fpo_id, "SELECT count(*) FROM delivery_attempts")[0][0] == 1
    other = call(a.admin, "POST", f"/instructions/{ins['id']}/send", {"channel": "PREVIEW"}, "preview-key-0001")
    assert other.status_code == 201  # same body again: the stored answer
    assert send(a.admin, ins["id"]).status_code == 201  # a second preview is harmless and is recorded
    attempts = call(a.admin, "GET", f"/instructions/{ins['id']}/attempts").json()
    assert len(attempts) == 2 and all(x["status"] == "PREVIEWED" for x in attempts)
    for x in attempts:
        valid("messaging", "delivery_attempt", x)
    reuse = call(a.admin, "POST", f"/instructions/{ins['id']}/send", {"channel": "WHATSAPP"}, "preview-key-0001")
    assert reuse.status_code in (409, 422)


def test_the_real_whatsapp_channel_does_not_exist_yet(two: tuple[Fpo, Fpo], market: dict[str, Any]) -> None:
    a, _ = two
    plan, lots = approved_plan(a, market, ("1250.000",))
    ready(a, lots[0]["member_id"], PHONE_A)
    ins = prepare(a.admin, plan["id"]).json()[0]
    r = send(a.admin, ins["id"], channel="WHATSAPP")
    assert r.status_code == 422 and r.json()["issues"][0]["code"] == "CHANNEL_NOT_AVAILABLE"
    assert call(a.admin, "POST", f"/instructions/{ins['id']}/send", {"channel": "SMS"}).status_code == 422
    assert set(channels.CHANNELS) == {"PREVIEW"}
    assert call(a.admin, "GET", f"/instructions/{ins['id']}/attempts").json() == []


# ---- re-checks at send time ----
def test_consent_withdrawn_after_preparation_stops_the_send_and_leaves_the_text_alone(
    two: tuple[Fpo, Fpo], market: dict[str, Any]
) -> None:
    a, _ = two
    plan, lots = approved_plan(a, market, ("1250.000",))
    mid = lots[0]["member_id"]
    ready(a, mid, PHONE_A)
    ins = prepare(a.admin, plan["id"]).json()[0]
    assert consent(a.admin, mid, "WITHDRAWN").status_code == 201
    r = send(a.admin, ins["id"])
    att = r.json()
    assert (
        r.status_code == 201
        and att["status"] == "BLOCKED"
        and att["issues"] == ["CONSENT_WITHDRAWN"]
        and att["preview"] is None
    )
    valid("messaging", "delivery_attempt", att)
    q = call(a.admin, "GET", "/instruction-queue").json()
    assert [x["id"] for x in q] == [ins["id"]] and q[0]["delivery"]["state"] == "BLOCKED"
    assert call(a.admin, "GET", f"/instructions/{ins['id']}").json()["text"] == ins["text"]  # past content unchanged
    assert consent(a.admin, mid, "GRANTED").status_code == 201
    ok = send(a.admin, ins["id"]).json()
    assert ok["status"] == "PREVIEWED"
    assert call(a.admin, "GET", "/instruction-queue").json() == []  # a good attempt clears the queue


def test_a_changed_language_blocks_the_send_and_a_new_version_is_written_in_the_new_language(
    two: tuple[Fpo, Fpo], market: dict[str, Any]
) -> None:
    a, _ = two
    plan, lots = approved_plan(a, market, ("1250.000",))
    mid = lots[0]["member_id"]
    ready(a, mid, PHONE_A)
    old = prepare(a.admin, plan["id"]).json()[0]
    assert old["language"] == "pa"
    set_language(a, mid, "en")
    r = send(a.admin, old["id"]).json()
    assert r["status"] == "BLOCKED" and r["issues"] == ["LANGUAGE_CHANGED"]  # no silent switch to the new language
    new = prepare(a.admin, plan["id"]).json()[0]
    assert new["id"] != old["id"] and new["version"] == 2 and new["supersedes"] == old["id"] and new["language"] == "en"
    assert new["text"].startswith("Namaste Farmer0 (fictional).")
    assert call(a.admin, "GET", f"/instructions/{old['id']}").json()["text"] == old["text"]  # the old one is untouched
    assert send(a.admin, new["id"]).json()["status"] == "PREVIEWED"


def test_a_sale_instruction_cannot_go_out_after_the_reservation_was_released(
    two: tuple[Fpo, Fpo], market: dict[str, Any]
) -> None:
    a, _ = two
    plan, lots = approved_plan(a, market, ("1250.000",))
    ready(a, lots[0]["member_id"], PHONE_A)
    ins = prepare(a.admin, plan["id"]).json()[0]
    assert call(a.admin, "POST", f"/plans/{plan['id']}/release", {"reason_code": "PLAN_CANCELLED"}).status_code == 201
    att = send(a.admin, ins["id"]).json()
    assert att["status"] == "BLOCKED" and att["issues"] == ["COMMITMENT_NOT_ACTIVE"]
    late = prepare(a.admin, plan["id"])
    assert late.status_code == 409 and late.json()["issues"][0]["code"] == "NOT_APPROVED"


def test_an_inactive_member_blocks_the_send(two: tuple[Fpo, Fpo], market: dict[str, Any]) -> None:
    a, _ = two
    plan, lots = approved_plan(a, market, ("1250.000",))
    mid = lots[0]["member_id"]
    ready(a, mid, PHONE_A)
    ins = prepare(a.admin, plan["id"]).json()[0]
    call(a.admin, "PATCH", f"/members/{mid}", {"active": False})
    assert send(a.admin, ins["id"]).json()["issues"] == ["MEMBER_INACTIVE"]


# ---- cancellation ----
def test_a_cancellation_goes_only_to_farmers_who_were_told_and_links_the_earlier_instruction(
    two: tuple[Fpo, Fpo], market: dict[str, Any]
) -> None:
    a, _ = two
    plan, lots = approved_plan(a, market, ("1250.000", "800.000"))
    m0, m1 = lots[0]["member_id"], lots[1]["member_id"]
    set_language(a, m0, "hi")
    set_language(a, m1, "en")
    ready(a, m0, PHONE_A)
    ready(a, m1, PHONE_B)
    sale = by_member(prepare(a.admin, plan["id"]).json())
    early = prepare(a.admin, plan["id"], "CANCELLATION")
    assert early.status_code == 409 and early.json()["issues"][0]["code"] == "NOT_RELEASED"
    assert send(a.admin, sale[m0]["id"]).json()["status"] == "PREVIEWED"  # only the first farmer was previewed
    assert call(a.admin, "POST", f"/plans/{plan['id']}/release", {"reason_code": "MARKET_CHANGED"}).status_code == 201
    cancel = prepare(a.admin, plan["id"], "CANCELLATION")
    assert cancel.status_code == 201
    docs = cancel.json()
    assert [d["member_id"] for d in docs] == [m0]  # the second farmer never saw anything: nothing to cancel for them
    c = docs[0]
    valid("messaging", "instruction", c)
    assert (
        c["kind"] == "CANCELLATION"
        and c["supersedes"] == sale[m0]["id"]
        and c["status"] == "PREPARED"
        and c["language"] == "hi"
    )
    d1 = plan["allocation_input"]["dates"][0]
    y, m, d = d1.split("-")
    assert c["text"] == (
        "नमस्ते Farmer0 (fictional) जी। आपके 1121 बासमती धान की पहले वाली योजना रद्द हो गई है। कृपया उसके अनुसार कुछ न करें:\n"
        f"• 1200.000 किलो, Alpha APMC में, {int(d)} {MONTHS['hi'][int(m)]} {y} को\n"
        "विवरण के लिए अपने FPO कार्यालय से संपर्क करें।"
    )
    assert send(a.admin, c["id"]).json()["status"] == "PREVIEWED"  # a cancellation does not need an active reservation
    assert by_member(prepare(a.admin, plan["id"], "CANCELLATION").json())[m0]["id"] == c["id"]  # no duplicate
    assert (
        len(call(a.admin, "GET", f"/plans/{plan['id']}/instructions").json()) == 3
    )  # two sale instructions and one cancellation


def test_a_release_with_nobody_told_has_nothing_to_cancel(two: tuple[Fpo, Fpo], market: dict[str, Any]) -> None:
    a, _ = two
    plan, lots = approved_plan(a, market, ("1250.000",))
    ready(a, lots[0]["member_id"], PHONE_A)
    prepare(a.admin, plan["id"])
    call(a.admin, "POST", f"/plans/{plan['id']}/release", {"reason_code": "OTHER"})
    r = prepare(a.admin, plan["id"], "CANCELLATION")
    assert r.status_code == 422 and r.json()["issues"][0]["code"] == "NOTHING_TO_CANCEL"


# ---- who may do what ----
def test_sending_needs_messages_send_and_viewers_only_read(two: tuple[Fpo, Fpo], market: dict[str, Any]) -> None:
    a, _ = two
    plan, lots = approved_plan(a, market, ("1250.000",))
    ready(a, lots[0]["member_id"], PHONE_A)
    _, _, staff = add_user(a, "STAFF")
    _, _, sender = add_user(a, "STAFF", ["messages.send"])
    _, _, viewer = add_user(a, "VIEWER")
    assert prepare(staff, plan["id"]).status_code == 403 and prepare(viewer, plan["id"]).status_code == 403
    ins = prepare(sender, plan["id"]).json()[0]
    assert send(staff, ins["id"]).status_code == 403 and send(viewer, ins["id"]).status_code == 403
    assert send(sender, ins["id"]).status_code == 201
    for s in (staff, viewer):
        assert call(s, "GET", f"/instructions/{ins['id']}").status_code == 200
        assert call(s, "GET", "/instruction-queue").status_code == 200
        assert call(s, "GET", f"/instructions/{ins['id']}/attempts").status_code == 200


# ---- privacy: logs, stores, and the other FPO ----
def test_a_phone_number_appears_nowhere_except_the_contact_table_and_a_permitted_view(
    two: tuple[Fpo, Fpo], market: dict[str, Any], engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    a, _ = two
    plan, lots = approved_plan(a, market, ("1250.000", "800.000"))
    _, _, staff = add_user(a, "STAFF", ["messages.send"])
    outputs: list[str] = []
    for lot, phone in zip(lots, (PHONE_A, PHONE_B), strict=True):
        outputs.append(contact(staff, lot["member_id"], phone).text)
        outputs.append(consent(staff, lot["member_id"]).text)
    outputs.append(contact(staff, lots[0]["member_id"], "not a number 9000000001x").text)
    docs = prepare(staff, plan["id"])
    outputs.append(docs.text)
    for d in docs.json():
        outputs.append(send(staff, d["id"]).text)
        outputs.append(call(staff, "GET", f"/instructions/{d['id']}").text)
        outputs.append(call(staff, "GET", f"/instructions/{d['id']}/attempts").text)
    outputs += [
        call(staff, "GET", "/contacts").text,
        call(staff, "GET", "/instruction-queue").text,
        call(staff, "GET", f"/plans/{plan['id']}/instructions").text,
    ]
    blob = "\n".join(outputs)
    for number in ("9000000001", "9000000002", "+919000000001", "+919000000002"):
        assert number not in blob  # a role without contacts.view_phone never receives a number
    assert "9000000001" not in caplog.text and "9000000002" not in caplog.text
    with engine.connect() as c:
        for table, col in (
            ("instructions", "body"),
            ("instructions", "facts::text"),
            ("delivery_attempts", "preview::text"),
            ("audit_log", "event"),
        ):
            n = c.execute(
                text(f"SELECT count(*) FROM {table} WHERE {col} LIKE '%9000000001%' OR {col} LIKE '%9000000002%'")
            ).scalar_one()
            assert n == 0, (table, col)
        assert (
            c.execute(text("SELECT count(*) FROM member_contacts WHERE fpo_id = :f"), {"f": a.fpo_id}).scalar_one() == 2
        )


def test_another_fpo_sees_and_touches_nothing_of_ours(
    two: tuple[Fpo, Fpo], market: dict[str, Any], engine: Engine
) -> None:
    a, b = two
    plan, lots = approved_plan(a, market, ("1250.000",))
    mid = lots[0]["member_id"]
    ready(a, mid, PHONE_A)
    ins = prepare(a.admin, plan["id"]).json()[0]
    send(a.admin, ins["id"])
    ghost_m, ghost_i = "mbr_" + "0" * 26, "ins_" + "0" * 26
    probes = [
        ("POST", f"/members/{mid}/contact", {"phone": PHONE_B}, f"/members/{ghost_m}/contact"),
        ("GET", f"/members/{mid}/contact/history", None, f"/members/{ghost_m}/contact/history"),
        (
            "POST",
            f"/members/{mid}/consents",
            {"kind": "WHATSAPP", "action": "WITHDRAWN", "notice_version": NOTICE, "method": "PAPER_FORM"},
            f"/members/{ghost_m}/consents",
        ),
        ("GET", f"/members/{mid}/consents", None, f"/members/{ghost_m}/consents"),
        (
            "POST",
            f"/plans/{plan['id']}/instructions",
            {"kind": "SALE_INSTRUCTION"},
            f"/plans/pln_{'0' * 26}/instructions",
        ),
        ("GET", f"/plans/{plan['id']}/instructions", None, f"/plans/pln_{'0' * 26}/instructions"),
        ("GET", f"/instructions/{ins['id']}", None, f"/instructions/{ghost_i}"),
        ("GET", f"/instructions/{ins['id']}/attempts", None, f"/instructions/{ghost_i}/attempts"),
        ("POST", f"/instructions/{ins['id']}/send", {"channel": "PREVIEW"}, f"/instructions/{ghost_i}/send"),
    ]
    for method, path, body, ghost in probes:
        theirs, nothing = call(b.admin, method, path, body), call(b.admin, method, ghost, body)
        assert theirs.status_code == nothing.status_code == 404, (method, path, theirs.text)
        assert theirs.json()["detail"].split()[0] == nothing.json()["detail"].split()[0]
    assert "CROSS_TENANT_REFERENCE" in audit_events(engine, actor_user_id=b.admin_id)
    assert call(b.admin, "GET", "/contacts").json() == [] and call(b.admin, "GET", "/instruction-queue").json() == []
    assert (
        call(a.admin, "GET", f"/members/{mid}/consents").json()["history"][0]["action"] == "GRANTED"
    )  # nothing was changed
    for t in ("member_contacts", "member_consents", "instructions", "delivery_attempts", "personal_data_access_log"):
        assert as_app(engine, None, f"SELECT 1 FROM {t}") == []
        assert as_app(engine, b.fpo_id, f"SELECT 1 FROM {t}") == []


def test_every_messaging_record_is_append_only_for_everyone(
    two: tuple[Fpo, Fpo], market: dict[str, Any], engine: Engine
) -> None:
    a, _ = two
    plan, lots = approved_plan(a, market, ("1250.000",))
    ready(a, lots[0]["member_id"], PHONE_A)
    ins = prepare(a.admin, plan["id"]).json()[0]
    send(a.admin, ins["id"])
    call(a.admin, "GET", "/contacts")
    for t in ("member_contacts", "member_consents", "instructions", "delivery_attempts", "personal_data_access_log"):
        for sql in (f"UPDATE {t} SET fpo_id = fpo_id", f"DELETE FROM {t}"):
            with pytest.raises(DBAPIError):
                as_app(engine, a.fpo_id, sql)
            with pytest.raises(DBAPIError), engine.begin() as c:
                c.execute(text(sql))


def test_unauthenticated_requests_are_refused(two: tuple[Fpo, Fpo]) -> None:
    from fastapi.testclient import TestClient

    from kisaniti_api.main import app

    c = TestClient(app)
    for path in ("/contacts", "/message-templates", "/instruction-queue", f"/instructions/ins_{'0' * 26}"):
        assert c.get(f"/api/v1{path}").status_code == 401
    assert c.post(f"/api/v1/instructions/ins_{'0' * 26}/send", json={"channel": "PREVIEW"}).status_code == 401

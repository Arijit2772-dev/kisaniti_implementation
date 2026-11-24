# ruff: noqa: F811, F401  (fixtures imported from other test modules by name: pytest uses them as arguments)
"""messaging/v1 amendment A1.2: sending through the WhatsApp Cloud API with approved templates (D1, D2).

Meta is never called: the transport is replaced by a fake that records each request and answers as told."""

from __future__ import annotations

import dataclasses
import json
import logging
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

from kisaniti_api.messaging import templates, whatsapp
from tests.test_access import Fpo, access_env, clean_up_access_data, two
from tests.test_messaging import FULL, PHONE_A, approved_plan, consent, prepare, ready, send
from tests.test_org import call
from tests.test_plan import fresh_market, market, valid
from tests.test_whatsapp_webhook import SECRET, TOKEN, post, statuses

MAPPING = {
    f"{k}.v2.{lang}": f"{name}:{lang}"
    for k, name in (("SALE_INSTRUCTION", "kisaniti_sale_v2"), ("CANCELLATION", "kisaniti_cancel_v2"))
    for lang in ("en", "hi", "pa")
}


class FakeMeta:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.answer: Callable[[int], tuple[int, bytes]] = lambda n: (200, self.ok(n))

    @staticmethod
    def ok(n: int) -> bytes:
        return json.dumps({"messaging_product": "whatsapp", "messages": [{"id": f"wamid.FICTIONAL{n:04d}"}]}).encode()

    def __call__(self, url: str, headers: dict[str, str], data: bytes) -> tuple[int, bytes]:
        self.calls.append({"url": url, "headers": headers, "body": json.loads(data)})
        return self.answer(len(self.calls))


@pytest.fixture()
def meta(monkeypatch: pytest.MonkeyPatch) -> FakeMeta:
    fake = FakeMeta()
    monkeypatch.setattr(whatsapp, "transport", fake)
    monkeypatch.setenv("KN_WA_TOKEN", "fictional-token-for-tests")
    monkeypatch.setenv("KN_WA_PHONE_NUMBER_ID", "100000000000001")
    monkeypatch.setenv("KN_WA_API_VERSION", "v23.0")
    monkeypatch.setenv("KN_WA_TEMPLATES", json.dumps(MAPPING))
    monkeypatch.setenv("KN_WA_APP_SECRET", SECRET)
    monkeypatch.setenv("KN_WA_VERIFY_TOKEN", TOKEN)
    return fake


def _with_v2(review: str) -> dict[tuple[str, int], templates.Template]:
    out = dict(templates._all())
    for (tid, ver), t in list(out.items()):
        if ver == 2:
            rv = ("REVIEWED", "test reviewers", "2026-11-24") if review == "REVIEWED" else ("UNREVIEWED", None, None)
            variants = {
                k: dataclasses.replace(v, review_status=rv[0], reviewer=rv[1], reviewed_at=rv[2])
                for k, v in t.variants.items()
            }
            out[(tid, ver)] = dataclasses.replace(t, variants=variants)
    return out


@pytest.fixture()
def reviewed_v2(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """As if the reviewers had signed off the WhatsApp texts: v2 becomes the version messages are prepared with."""
    patched = _with_v2("REVIEWED")
    monkeypatch.setattr(templates, "_all", lambda: patched)
    yield


def sendable(a: Fpo, market: dict[str, Any], kgs: tuple[str, ...] = ("1250.000",)) -> dict[str, Any]:
    plan, lots = approved_plan(a, market, kgs)
    ready(a, lots[0]["member_id"], PHONE_A)
    ins: dict[str, Any] = prepare(a.admin, plan["id"]).json()[0]
    return ins


# ---- the template form and the exact-match check, on their own ----
def test_meta_form_numbers_the_placeholders_and_values_refill_to_exactly_the_prepared_text() -> None:
    v2 = templates.get("SALE_INSTRUCTION", 2)
    v1 = templates.get("SALE_INSTRUCTION", 1)
    assert v2 is not None and v1 is not None
    for lang, var in v2.variants.items():
        form, names = whatsapp.meta_form(var.body)
        assert names == ["farmer", "variety", "lines"] and "{{3}}" in form and "{" not in form.replace("{{", "")[:0]
        from datetime import date

        from kisaniti_core.messaging import Line, render

        lines = [Line("1250.000", "Mansa APMC", date(2026, 11, 30)), Line("400.050", "Mansa APMC", date(2026, 12, 2))]
        prepared = render(var.body, var.line, lang, "Farmer1 (fictional)", "1121", lines, v2.joiner)
        values = whatsapp.parameters(var.body, prepared)
        assert values is not None and values[0] == "Farmer1 (fictional)" and values[1] == "1121"
        assert " ; " in values[2] and "\n" not in values[2]
        old = render(v1.variants[lang].body, v1.variants[lang].line, lang, "Farmer1 (fictional)", "1121", lines)
        assert whatsapp.parameters(var.body, old) is None, "a version 1 text (lines on new lines) is never sent"
        assert whatsapp.parameters(var.body, prepared + " extra") is None
        assert whatsapp.parameters(var.body, prepared.replace("Farmer1", "Farmer1    x")) is None  # 4 spaces


def test_settings_are_all_or_nothing_and_a_wrong_value_switches_the_channel_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for k in ("KN_WA_TOKEN", "KN_WA_PHONE_NUMBER_ID", "KN_WA_API_VERSION", "KN_WA_TEMPLATES"):
        monkeypatch.delenv(k, raising=False)
    assert whatsapp.settings() is None
    monkeypatch.setenv("KN_WA_TOKEN", "t")
    monkeypatch.setenv("KN_WA_PHONE_NUMBER_ID", "123")
    assert whatsapp.settings() is None  # no API version
    monkeypatch.setenv("KN_WA_API_VERSION", "v23.0")
    s = whatsapp.settings()
    assert s is not None and s.templates == {}
    monkeypatch.setenv("KN_WA_TEMPLATES", "")  # what compose passes when the line is missing from .env
    s = whatsapp.settings()
    assert s is not None and s.templates == {}
    for bad_env in (("KN_WA_PHONE_NUMBER_ID", "12a"), ("KN_WA_API_VERSION", "23"), ("KN_WA_TEMPLATES", "[1]")):
        with monkeypatch.context() as m:
            m.setenv(*bad_env)
            assert whatsapp.settings() is None, bad_env
    monkeypatch.setenv("KN_WA_TEMPLATES", json.dumps({"SALE_INSTRUCTION.v2.pa": "Bad Name:pa"}))
    assert whatsapp.settings() is None
    monkeypatch.setenv("KN_WA_TEMPLATES", json.dumps(MAPPING))
    s = whatsapp.settings()
    assert s is not None and s.templates["SALE_INSTRUCTION.v2.pa"] == ("kisaniti_sale_v2", "pa")


# ---- through the API ----
def test_a_version_1_message_is_never_sent_through_whatsapp(
    two: tuple[Fpo, Fpo], market: dict[str, Any], meta: FakeMeta, monkeypatch: pytest.MonkeyPatch
) -> None:
    a, _ = two
    ins = sendable(a, market)
    assert ins["template"]["version"] == 1  # v2 is not reviewed, so v1 stays in use
    r = send(a.admin, ins["id"], channel="WHATSAPP")
    assert r.status_code == 422 and r.json()["issues"][0]["code"] == "CHANNEL_NOT_AVAILABLE"
    assert meta.calls == [] and call(a.admin, "GET", f"/instructions/{ins['id']}/attempts").json() == []


def test_a_text_that_no_longer_equals_its_template_is_refused(
    two: tuple[Fpo, Fpo], market: dict[str, Any], meta: FakeMeta, reviewed_v2: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    a, _ = two
    ins = sendable(a, market)
    changed = dict(templates._all())
    t = changed[("SALE_INSTRUCTION", 2)]
    v = t.variants[ins["language"]]
    variants = {**t.variants, ins["language"]: dataclasses.replace(v, body=v.body.replace("{farmer}", "{farmer}!"))}
    changed[("SALE_INSTRUCTION", 2)] = dataclasses.replace(t, variants=variants)
    monkeypatch.setattr(templates, "_all", lambda: changed)
    r = send(a.admin, ins["id"], channel="WHATSAPP")
    assert r.status_code == 409 and r.json()["issues"][0]["code"] == "TEMPLATE_MISMATCH" and meta.calls == []


def test_an_unreviewed_text_is_refused_before_anything_is_sent(
    two: tuple[Fpo, Fpo], market: dict[str, Any], meta: FakeMeta, monkeypatch: pytest.MonkeyPatch
) -> None:
    a, _ = two
    only_v2 = {k: t for k, t in _with_v2("UNREVIEWED").items() if k[1] == 2}  # no reviewed version at all
    monkeypatch.setattr(templates, "_all", lambda: only_v2)
    ins = sendable(a, market)
    assert ins["template"] == {**ins["template"], "version": 2, "review_status": "UNREVIEWED"}
    r = send(a.admin, ins["id"], channel="WHATSAPP")
    assert r.status_code == 409 and r.json()["issues"][0]["code"] == "TEMPLATE_UNREVIEWED"
    assert meta.calls == []


def test_a_reviewed_message_goes_to_meta_exactly_once_and_its_status_then_follows_the_webhook(
    two: tuple[Fpo, Fpo], market: dict[str, Any], meta: FakeMeta, reviewed_v2: None, caplog: pytest.LogCaptureFixture
) -> None:
    a, _ = two
    ins = sendable(a, market, ("1250.000",))
    assert ins["template"]["version"] == 2
    with caplog.at_level(logging.INFO, logger="kisaniti_api"):
        logging.getLogger("kisaniti_api").addHandler(caplog.handler)
        try:
            r = send(a.admin, ins["id"], channel="WHATSAPP")
        finally:
            logging.getLogger("kisaniti_api").removeHandler(caplog.handler)
    assert r.status_code == 201, r.text
    att = r.json()
    valid("messaging", "delivery_attempt", att)
    assert att["status"] == "QUEUED" and att["provider_message_id"] == "wamid.FICTIONAL0001"
    assert att["simulated"] is False and att["preview"] is None and att["failure"] is None
    (c,) = meta.calls
    assert c["url"] == "https://graph.facebook.com/v23.0/100000000000001/messages"
    assert c["headers"]["Authorization"] == "Bearer fictional-token-for-tests"
    lang = ins["language"]
    assert c["body"]["to"] == FULL[PHONE_A].lstrip("+") and c["body"]["type"] == "template"
    assert c["body"]["template"]["name"] == "kisaniti_sale_v2" and c["body"]["template"]["language"] == {"code": lang}
    values = [p["text"] for p in c["body"]["template"]["components"][0]["parameters"]]
    form, _ = whatsapp.meta_form(templates.get("SALE_INSTRUCTION", 2).variants[lang].body)  # type: ignore[union-attr]
    for i, v in enumerate(values, 1):
        form = form.replace("{{" + str(i) + "}}", v, 1)
    assert form == ins["text"], "what Meta shows the farmer is exactly the prepared, reviewed text"
    assert all(PHONE_A not in rec.getMessage() for rec in caplog.records), "the number is never logged"
    again = send(a.admin, ins["id"], channel="WHATSAPP")
    assert again.status_code == 409 and again.json()["issues"][0]["code"] == "ALREADY_SENT" and len(meta.calls) == 1
    assert post(statuses(("wamid.FICTIONAL0001", "delivered"))).json()["outcomes"] == {"RECORDED": 1}
    (latest,) = call(a.admin, "GET", f"/instructions/{ins['id']}/attempts").json()
    assert latest["status"] == "DELIVERED"


def test_a_refusal_keeps_meta_s_code_and_may_be_tried_again(
    two: tuple[Fpo, Fpo], market: dict[str, Any], meta: FakeMeta, reviewed_v2: None
) -> None:
    a, _ = two
    ins = sendable(a, market)
    meta.answer = lambda n: (400, json.dumps({"error": {"code": 131026, "message": "not text we keep"}}).encode())
    r = send(a.admin, ins["id"], channel="WHATSAPP")
    assert r.status_code == 201
    att = r.json()
    valid("messaging", "delivery_attempt", att)
    assert (att["status"], att["failure"], att["error_codes"]) == ("FAILED", "PROVIDER_REFUSED", [131026])
    assert "not text we keep" not in json.dumps(att)
    assert [d["id"] for d in call(a.admin, "GET", "/instruction-queue").json()] == [ins["id"]]  # needs a person
    meta.answer = lambda n: (200, FakeMeta.ok(n))
    assert send(a.admin, ins["id"], channel="WHATSAPP").json()["status"] == "QUEUED" and len(meta.calls) == 2


@pytest.mark.parametrize(
    "answer",
    [
        pytest.param(lambda n: (_ for _ in ()).throw(TimeoutError()), id="timeout"),
        pytest.param(lambda n: (500, b'{"error": {"code": 131000}}'), id="server-error"),
        pytest.param(lambda n: (200, b'{"messages": []}'), id="no-message-id"),
        pytest.param(lambda n: (200, b"not json"), id="unreadable"),
    ],
)
def test_an_unclear_answer_is_recorded_as_unknown_and_never_repeated(
    two: tuple[Fpo, Fpo], market: dict[str, Any], meta: FakeMeta, reviewed_v2: None, answer: Any
) -> None:
    a, _ = two
    ins = sendable(a, market)
    meta.answer = answer
    att = send(a.admin, ins["id"], channel="WHATSAPP").json()
    valid("messaging", "delivery_attempt", att)
    assert (att["status"], att["failure"], att["error_codes"]) == ("FAILED", "PROVIDER_OUTCOME_UNKNOWN", [])
    meta.answer = lambda n: (200, FakeMeta.ok(n))
    r = send(a.admin, ins["id"], channel="WHATSAPP")
    assert r.status_code == 409 and r.json()["issues"][0]["code"] == "OUTCOME_UNKNOWN" and len(meta.calls) == 1


def test_a_consent_withdrawn_after_preparation_blocks_without_calling_meta(
    two: tuple[Fpo, Fpo], market: dict[str, Any], meta: FakeMeta, reviewed_v2: None
) -> None:
    a, _ = two
    ins = sendable(a, market)
    assert consent(a.admin, ins["member_id"], "WITHDRAWN").status_code == 201
    att = send(a.admin, ins["id"], channel="WHATSAPP").json()
    assert att["status"] == "BLOCKED" and "CONSENT_WITHDRAWN" in att["issues"] and meta.calls == []


def test_the_database_refuses_a_failure_without_a_cause_and_a_cause_without_a_failure(
    two: tuple[Fpo, Fpo], market: dict[str, Any], engine: Engine, meta: FakeMeta, reviewed_v2: None
) -> None:
    a, _ = two
    ins = sendable(a, market)
    for status, failure, codes in (
        ("FAILED", None, []),
        ("QUEUED", "PROVIDER_REFUSED", []),
        ("FAILED", "PROVIDER_OUTCOME_UNKNOWN", [1]),
    ):
        with pytest.raises(DBAPIError), engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO delivery_attempts (id, instruction_id, fpo_id, channel, status, simulated, "
                    "attempted_by, request_id, failure, error_codes) VALUES ('att_01M5VFB9H0D4ZR6JVN7Y7ZQWJ9', :i, :f, "
                    "'WHATSAPP', :s, false, :u, 'test-request-0002', :fl, :ec)"
                ),
                {"i": ins["id"], "f": a.fpo_id, "s": status, "u": a.admin_id, "fl": failure, "ec": codes},
            )

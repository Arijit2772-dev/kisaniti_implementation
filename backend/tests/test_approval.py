# ruff: noqa: F811, F401  (fixtures imported from test_access by name: pytest uses them as arguments)
"""Approval slice (contract approval/v1) on real PostgreSQL: revisions, the decision and the exact reservation.

Fictional FPOs and farmers; fixture prices (see test_plan). The stock ledger is the reference for every reservation
check: the reserved quantity of a lot is read back through the lots API and through SQL, never from the code under
test."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from kisaniti_core.allocation import solve
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

from kisaniti_api.approval import routes as approval_routes
from kisaniti_api.market import import_release as imp
from tests.test_access import (
    GOOD_PW,
    Fpo,
    Session,
    access_env,
    add_user,
    as_app,
    audit_events,
    clean_up_access_data,
    two,
)
from tests.test_devices import dev_id, register
from tests.test_market import make_release
from tests.test_org import balance, call, event_count, lot
from tests.test_plan import (
    ALPHA,
    BETA,
    add_assessment,
    fresh_market,
    market,
    req,
    stock,
    valid,
)

MARKET_TABLES = ("plan_releases", "plan_reservations", "plan_decisions", "plan_revisions")


def make_plan(
    a: Fpo, market: dict[str, Any], kgs: tuple[str, ...] = ("1250.000", "800.000"), **over: Any
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    lots = [stock(a, market, kg, who=f"Farmer{i}") for i, kg in enumerate(kgs)]
    r = call(a.admin, "POST", "/plans", req(market, **over))
    assert r.status_code == 201, r.text
    return r.json(), lots


def acks(s: Session, plan_id: str) -> list[str]:
    ctx = call(s, "GET", f"/plans/{plan_id}/approval-context").json()
    out: list[str] = ctx["required_acknowledgements"]
    return out


def approve(
    s: Session, plan_id: str, revision_no: int = 1, key: str | None = None, ack: list[str] | None = None
) -> Any:
    return call(
        s,
        "POST",
        f"/plans/{plan_id}/approve",
        {"revision_no": revision_no, "acknowledged_warnings": ack if ack is not None else acks(s, plan_id)},
        key,
    )


def state(s: Session, plan_id: str) -> dict[str, Any]:
    r = call(s, "GET", f"/plans/{plan_id}/state")
    assert r.status_code == 200, r.text
    out: dict[str, Any] = r.json()
    valid("approval", "plan_state", out)
    return out


def issue_codes(r: Any) -> list[str]:
    return [i["code"] for i in r.json().get("issues", [])]


def sql_one(engine: Engine, sql: str, **params: Any) -> Any:
    with engine.connect() as c:
        return c.execute(text(sql), params).scalar_one()


def edit_body(base: int, lines: list[tuple[str, str, str, str]]) -> dict[str, Any]:
    return {
        "base_revision_no": base,
        "assignments": [{"lot_id": lt, "mandi_id": m, "date": d, "quantity_kg": q} for lt, m, d, q in lines],
    }


# ---- state, context, revision 1 ----
def test_a_new_plan_is_proposed_with_the_system_proposal_as_revision_one(
    two: tuple[Fpo, Fpo], market: dict[str, Any]
) -> None:
    a, _ = two
    plan, _lots = make_plan(a, market)
    st = state(a.admin, plan["id"])
    assert st["status"] == "PROPOSED" and st["current_revision_no"] == 1 and st["decision"] is None
    assert st["reservation"] == {"state": "NONE", "lines": [], "release": None}
    assert [r["revision_no"] for r in st["revisions"]] == [1] and st["revisions"][0]["superseded"] is False
    revs = call(a.admin, "GET", f"/plans/{plan['id']}/revisions").json()
    assert len(revs) == 1 and revs[0]["id"] is None and revs[0]["kind"] == "SYSTEM_PROPOSAL"
    valid("approval", "revision", revs[0])
    assert revs[0]["assignments"] == plan["allocation_result"]["assignments"]  # read from the plan, not copied
    listed = call(a.admin, "GET", "/plan-states").json()
    assert [s["plan_id"] for s in listed] == [plan["id"]]


def test_the_approval_context_lists_what_would_be_reserved_and_what_to_acknowledge(
    two: tuple[Fpo, Fpo], market: dict[str, Any]
) -> None:
    a, _ = two
    plan, _ = make_plan(a, market)
    ctx = call(a.admin, "GET", f"/plans/{plan['id']}/approval-context").json()
    valid("approval", "approval_context", ctx)
    assert ctx["can_approve"] is True and ctx["blockers"] == [] and ctx["context_warnings"] == []
    assert ctx["required_acknowledgements"] == sorted({w["code"] for w in plan["warnings"]})
    assigned = {x["lot_id"]: x["scheduled_kg"] for x in plan["allocation_result"]["lots"]}
    assert {ln["lot_id"]: ln["assigned_kg"] for ln in ctx["lines"]} == assigned
    assert all(ln["can_reserve"] for ln in ctx["lines"])


# ---- approving ----
def test_every_warning_must_be_acknowledged_and_a_refusal_changes_nothing(
    two: tuple[Fpo, Fpo], market: dict[str, Any], engine: Engine
) -> None:
    a, _ = two
    plan, lots = make_plan(a, market)
    need = acks(a.admin, plan["id"])
    assert len(need) >= 3
    for given in ([], need[:-1]):
        r = approve(a.admin, plan["id"], ack=given)
        assert r.status_code == 422 and issue_codes(r) == ["WARNINGS_NOT_ACKNOWLEDGED"]
        assert need[-1] in r.json()["detail"]
    assert state(a.admin, plan["id"])["status"] == "PROPOSED"
    for lt in lots:
        assert balance(a, lt["id"])["reserved_kg"] == "0.000"
    assert sql_one(engine, "SELECT count(*) FROM plan_decisions") == 0


def test_approval_reserves_exactly_the_assigned_quantity_and_decides_in_one_step(
    two: tuple[Fpo, Fpo], market: dict[str, Any], engine: Engine
) -> None:
    a, _ = two
    plan, lots = make_plan(a, market)
    r = approve(a.admin, plan["id"])
    assert r.status_code == 201, r.text
    dec = r.json()
    valid("approval", "decision", dec)
    assert dec["decision"] == "APPROVED" and dec["revision_no"] == 1 and dec["decided_by"] == a.admin_id
    assigned = {x["lot_id"]: x["scheduled_kg"] for x in plan["allocation_result"]["lots"]}
    assert assigned == {lots[0]["id"]: "1200.000", lots[1]["id"]: "800.000"}
    assert {ln["lot_id"]: ln["reserved_kg"] for ln in dec["reservation"]["lines"]} == assigned
    for lt in lots:
        bal = balance(a, lt["id"])
        assert bal["reserved_kg"] == assigned[lt["id"]] and bal["on_hand_kg"] == lt["registered_quantity_kg"]
        reg = Decimal_kg(lt["registered_quantity_kg"])
        assert Decimal_kg(bal["available_kg"]) == reg - Decimal_kg(assigned[lt["id"]])
    st = state(a.admin, plan["id"])
    assert st["status"] == "APPROVED" and st["reservation"]["state"] == "ACTIVE"
    assert all(ln["remaining_kg"] == ln["reserved_kg"] for ln in st["reservation"]["lines"])
    kinds = [r[0] for r in as_app(engine, a.fpo_id, "SELECT kind FROM stock_events WHERE kind = 'RESERVED'")]
    assert kinds == ["RESERVED", "RESERVED"]
    # the stored proposal itself is untouched
    assert call(a.admin, "GET", f"/plans/{plan['id']}").json() == plan
    row = as_app(engine, a.fpo_id, "SELECT request_id, acknowledged, context FROM plan_decisions")[0]
    assert len(row.request_id) >= 8 and sorted(row.acknowledged) == acks_of(plan) and row.context["can_approve"] is True


def Decimal_kg(s: str) -> Any:
    from decimal import Decimal

    return Decimal(s)


def acks_of(plan: dict[str, Any]) -> list[str]:
    return sorted({w["code"] for w in plan["warnings"]})


def test_the_next_plan_does_not_count_stock_that_is_already_reserved(
    two: tuple[Fpo, Fpo], market: dict[str, Any]
) -> None:
    a, _ = two
    plan, _ = make_plan(a, market, ("1250.000",))
    assert approve(a.admin, plan["id"]).status_code == 201  # reserves 1200 of 1250
    again = call(a.admin, "POST", "/plans", req(market))
    assert again.status_code == 422 and issue_codes(again) == ["NO_ELIGIBLE_LOTS"]  # 50 kg left: less than one step


def test_a_second_approval_never_reserves_twice(two: tuple[Fpo, Fpo], market: dict[str, Any], engine: Engine) -> None:
    a, _ = two
    plan, lots = make_plan(a, market)
    need = acks(a.admin, plan["id"])
    first = approve(a.admin, plan["id"], key="approve-key-0001", ack=need)
    assert first.status_code == 201
    replay = approve(a.admin, plan["id"], key="approve-key-0001", ack=need)  # a double click: same key and body
    assert replay.status_code == 201 and replay.json() == first.json()
    other = approve(a.admin, plan["id"], ack=need)  # no key: refused
    assert other.status_code == 409 and issue_codes(other) == ["ALREADY_DECIDED"]
    changed = call(
        a.admin,
        "POST",
        f"/plans/{plan['id']}/approve",
        {"revision_no": 1, "acknowledged_warnings": []},
        "approve-key-0001",
    )
    assert changed.status_code == 409 and changed.json()["code"] == "IDEMPOTENCY_KEY_REUSED"
    assert balance(a, lots[0]["id"])["reserved_kg"] == "1200.000"
    assert sql_one(engine, "SELECT count(*) FROM plan_reservations") == 2
    assert sql_one(engine, "SELECT count(*) FROM stock_events WHERE kind = 'RESERVED'") == 2


def test_only_the_approver_decides_and_a_viewer_cannot_even_edit(
    two: tuple[Fpo, Fpo], market: dict[str, Any], engine: Engine
) -> None:
    a, _ = two
    plan, _ = make_plan(a, market)
    _, _, staff = add_user(a, "STAFF")
    _, _, viewer = add_user(a, "VIEWER")
    need = acks(a.admin, plan["id"])
    for s in (staff, viewer):
        r = s.post(f"/plans/{plan['id']}/approve", {"revision_no": 1, "acknowledged_warnings": need})
        assert r.status_code == 403
        assert s.post(f"/plans/{plan['id']}/reject", {"revision_no": 1, "reason_code": "OTHER"}).status_code == 403
        assert s.post(f"/plans/{plan['id']}/release", {"reason_code": "OTHER"}).status_code == 403
    assert viewer.post(f"/plans/{plan['id']}/revisions", edit_body(1, [])).status_code == 403
    assert viewer.get(f"/plans/{plan['id']}/state").status_code == 200
    assert "ACCESS_DENIED" in audit_events(engine, fpo_id=a.fpo_id)
    assert state(a.admin, plan["id"])["status"] == "PROPOSED"


# ---- editing: new immutable revisions ----
def choice_keys(plan: dict[str, Any]) -> list[tuple[str, str, str]]:
    return sorted({(c["lot_id"], c["mandi_id"], c["date"]) for c in plan["allocation_input"]["choices"]})


def test_a_manager_edit_is_a_new_revision_the_validator_accepts(two: tuple[Fpo, Fpo], market: dict[str, Any]) -> None:
    a, _ = two
    plan, lots = make_plan(a, market, ("1250.000",))
    alpha, d1, d2 = market["mandi"][ALPHA], plan["allocation_input"]["dates"][0], plan["allocation_input"]["dates"][1]
    r = call(
        a.admin,
        "POST",
        f"/plans/{plan['id']}/revisions",
        edit_body(1, [(lots[0]["id"], alpha, d1, "600.000"), (lots[0]["id"], alpha, d2, "600.000")]),
    )
    assert r.status_code == 201, r.text
    rev = r.json()
    valid("approval", "revision", rev)
    valid("allocation", "allocation_result", rev["allocation_result"])
    valid("allocation", "plan_validation", rev["validation"])
    assert (
        rev["revision_no"] == 2
        and rev["kind"] == "MANAGER_EDIT"
        and rev["base_revision_no"] == 1
        and rev["id"].startswith("rev_")
    )
    assert rev["validation"]["valid"] is True and rev["allocation_result"]["solver"]["name"] == "MANAGER_EDIT"
    assert rev["allocation_result"]["totals"]["scheduled_kg"] == "1200.000"
    assert rev["changes"] == [
        {"lot_id": lots[0]["id"], "mandi_id": alpha, "date": d1, "from_kg": "1200.000", "to_kg": "600.000"},
        {"lot_id": lots[0]["id"], "mandi_id": alpha, "date": d2, "from_kg": "0.000", "to_kg": "600.000"},
    ]
    st = state(a.admin, plan["id"])
    assert st["current_revision_no"] == 2 and [r["superseded"] for r in st["revisions"]] == [True, False]
    revs = call(a.admin, "GET", f"/plans/{plan['id']}/revisions").json()
    assert [r["revision_no"] for r in revs] == [1, 2] and revs[0]["id"] is None
    assert call(a.admin, "GET", f"/plans/{plan['id']}").json() == plan  # the proposal is never edited


def test_approving_an_edit_reserves_exactly_the_edited_quantities(two: tuple[Fpo, Fpo], market: dict[str, Any]) -> None:
    a, _ = two
    plan, lots = make_plan(a, market, ("1250.000",))
    alpha, d1 = market["mandi"][ALPHA], plan["allocation_input"]["dates"][0]
    call(a.admin, "POST", f"/plans/{plan['id']}/revisions", edit_body(1, [(lots[0]["id"], alpha, d1, "700.000")]))
    stale = approve(a.admin, plan["id"], revision_no=1)
    assert stale.status_code == 409 and issue_codes(stale) == ["STALE_REVISION"]
    ok = approve(a.admin, plan["id"], revision_no=2)
    assert ok.status_code == 201 and ok.json()["revision_no"] == 2
    assert balance(a, lots[0]["id"])["reserved_kg"] == "700.000"  # not the 1200 of the proposal


@pytest.mark.parametrize(
    ("mutate", "word"),
    [
        ("over", "over-allocated"),
        ("step", "increment"),
        ("choice", "admissible"),
        ("duplicate", "duplicate"),
        ("zero", "positive"),
        ("lot", "not part"),
    ],
)
def test_an_invalid_edit_is_refused_with_the_validators_issues_and_stores_nothing(
    two: tuple[Fpo, Fpo], market: dict[str, Any], mutate: str, word: str
) -> None:
    a, _ = two
    plan, lots = make_plan(a, market, ("1250.000",))
    alpha, d1 = market["mandi"][ALPHA], plan["allocation_input"]["dates"][0]
    lid = lots[0]["id"]
    lines = {
        "over": [(lid, alpha, d1, "1300.000")],
        "step": [(lid, alpha, d1, "150.000")],
        "choice": [(lid, market["mandi"]["Gamma APMC"], d1, "100.000")],
        "duplicate": [(lid, alpha, d1, "100.000"), (lid, alpha, d1, "100.000")],
        "zero": [(lid, alpha, d1, "0.000")],
        "lot": [("lot_" + "0" * 26, alpha, d1, "100.000")],
    }[mutate]
    r = call(a.admin, "POST", f"/plans/{plan['id']}/revisions", edit_body(1, lines))
    assert r.status_code == 422, r.text
    issues = r.json()["issues"]
    assert issues[0]["code"] == "PLAN_INVALID" and any(word in i["message"] for i in issues[1:]), issues
    assert len(call(a.admin, "GET", f"/plans/{plan['id']}/revisions").json()) == 1


def test_edit_refusals_no_change_stale_base_malformed_and_foreign_fields(
    two: tuple[Fpo, Fpo], market: dict[str, Any]
) -> None:
    a, _ = two
    plan, lots = make_plan(a, market, ("1250.000",))
    same = [(x["lot_id"], x["mandi_id"], x["date"], x["quantity_kg"]) for x in plan["allocation_result"]["assignments"]]
    r = call(a.admin, "POST", f"/plans/{plan['id']}/revisions", edit_body(1, same))
    assert r.status_code == 422 and issue_codes(r) == ["NO_CHANGE"]
    alpha, d1 = market["mandi"][ALPHA], plan["allocation_input"]["dates"][0]
    one = edit_body(1, [(lots[0]["id"], alpha, d1, "100.000")])
    assert (
        call(a.admin, "POST", f"/plans/{plan['id']}/revisions", {**one, "base_revision_no": 3}).json()["issues"][0][
            "code"
        ]
        == "STALE_REVISION"
    )
    assert (
        call(
            a.admin, "POST", f"/plans/{plan['id']}/revisions", edit_body(1, [(lots[0]["id"], alpha, d1, "100")])
        ).status_code
        == 422
    )
    assert call(a.admin, "POST", f"/plans/{plan['id']}/revisions", {**one, "fpo_id": a.fpo_id}).status_code == 422
    one["assignments"][0]["price_inr_per_kg"] = "99.0000"  # an edit cannot introduce a price
    assert call(a.admin, "POST", f"/plans/{plan['id']}/revisions", one).status_code == 422
    assert len(call(a.admin, "GET", f"/plans/{plan['id']}/revisions").json()) == 1


def test_staff_may_prepare_a_revision_and_the_admin_approves_it(two: tuple[Fpo, Fpo], market: dict[str, Any]) -> None:
    a, _ = two
    plan, lots = make_plan(a, market, ("1250.000",))
    _, _, staff = add_user(a, "STAFF")
    alpha, d1 = market["mandi"][ALPHA], plan["allocation_input"]["dates"][0]
    r = staff.post(f"/plans/{plan['id']}/revisions", edit_body(1, [(lots[0]["id"], alpha, d1, "500.000")]))
    assert r.status_code == 201 and r.json()["created_by"] != a.admin_id
    assert approve(a.admin, plan["id"], revision_no=2).status_code == 201


def test_editing_is_idempotent_by_key(two: tuple[Fpo, Fpo], market: dict[str, Any]) -> None:
    a, _ = two
    plan, lots = make_plan(a, market, ("1250.000",))
    alpha, d1 = market["mandi"][ALPHA], plan["allocation_input"]["dates"][0]
    body = edit_body(1, [(lots[0]["id"], alpha, d1, "500.000")])
    r1 = call(a.admin, "POST", f"/plans/{plan['id']}/revisions", body, "edit-key-00001")
    r2 = call(a.admin, "POST", f"/plans/{plan['id']}/revisions", body, "edit-key-00001")
    assert r1.status_code == r2.status_code == 201 and r1.json() == r2.json()
    assert len(call(a.admin, "GET", f"/plans/{plan['id']}/revisions").json()) == 2
    other = edit_body(1, [(lots[0]["id"], alpha, d1, "400.000")])
    assert (
        call(a.admin, "POST", f"/plans/{plan['id']}/revisions", other, "edit-key-00001").json()["code"]
        == "IDEMPOTENCY_KEY_REUSED"
    )


# ---- rejecting, release, and the end of a plan ----
def test_a_rejection_records_the_reason_and_ends_the_plan(
    two: tuple[Fpo, Fpo], market: dict[str, Any], engine: Engine
) -> None:
    a, _ = two
    plan, lots = make_plan(a, market)
    bad = call(a.admin, "POST", f"/plans/{plan['id']}/reject", {"revision_no": 1, "reason_code": "I do not like it"})
    assert bad.status_code == 422
    r = call(a.admin, "POST", f"/plans/{plan['id']}/reject", {"revision_no": 1, "reason_code": "PRICE_CONCERN"})
    assert r.status_code == 201
    valid("approval", "decision", r.json())
    assert (
        r.json()["decision"] == "REJECTED"
        and r.json()["reservation"] is None
        and r.json()["reason_code"] == "PRICE_CONCERN"
    )
    st = state(a.admin, plan["id"])
    assert st["status"] == "REJECTED" and st["reservation"]["state"] == "NONE"
    for lt in lots:
        assert balance(a, lt["id"])["reserved_kg"] == "0.000"
    assert approve(a.admin, plan["id"]).json()["issues"][0]["code"] == "ALREADY_DECIDED"
    alpha, d1 = market["mandi"][ALPHA], plan["allocation_input"]["dates"][0]
    assert (
        call(
            a.admin, "POST", f"/plans/{plan['id']}/revisions", edit_body(1, [(lots[0]["id"], alpha, d1, "100.000")])
        ).json()["issues"][0]["code"]
        == "ALREADY_DECIDED"
    )
    assert (
        call(a.admin, "POST", f"/plans/{plan['id']}/release", {"reason_code": "OTHER"}).json()["issues"][0]["code"]
        == "NOT_APPROVED"
    )
    assert (
        call(a.admin, "POST", f"/plans/{plan['id']}/reject", {"revision_no": 1, "reason_code": "OTHER"}).json()[
            "issues"
        ][0]["code"]
        == "ALREADY_DECIDED"
    )


def test_release_returns_the_remaining_reservation_once(
    two: tuple[Fpo, Fpo], market: dict[str, Any], engine: Engine
) -> None:
    a, _ = two
    plan, lots = make_plan(a, market)
    assert (
        call(a.admin, "POST", f"/plans/{plan['id']}/release", {"reason_code": "OTHER"}).json()["issues"][0]["code"]
        == "NOT_APPROVED"
    )
    approve(a.admin, plan["id"])
    r = call(a.admin, "POST", f"/plans/{plan['id']}/release", {"reason_code": "MARKET_CHANGED"}, "release-key-001")
    assert r.status_code == 201, r.text
    st = r.json()
    valid("approval", "plan_state", st)
    assert st["status"] == "APPROVED"  # the historical decision stays
    assert st["reservation"]["state"] == "RELEASED" and st["reservation"]["release"]["reason_code"] == "MARKET_CHANGED"
    assert all(
        ln["remaining_kg"] == "0.000" and ln["released_kg"] == ln["reserved_kg"] for ln in st["reservation"]["lines"]
    )
    for lt in lots:
        bal = balance(a, lt["id"])
        assert bal["reserved_kg"] == "0.000" and bal["available_kg"] == lt["registered_quantity_kg"]
    again = call(a.admin, "POST", f"/plans/{plan['id']}/release", {"reason_code": "MARKET_CHANGED"}, "release-key-001")
    assert again.status_code == 201 and again.json() == st  # replay
    second = call(a.admin, "POST", f"/plans/{plan['id']}/release", {"reason_code": "MARKET_CHANGED"})
    assert second.status_code == 409 and issue_codes(second) == ["NOTHING_TO_RELEASE"]
    assert (
        call(a.admin, "POST", f"/plans/{plan['id']}/release", {"reason_code": "OTHER"}, "release-key-001").json()[
            "code"
        ]
        == "IDEMPOTENCY_KEY_REUSED"
    )
    kinds = [
        r[0]
        for r in as_app(
            engine,
            a.fpo_id,
            "SELECT kind FROM stock_events WHERE kind IN ('RESERVED','RESERVATION_RELEASED') ORDER BY id",
        )
    ]
    assert kinds == ["RESERVED", "RESERVED", "RESERVATION_RELEASED", "RESERVATION_RELEASED"]
    assert approve(a.admin, plan["id"]).json()["issues"][0]["code"] == "ALREADY_DECIDED"  # an approval is not revived


def test_released_stock_can_be_planned_and_approved_again(two: tuple[Fpo, Fpo], market: dict[str, Any]) -> None:
    a, _ = two
    plan, lots = make_plan(a, market, ("1250.000",))
    approve(a.admin, plan["id"])
    call(a.admin, "POST", f"/plans/{plan['id']}/release", {"reason_code": "PLAN_CANCELLED"})
    again = call(a.admin, "POST", "/plans", req(market))
    assert again.status_code == 201 and approve(a.admin, again.json()["id"]).status_code == 201
    assert balance(a, lots[0]["id"])["reserved_kg"] == "1200.000"


# ---- freshness and the approval context ----
def test_changes_since_the_plan_must_be_acknowledged_too(
    two: tuple[Fpo, Fpo], market: dict[str, Any], engine: Engine, tmp_path: Path
) -> None:
    a, _ = two
    plan, lots = make_plan(a, market, ("1250.000",))
    # 1. stock changed (30 kg lost: 1220 left, still enough for the 1200 planned)
    assert (
        call(
            a.admin,
            "POST",
            f"/lots/{lots[0]['id']}/adjustments",
            {"quantity_delta_kg": "-30.000", "reason_code": "SPILLAGE"},
        ).status_code
        == 201
    )
    # 2. a sensor with a fresh HIGH assessment was linked
    d = dev_id()
    register(a, d)
    call(a.admin, "POST", f"/lots/{lots[0]['id']}/sensor-links", {"device_id": d})
    add_assessment(engine, a, d, "HIGH", timedelta(minutes=5))
    # 3. a newer price release was imported
    from tests.test_plan import rows_for

    imp.import_release(engine, make_release(tmp_path, obs=rows_for(datetime(2025, 9, 10).date(), [3300] * 12)))
    ctx = call(a.admin, "GET", f"/plans/{plan['id']}/approval-context").json()
    valid("approval", "approval_context", ctx)
    codes = {w["code"] for w in ctx["context_warnings"]}
    assert codes == {"STOCK_CHANGED_SINCE_PLAN", "STORAGE_CHANGED", "NEWER_PRICE_DATASET"}
    assert set(ctx["required_acknowledgements"]) >= codes
    storage = next(w for w in ctx["context_warnings"] if w["code"] == "STORAGE_CHANGED")["params"]
    assert (storage["from"], storage["to"]) == ("UNKNOWN", "HIGH")
    old = sorted({w["code"] for w in plan["warnings"]})
    refused = approve(a.admin, plan["id"], ack=old)
    assert refused.status_code == 422 and issue_codes(refused) == ["WARNINGS_NOT_ACKNOWLEDGED"]
    assert balance(a, lots[0]["id"])["reserved_kg"] == "0.000"
    assert approve(a.admin, plan["id"]).status_code == 201


def test_a_plan_that_no_longer_fits_the_stock_cannot_be_approved(
    two: tuple[Fpo, Fpo], market: dict[str, Any], engine: Engine
) -> None:
    a, _ = two
    plan, lots = make_plan(a, market, ("1000.000",))  # plans all 1000
    call(
        a.admin, "POST", f"/lots/{lots[0]['id']}/adjustments", {"quantity_delta_kg": "-100.000", "reason_code": "OTHER"}
    )
    ctx = call(a.admin, "GET", f"/plans/{plan['id']}/approval-context").json()
    assert ctx["can_approve"] is False and ctx["blockers"] == ["INSUFFICIENT_AVAILABLE"]
    assert ctx["lines"][0]["available_kg"] == "900.000" and ctx["lines"][0]["can_reserve"] is False
    r = approve(a.admin, plan["id"])
    assert r.status_code == 409 and issue_codes(r) == ["INSUFFICIENT_AVAILABLE"]
    assert (
        sql_one(engine, "SELECT count(*) FROM plan_decisions") == 0
        and balance(a, lots[0]["id"])["reserved_kg"] == "0.000"
    )


def test_a_refusal_by_the_ledger_rolls_the_decision_back(
    two: tuple[Fpo, Fpo], market: dict[str, Any], engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Simulate a race: the pre-check says the stock is there, but the ledger's trigger refuses the reservation."""
    a, _ = two
    plan, lots = make_plan(a, market, ("1000.000",))
    call(
        a.admin, "POST", f"/lots/{lots[0]['id']}/adjustments", {"quantity_delta_kg": "-100.000", "reason_code": "OTHER"}
    )
    real = approval_routes._context

    def optimistic(*args: Any, **kw: Any) -> Any:
        ctx = real(*args, **kw)
        for ln in ctx["lines"]:
            ln["can_reserve"] = True
        ctx["blockers"] = [b for b in ctx["blockers"] if b != "INSUFFICIENT_AVAILABLE"]
        return ctx

    monkeypatch.setattr(approval_routes, "_context", optimistic)
    r = approve(a.admin, plan["id"], ack=acks(a.admin, plan["id"]))
    assert r.status_code == 409 and issue_codes(r) == ["INSUFFICIENT_AVAILABLE"]
    assert sql_one(engine, "SELECT count(*) FROM plan_decisions") == 0
    assert sql_one(engine, "SELECT count(*) FROM plan_reservations") == 0
    assert sql_one(engine, "SELECT count(*) FROM stock_events WHERE kind = 'RESERVED'") == 0
    assert state(a.admin, plan["id"])["status"] == "PROPOSED"


def test_a_plan_that_was_not_proposed_cannot_be_decided(
    two: tuple[Fpo, Fpo], market: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    import dataclasses

    from kisaniti_api.plan import builder

    a, _ = two
    stock(a, market, "1250.000")
    real = solve
    monkeypatch.setattr(
        "kisaniti_api.plan.builder.solve",
        lambda inp, **kw: dataclasses.replace(
            real(inp, **kw), optimality="FEASIBLE_NOT_PROVEN", termination="TIME_LIMIT"
        ),
    )
    plan = call(a.admin, "POST", "/plans", req(market)).json()
    assert plan["status"] == "NOT_PROPOSED"
    assert state(a.admin, plan["id"])["status"] == "NOT_PROPOSED"
    assert approve(a.admin, plan["id"], ack=[]).json()["issues"][0]["code"] == "PLAN_NOT_PROPOSED"
    assert (
        call(a.admin, "POST", f"/plans/{plan['id']}/reject", {"revision_no": 1, "reason_code": "OTHER"}).json()[
            "issues"
        ][0]["code"]
        == "PLAN_NOT_PROPOSED"
    )
    ctx = call(a.admin, "GET", f"/plans/{plan['id']}/approval-context").json()
    assert "PLAN_NOT_PROPOSED" in ctx["blockers"] and ctx["can_approve"] is False


def test_a_revision_that_allocates_nothing_can_be_rejected_but_not_approved(
    two: tuple[Fpo, Fpo], market: dict[str, Any]
) -> None:
    a, _ = two
    plan, _ = make_plan(a, market, ("1250.000",))
    r = call(a.admin, "POST", f"/plans/{plan['id']}/revisions", edit_body(1, []))
    assert r.status_code == 201 and r.json()["allocation_result"]["totals"]["scheduled_kg"] == "0.000"
    refused = approve(a.admin, plan["id"], revision_no=2)
    assert refused.status_code == 422 and issue_codes(refused) == ["NOTHING_TO_RESERVE"]
    assert (
        call(a.admin, "POST", f"/plans/{plan['id']}/reject", {"revision_no": 2, "reason_code": "OTHER"}).status_code
        == 201
    )


# ---- concurrency ----
def run_parallel(fns: list[Any]) -> list[Any]:
    gate, lock, out = threading.Barrier(len(fns)), threading.Lock(), []

    def worker(fn: Any) -> None:
        gate.wait()
        res = fn()
        with lock:
            out.append(res)

    ts = [threading.Thread(target=worker, args=(f,)) for f in fns]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    return out


def test_simultaneous_approvals_of_one_plan_reserve_once(
    two: tuple[Fpo, Fpo], market: dict[str, Any], engine: Engine
) -> None:
    a, _ = two
    plan, lots = make_plan(a, market)
    need = acks(a.admin, plan["id"])
    sessions = [Session(a.admin_name) for _ in range(8)]
    results = run_parallel(
        [
            (
                lambda s=s, i=i: call(
                    s,
                    "POST",
                    f"/plans/{plan['id']}/approve",
                    {"revision_no": 1, "acknowledged_warnings": need},
                    f"parallel-key-{i:03d}",
                ).status_code
            )
            for i, s in enumerate(sessions)
        ]
    )
    assert sorted(results) == [201] + [409] * 7
    assert sql_one(engine, "SELECT count(*) FROM plan_decisions") == 1
    assert sql_one(engine, "SELECT count(*) FROM plan_reservations") == 2
    assert [balance(a, lt["id"])["reserved_kg"] for lt in lots] == ["1200.000", "800.000"]


def test_simultaneous_double_click_with_one_key_returns_one_decision(
    two: tuple[Fpo, Fpo], market: dict[str, Any], engine: Engine
) -> None:
    a, _ = two
    plan, lots = make_plan(a, market)
    need = acks(a.admin, plan["id"])
    sessions = [Session(a.admin_name) for _ in range(6)]
    results = run_parallel(
        [
            lambda s=s: call(
                s,
                "POST",
                f"/plans/{plan['id']}/approve",
                {"revision_no": 1, "acknowledged_warnings": need},
                "same-click-key-1",
            ).status_code
            for s in sessions
        ]
    )
    assert results == [201] * 6
    assert sql_one(engine, "SELECT count(*) FROM plan_decisions") == 1
    assert balance(a, lots[0]["id"])["reserved_kg"] == "1200.000"


def test_two_plans_for_the_same_stock_cannot_both_be_approved(
    two: tuple[Fpo, Fpo], market: dict[str, Any], engine: Engine
) -> None:
    a, _ = two
    lt = stock(a, market, "1000.000")
    p1 = call(a.admin, "POST", "/plans", req(market)).json()
    p2 = call(a.admin, "POST", "/plans", req(market)).json()  # made before any reservation: both plan all 1000 kg
    assert (
        p1["allocation_result"]["totals"]["scheduled_kg"]
        == p2["allocation_result"]["totals"]["scheduled_kg"]
        == "1000.000"
    )
    n1, n2 = acks(a.admin, p1["id"]), acks(a.admin, p2["id"])
    s1, s2 = Session(a.admin_name), Session(a.admin_name)

    def attempt(n: int, s: Session, pid: str, need: list[str]) -> tuple[int, int]:
        r = call(s, "POST", f"/plans/{pid}/approve", {"revision_no": 1, "acknowledged_warnings": need})
        return n, r.status_code

    results = run_parallel([lambda: attempt(1, s1, p1["id"], n1), lambda: attempt(2, s2, p2["id"], n2)])
    assert sorted(code for _, code in results) == [201, 409]
    assert balance(a, lt["id"])["reserved_kg"] == "1000.000" and balance(a, lt["id"])["available_kg"] == "0.000"
    assert sql_one(engine, "SELECT count(*) FROM plan_decisions") == 1
    loser = {1: p1, 2: p2}[next(n for n, code in results if code == 409)]["id"]
    assert state(a.admin, loser)["status"] == "PROPOSED"  # still open; nothing half-applied
    assert call(a.admin, "GET", f"/plans/{loser}/approval-context").json()["blockers"] == ["INSUFFICIENT_AVAILABLE"]


def test_an_edit_and_an_approval_at_the_same_moment_never_split_the_plan(
    two: tuple[Fpo, Fpo], market: dict[str, Any], engine: Engine
) -> None:
    a, _ = two
    plan, lots = make_plan(a, market, ("1250.000",))
    need = acks(a.admin, plan["id"])
    alpha, d1 = market["mandi"][ALPHA], plan["allocation_input"]["dates"][0]
    s1, s2 = Session(a.admin_name), Session(a.admin_name)
    run_parallel(
        [
            lambda: call(
                s1, "POST", f"/plans/{plan['id']}/approve", {"revision_no": 1, "acknowledged_warnings": need}
            ).status_code,
            lambda: call(
                s2, "POST", f"/plans/{plan['id']}/revisions", edit_body(1, [(lots[0]["id"], alpha, d1, "500.000")])
            ).status_code,
        ]
    )
    st = state(a.admin, plan["id"])
    row = sql_one(engine, "SELECT revision_no FROM plan_decisions") if st["decision"] else None
    if st["decision"]:
        assert row == st["current_revision_no"]  # the decision is on the revision that is current
        assert balance(a, lots[0]["id"])["reserved_kg"] == ("1200.000" if row == 1 else "500.000")


# ---- isolation and immutability ----
def test_another_fpo_sees_decides_and_reserves_nothing_of_ours(
    two: tuple[Fpo, Fpo], market: dict[str, Any], engine: Engine
) -> None:
    a, b = two
    plan, lots = make_plan(a, market)
    approve(a.admin, plan["id"])
    ghost = f"pln_{'0' * 26}"
    pairs = [
        ("GET", "/state", None),
        ("GET", "/revisions", None),
        ("GET", "/approval-context", None),
        ("POST", "/revisions", edit_body(1, [])),
        ("POST", "/approve", {"revision_no": 1, "acknowledged_warnings": []}),
        ("POST", "/reject", {"revision_no": 1, "reason_code": "OTHER"}),
        ("POST", "/release", {"reason_code": "OTHER"}),
    ]
    for method, tail, body in pairs:
        theirs = call(b.admin, method, f"/plans/{plan['id']}{tail}", body)
        nothing = call(b.admin, method, f"/plans/{ghost}{tail}", body)
        assert theirs.status_code == nothing.status_code == 404, (method, tail)
        assert theirs.json()["detail"] == nothing.json()["detail"]
    assert "CROSS_TENANT_REFERENCE" in audit_events(engine, actor_user_id=b.admin_id)
    assert call(b.admin, "GET", "/plan-states").json() == []
    assert balance(a, lots[0]["id"])["reserved_kg"] == "1200.000"  # nothing changed by the attempts
    assert as_app(engine, None, "SELECT 1 FROM plan_decisions") == []
    for t in MARKET_TABLES:
        assert as_app(engine, b.fpo_id, f"SELECT 1 FROM {t}") == []
        assert as_app(engine, None, f"SELECT 1 FROM {t}") == []
    assert len(as_app(engine, a.fpo_id, "SELECT 1 FROM plan_decisions")) == 1


def test_decisions_reservations_and_revisions_are_append_only_for_everyone(
    two: tuple[Fpo, Fpo], market: dict[str, Any], engine: Engine
) -> None:
    a, _ = two
    plan, lots = make_plan(a, market, ("1250.000",))
    alpha, d1 = market["mandi"][ALPHA], plan["allocation_input"]["dates"][0]
    call(a.admin, "POST", f"/plans/{plan['id']}/revisions", edit_body(1, [(lots[0]["id"], alpha, d1, "500.000")]))
    approve(a.admin, plan["id"], revision_no=2)
    call(a.admin, "POST", f"/plans/{plan['id']}/release", {"reason_code": "OTHER"})
    for t in MARKET_TABLES:
        for sql in (f"UPDATE {t} SET fpo_id = fpo_id", f"DELETE FROM {t}"):
            with pytest.raises(DBAPIError):
                as_app(engine, a.fpo_id, sql)
            with pytest.raises(DBAPIError), engine.begin() as c:
                c.execute(text(sql))
    with pytest.raises(DBAPIError):  # a decision cannot name another FPO's plan
        as_app(
            engine,
            a.fpo_id,
            "INSERT INTO plan_decisions (plan_id, fpo_id, revision_no, decision, reason_code, context, "
            "decided_by, request_id) "
            "VALUES (:p, :f, 1, 'REJECTED', 'OTHER', '{}', :u, 'request-0001')",
            p=plan["id"],
            f="fpo_" + "0" * 26,
            u=a.admin_id,
        )


def test_unauthenticated_requests_are_refused(two: tuple[Fpo, Fpo], market: dict[str, Any]) -> None:
    from fastapi.testclient import TestClient

    from kisaniti_api.main import app

    c = TestClient(app)
    pid = f"pln_{'0' * 26}"
    assert c.get("/api/v1/plan-states").status_code == 401
    assert c.get(f"/api/v1/plans/{pid}/state").status_code == 401
    assert (
        c.post(f"/api/v1/plans/{pid}/approve", json={"revision_no": 1, "acknowledged_warnings": []}).status_code == 401
    )

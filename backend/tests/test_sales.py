# ruff: noqa: F811, F401  (fixtures imported from test_access by name: pytest uses them as arguments)
"""Sales slice (contract sales/v1) on real PostgreSQL: actual sales that consume the reservation, off-plan sales and
their reconciliation, voids, execution, the planned-against-actual report, outcomes and replies. Fictional people.
Expected numbers are worked out by hand (Decimal) in the tests, never taken from the code under test."""

from __future__ import annotations

import logging
import threading
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

from kisaniti_api.sales import routes as sales_routes
from tests.test_access import Fpo, Session, access_env, add_user, as_app, audit_events, clean_up_access_data, two
from tests.test_approval import acks, approve, make_plan, state
from tests.test_messaging import consent, contact, prepare, ready, send
from tests.test_org import balance, call
from tests.test_plan import ALPHA, fresh_market, market, stock, valid

PAST = "2025-11-10"
KG = Decimal("0.001")


def approved(
    a: Fpo, market: dict[str, Any], kgs: tuple[str, ...] = ("1250.000",)
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    plan, lots = make_plan(a, market, kgs)
    assert approve(a.admin, plan["id"]).status_code == 201
    return plan, lots


def ref(plan: dict[str, Any], market: dict[str, Any], day: int = 0) -> dict[str, str]:
    return {"plan_id": plan["id"], "mandi_id": market["mandi"][ALPHA], "date": plan["allocation_input"]["dates"][day]}


_n = {"j": 0}


def sale_body(
    lot: dict[str, Any],
    market: dict[str, Any],
    plan_ref: dict[str, str] | None,
    qty: str = "500.000",
    price: str = "4400.00",
    **over: Any,
) -> dict[str, Any]:
    _n["j"] += 1
    body = {
        "lot_id": lot["id"],
        "plan_ref": plan_ref,
        "mandi_id": market["mandi"][ALPHA],
        "sale_date": PAST,
        "quantity_kg": qty,
        "price_inr_per_quintal": price,
        "j_form_no": f"J/2025/{_n['j']:05d}",
    }
    body.update(over)
    return body


def post_sale(s: Session, body: dict[str, Any], key: str | None = None) -> Any:
    return call(s, "POST", "/sales", body, key)


def events(engine: Engine, fpo: Fpo, lot_id: str) -> list[tuple[str, int]]:
    return [
        (r.kind, r.quantity_g)
        for r in as_app(
            engine, fpo.fpo_id, "SELECT kind, quantity_g FROM stock_events WHERE lot_id = :l ORDER BY id", l=lot_id
        )
    ]


def money(kg: str, price: str) -> str:
    return str((Decimal(kg) * Decimal(price) / 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN))


def issue_codes(r: Any) -> list[str]:
    return [i["code"] for i in r.json().get("issues", [])]


# ---- a sale that follows a plan ----
def test_a_linked_sale_consumes_the_reservation_and_sells_in_one_transaction(
    two: tuple[Fpo, Fpo], market: dict[str, Any], engine: Engine
) -> None:
    a, _ = two
    plan, lots = approved(a, market)
    lot = lots[0]
    r = post_sale(a.admin, sale_body(lot, market, ref(plan, market), "500.000", "4400.00"))
    assert r.status_code == 201, r.text
    sale = r.json()
    valid("sales", "actual_sale", sale)
    assert (
        sale["amount_inr"] == "22000.00" == money("500.000", "4400.00")
        and sale["consumed_kg"] == "500.000"
        and sale["voided"] is None
    )
    assert (
        sale["origin"] == "SYNTHETIC"
        and sale["plan_ref"] == ref(plan, market)
        and sale["member_id"] == lot["member_id"]
    )
    assert events(engine, a, lot["id"]) == [
        ("REGISTERED", 1_250_000),
        ("RESERVED", 1_200_000),
        ("RESERVATION_CONSUMED", 500_000),
        ("SOLD", 500_000),
    ]
    assert balance(a, lot["id"]) == {
        "on_hand_kg": "750.000",
        "reserved_kg": "700.000",
        "available_kg": "50.000",
        "sold_kg": "500.000",
    }
    ex = call(a.admin, "GET", f"/plans/{plan['id']}/execution").json()
    valid("sales", "execution", ex)
    assert (
        ex["status"] == "PARTIAL"
        and ex["lines"][0]["planned_kg"] == "1200.000"
        and ex["lines"][0]["sold_kg"] == "500.000"
        and ex["lines"][0]["status"] == "PARTIAL"
    )
    assert ex["lots"][0] == {
        "lot_id": lot["id"],
        "reserved_kg": "1200.000",
        "consumed_kg": "500.000",
        "released_kg": "0.000",
        "released_for_sale_kg": "0.000",
        "remaining_kg": "700.000",
        "off_plan_sold_kg": "0.000",
    }
    assert (
        state(a.admin, plan["id"])["reservation"]["lines"][0]["remaining_kg"] == "700.000"
    )  # approval/v1's view agrees
    r2 = post_sale(a.admin, sale_body(lot, market, ref(plan, market), "700.000", "4500.00"))
    assert r2.status_code == 201
    ex = call(a.admin, "GET", f"/plans/{plan['id']}/execution").json()
    assert (
        ex["status"] == "COMPLETED"
        and ex["lines"][0]["status"] == "COMPLETED"
        and ex["lots"][0]["remaining_kg"] == "0.000"
    )
    assert balance(a, lot["id"])["reserved_kg"] == "0.000" and balance(a, lot["id"])["on_hand_kg"] == "50.000"
    st = state(a.admin, plan["id"])
    assert (
        st["status"] == "APPROVED" and st["reservation"]["state"] == "RELEASED" and st["reservation"]["release"] is None
    )  # nobody released it
    over = post_sale(a.admin, sale_body(lot, market, ref(plan, market), "1.000"))
    assert over.status_code == 409 and issue_codes(over) == ["EXCEEDS_RESERVATION"]
    assert len(call(a.admin, "GET", f"/sales?plan_id={plan['id']}").json()) == 2


def test_a_sale_larger_than_its_line_is_a_deviation_not_a_refusal(two: tuple[Fpo, Fpo], market: dict[str, Any]) -> None:
    a, _ = two
    plan, lots = make_plan(a, market, ("1250.000",))
    d1, d2 = plan["allocation_input"]["dates"][:2]
    alpha = market["mandi"][ALPHA]
    edit = {
        "base_revision_no": 1,
        "assignments": [
            {"lot_id": lots[0]["id"], "mandi_id": alpha, "date": d, "quantity_kg": "600.000"} for d in (d1, d2)
        ],
    }
    assert call(a.admin, "POST", f"/plans/{plan['id']}/revisions", edit).status_code == 201
    assert approve(a.admin, plan["id"], revision_no=2).status_code == 201
    assert (
        post_sale(a.admin, sale_body(lots[0], market, ref(plan, market, 0), "800.000")).status_code == 201
    )  # line 1 plans 600
    ex = call(a.admin, "GET", f"/plans/{plan['id']}/execution").json()
    assert [(ln["date"], ln["planned_kg"], ln["sold_kg"], ln["status"], ln["deviation_kg"]) for ln in ex["lines"]] == [
        (d1, "600.000", "800.000", "COMPLETED", "200.000"),
        (d2, "600.000", "0.000", "NOT_STARTED", "0.000"),
    ]
    assert ex["status"] == "PARTIAL" and ex["lots"][0]["remaining_kg"] == "400.000"


def test_the_amount_is_rounded_half_even_to_the_paisa(two: tuple[Fpo, Fpo], market: dict[str, Any]) -> None:
    a, _ = two
    plan, lots = approved(a, market)
    # 333.333 kg x 4401.55 / 100 = 14 671.7... (worked out below, not by the code under test)
    want = money("333.333", "4401.55")
    got = post_sale(a.admin, sale_body(lots[0], market, ref(plan, market), "333.333", "4401.55")).json()["amount_inr"]
    assert (
        got
        == want
        == str((Decimal("333.333") * Decimal("4401.55") / 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN))
    )
    # an exact tie: 25.000 kg x 0.10 per quintal = 2.5 paise; half-even gives 2 paise, half-up would give 3
    tie = post_sale(a.admin, sale_body(lots[0], market, None, "25.000", "0.10")).json()
    assert (
        tie["amount_inr"]
        == "0.02"
        == str((Decimal("25.000") * Decimal("0.10") / 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN))
    )
    assert (
        post_sale(a.admin, sale_body(lots[0], market, None, "15.000", "0.10")).json()["amount_inr"] == "0.02"
    )  # 1.5 paise -> 2


# ---- sales outside the plan and their reconciliation ----
def test_an_off_plan_sale_within_the_free_stock_touches_no_reservation(
    two: tuple[Fpo, Fpo], market: dict[str, Any], engine: Engine
) -> None:
    a, _ = two
    plan, lots = approved(a, market)
    r = post_sale(a.admin, sale_body(lots[0], market, None, "50.000"))  # 1250 on hand, 1200 reserved: 50 free
    assert (
        r.status_code == 201
        and r.json()["consumed_kg"] == "0.000"
        and r.json()["plan_ref"] is None
        and r.json()["released_for_sale"] == []
    )
    assert events(engine, a, lots[0]["id"])[-1] == ("SOLD", 50_000)
    assert balance(a, lots[0]["id"])["reserved_kg"] == "1200.000"
    ex = call(a.admin, "GET", f"/plans/{plan['id']}/execution").json()
    assert ex["lots"][0]["off_plan_sold_kg"] == "50.000" and ex["lines"][0]["sold_kg"] == "0.000"


def test_an_off_plan_sale_beyond_free_stock_asks_which_reservation_to_release(
    two: tuple[Fpo, Fpo], market: dict[str, Any], engine: Engine
) -> None:
    a, _ = two
    plan, lots = approved(a, market)
    lid = lots[0]["id"]
    asked = post_sale(a.admin, sale_body(lots[0], market, None, "300.000"))
    assert asked.status_code == 409 and issue_codes(asked)[0] == "RECONCILIATION_REQUIRED"
    holds = [(i["path"], i["message"]) for i in asked.json()["issues"][1:]]
    assert holds == [(plan["id"], "1200.000")]  # the system names the candidates, it does not choose
    assert (
        events(engine, a, lid)[-1] == ("RESERVED", 1_200_000) and balance(a, lid)["on_hand_kg"] == "1250.000"
    )  # nothing was written
    for bad in (
        [{"plan_id": plan["id"], "quantity_kg": "100.000"}],  # shortfall is 250: not enough
        [{"plan_id": plan["id"], "quantity_kg": "400.000"}],  # more than needed
        [{"plan_id": "pln_" + "0" * 26, "quantity_kg": "250.000"}],  # a plan that holds nothing
    ):
        r = post_sale(a.admin, sale_body(lots[0], market, None, "300.000", release_reservations=bad))
        assert r.status_code == 422 and issue_codes(r) == ["RELEASE_INVALID"], r.text
    dup = post_sale(
        a.admin,
        sale_body(
            lots[0],
            market,
            None,
            "300.000",
            release_reservations=[
                {"plan_id": plan["id"], "quantity_kg": "100.000"},
                {"plan_id": plan["id"], "quantity_kg": "150.000"},
            ],
        ),
    )
    assert dup.status_code == 422 and issue_codes(dup) == ["RELEASE_INVALID"]
    ok = post_sale(
        a.admin,
        sale_body(
            lots[0], market, None, "300.000", release_reservations=[{"plan_id": plan["id"], "quantity_kg": "250.000"}]
        ),
    )
    assert ok.status_code == 201, ok.text
    sale = ok.json()
    valid("sales", "actual_sale", sale)
    assert (
        sale["released_for_sale"] == [{"plan_id": plan["id"], "quantity_kg": "250.000"}]
        and sale["consumed_kg"] == "0.000"
    )
    assert events(engine, a, lid)[-2:] == [("RESERVATION_RELEASED", 250_000), ("SOLD", 300_000)]
    assert balance(a, lid) == {
        "on_hand_kg": "950.000",
        "reserved_kg": "950.000",
        "available_kg": "0.000",
        "sold_kg": "300.000",
    }
    ex = call(a.admin, "GET", f"/plans/{plan['id']}/execution").json()
    assert (
        ex["lots"][0]["released_for_sale_kg"] == "250.000"
        and ex["lots"][0]["remaining_kg"] == "950.000"
        and ex["lots"][0]["off_plan_sold_kg"] == "300.000"
    )
    assert state(a.admin, plan["id"])["reservation"]["lines"][0]["remaining_kg"] == "950.000"


def test_more_than_the_lot_holds_is_refused_and_a_release_is_not_needed_when_stock_is_free(
    two: tuple[Fpo, Fpo], market: dict[str, Any]
) -> None:
    a, _ = two
    plan, lots = approved(a, market)
    r = post_sale(a.admin, sale_body(lots[0], market, None, "1250.001"))
    assert r.status_code == 409 and issue_codes(r) == ["EXCEEDS_STOCK"]
    free = post_sale(
        a.admin,
        sale_body(
            lots[0], market, None, "10.000", release_reservations=[{"plan_id": plan["id"], "quantity_kg": "10.000"}]
        ),
    )
    assert free.status_code == 422 and issue_codes(free) == ["RELEASE_INVALID"]
    linked = post_sale(
        a.admin,
        sale_body(
            lots[0],
            market,
            ref(plan, market),
            "10.000",
            release_reservations=[{"plan_id": plan["id"], "quantity_kg": "10.000"}],
        ),
    )
    assert linked.status_code == 422 and issue_codes(linked) == ["RELEASE_INVALID"]


# ---- validation ----
@pytest.mark.parametrize(
    ("over", "status", "code"),
    [
        ({"sale_date": "2999-01-01"}, 422, "SALE_DATE_IN_FUTURE"),
        ({"sale_date": "2025-02-30"}, 422, "SALE_DATE_INVALID"),
        ({"mandi_id": "mnd_" + "0" * 26}, 422, "UNKNOWN_MANDI"),
        ({"price_inr_per_quintal": "0.00"}, 422, "PRICE_INVALID"),
        ({"price_inr_per_quintal": "4400.001"}, 422, "PRICE_INVALID"),
        ({"quantity_kg": "0.000"}, 422, "QUANTITY_INVALID"),
        ({"quantity_kg": "500"}, 422, "QUANTITY_INVALID"),
        ({"quantity_kg": "-5.000"}, 422, "QUANTITY_INVALID"),
    ],
)
def test_a_bad_sale_is_refused_with_its_code_and_records_nothing(
    two: tuple[Fpo, Fpo], market: dict[str, Any], engine: Engine, over: dict[str, Any], status: int, code: str
) -> None:
    a, _ = two
    lot = stock(a, market, "1000.000")
    r = post_sale(a.admin, sale_body(lot, market, None, **over))
    assert r.status_code == status and issue_codes(r)[0] == code, r.text
    assert as_app(engine, a.fpo_id, "SELECT count(*) FROM actual_sales")[0][0] == 0 and events(
        engine, a, lot["id"]
    ) == [("REGISTERED", 1_000_000)]


def test_more_refusals_harvest_date_j_form_assignment_and_foreign_objects(
    two: tuple[Fpo, Fpo], market: dict[str, Any]
) -> None:
    a, b = two
    from tests.test_org import lot as make_lot
    from tests.test_org import member

    harvested = make_lot(a, member(a, "Late (fictional)"), market["commodity"], "800.000", harvested_on="2025-11-20")
    r = post_sale(a.admin, sale_body(harvested, market, None, "10.000", sale_date="2025-11-19"))
    assert r.status_code == 422 and issue_codes(r) == ["SALE_BEFORE_HARVEST"]
    assert post_sale(a.admin, sale_body(harvested, market, None, "10.000", sale_date="2025-11-20")).status_code == 201
    first = sale_body(harvested, market, None, "10.000", j_form_no="J/DUP/1", sale_date="2025-11-21")
    assert post_sale(a.admin, first).status_code == 201
    dup = post_sale(a.admin, {**first, "quantity_kg": "5.000"})
    assert dup.status_code == 409 and issue_codes(dup) == ["DUPLICATE_J_FORM"]
    other_lot = stock(a, market, "100.000", who="Other")
    assert (
        post_sale(a.admin, {**first, "lot_id": other_lot["id"]}).status_code == 201
    )  # the same number on another lot is a different sale
    for bad_j in ("J 2025", "", "-J", "J" + "1" * 45):
        assert post_sale(a.admin, sale_body(harvested, market, None, "1.000", j_form_no=bad_j)).status_code == 422
    plan, lots = approved(a, market, ("500.000",))
    wrong_mandi = {**ref(plan, market), "mandi_id": market["mandi"]["Beta APMC"]}
    r = post_sale(a.admin, sale_body(lots[0], market, wrong_mandi, "10.000"))
    assert r.status_code == 422 and issue_codes(r) == ["ASSIGNMENT_NOT_FOUND"]
    other = approved(b, market, ("500.000",))
    assert (
        post_sale(a.admin, sale_body(lots[0], market, ref(other[0], market), "10.000")).status_code == 404
    )  # another FPO's plan
    assert post_sale(a.admin, sale_body(other[1][0], market, None, "10.000")).status_code == 404  # another FPO's lot
    pending, plots = make_plan(a, market, ("300.000",))  # a plan nobody approved
    r = post_sale(a.admin, sale_body(plots[0], market, ref(pending, market), "10.000"))
    assert r.status_code == 409 and issue_codes(r) == ["NOT_APPROVED"]
    for extra in ({"amount_inr": "1.00"}, {"fpo_id": a.fpo_id}, {"buyer": "Trader"}):
        assert post_sale(a.admin, {**sale_body(harvested, market, None, "1.000"), **extra}).status_code == 422
    _, _, viewer = add_user(a, "VIEWER")
    assert post_sale(viewer, sale_body(harvested, market, None, "1.000")).status_code == 403


def test_a_repeated_request_with_the_same_key_records_one_sale(
    two: tuple[Fpo, Fpo], market: dict[str, Any], engine: Engine
) -> None:
    a, _ = two
    plan, lots = approved(a, market)
    body = sale_body(lots[0], market, ref(plan, market), "100.000")
    r1, r2 = post_sale(a.admin, body, "sale-key-0001"), post_sale(a.admin, body, "sale-key-0001")
    assert r1.status_code == r2.status_code == 201 and r1.json() == r2.json()
    assert (
        as_app(engine, a.fpo_id, "SELECT count(*) FROM actual_sales")[0][0] == 1
        and balance(a, lots[0]["id"])["sold_kg"] == "100.000"
    )
    changed = post_sale(a.admin, {**body, "quantity_kg": "200.000"}, "sale-key-0001")
    assert changed.status_code == 409 and changed.json()["code"] == "IDEMPOTENCY_KEY_REUSED"
    assert post_sale(a.admin, body).status_code == 409  # without the key it is the same J-form number again


def test_a_failure_after_the_ledger_events_leaves_nothing_behind(
    two: tuple[Fpo, Fpo], market: dict[str, Any], engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    a, _ = two
    plan, lots = approved(a, market)
    before = events(engine, a, lots[0]["id"])

    def boom(*args: Any) -> int:
        raise RuntimeError("simulated crash after the events were written")

    monkeypatch.setattr(sales_routes, "_amount_paise", boom)
    r = post_sale(a.admin, sale_body(lots[0], market, ref(plan, market), "100.000"))
    assert r.status_code == 500
    assert (
        events(engine, a, lots[0]["id"]) == before
        and as_app(engine, a.fpo_id, "SELECT count(*) FROM actual_sales")[0][0] == 0
    )


# ---- voids ----
def test_a_void_returns_the_stock_but_not_the_reservation_and_leaves_the_sale_on_record(
    two: tuple[Fpo, Fpo], market: dict[str, Any], engine: Engine
) -> None:
    a, _ = two
    plan, lots = approved(a, market)
    sale = post_sale(a.admin, sale_body(lots[0], market, ref(plan, market), "400.000")).json()
    _, _, staff = add_user(a, "STAFF")
    assert call(staff, "POST", f"/sales/{sale['id']}/void", {"reason_code": "ENTRY_ERROR"}).status_code == 403
    r = call(a.admin, "POST", f"/sales/{sale['id']}/void", {"reason_code": "ENTRY_ERROR"})
    assert r.status_code == 201
    voided = r.json()
    valid("sales", "actual_sale", voided)
    assert voided["voided"]["reason_code"] == "ENTRY_ERROR" and voided["voided"]["voided_by"] == a.admin_id
    assert events(engine, a, lots[0]["id"])[-1] == ("ADJUSTED", 400_000)
    assert balance(a, lots[0]["id"]) == {
        "on_hand_kg": "1250.000",
        "reserved_kg": "800.000",
        "available_kg": "450.000",
        "sold_kg": "400.000",
    }
    ex = call(a.admin, "GET", f"/plans/{plan['id']}/execution").json()
    assert (
        ex["lines"][0]["sold_kg"] == "0.000" and ex["lines"][0]["status"] == "NOT_STARTED"
    )  # a voided sale is not counted
    assert (
        ex["lots"][0]["consumed_kg"] == "400.000" and ex["lots"][0]["remaining_kg"] == "800.000"
    )  # but the reservation stays used
    assert call(a.admin, "GET", f"/sales/{sale['id']}").json()["voided"] is not None
    again = call(a.admin, "POST", f"/sales/{sale['id']}/void", {"reason_code": "OTHER"})
    assert again.status_code == 409 and issue_codes(again) == ["ALREADY_VOIDED"]
    assert call(a.admin, "POST", f"/sales/{sale['id']}/void", {"reason_code": "typo"}).status_code == 422
    # the J-form number of a voided sale stays taken: a corrected entry needs the correct number
    assert post_sale(a.admin, sale_body(lots[0], market, None, "5.000", j_form_no=sale["j_form_no"])).status_code == 409


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


def test_simultaneous_sales_can_never_take_the_stock_below_zero(
    two: tuple[Fpo, Fpo], market: dict[str, Any], engine: Engine
) -> None:
    a, _ = two
    lot = stock(a, market, "1000.000")
    sessions = [Session(a.admin_name) for _ in range(6)]
    codes = run_parallel(
        [
            lambda s=s, i=i: post_sale(s, sale_body(lot, market, None, "300.000", j_form_no=f"J/RACE/{i}")).status_code
            for i, s in enumerate(sessions)
        ]
    )
    assert sorted(codes) == [201, 201, 201, 409, 409, 409]
    assert balance(a, lot["id"])["on_hand_kg"] == "100.000" and balance(a, lot["id"])["sold_kg"] == "900.000"
    assert as_app(engine, a.fpo_id, "SELECT count(*) FROM actual_sales")[0][0] == 3


def test_simultaneous_linked_sales_cannot_consume_more_than_was_reserved(
    two: tuple[Fpo, Fpo], market: dict[str, Any]
) -> None:
    a, _ = two
    plan, lots = approved(a, market)  # 1200 reserved
    sessions = [Session(a.admin_name) for _ in range(5)]
    codes = run_parallel(
        [
            lambda s=s, i=i: post_sale(
                s, sale_body(lots[0], market, ref(plan, market), "500.000", j_form_no=f"J/LINK/{i}")
            ).status_code
            for i, s in enumerate(sessions)
        ]
    )
    assert sorted(codes) == [201, 201, 409, 409, 409]  # 2 x 500 fit into 1200, a third would not
    ex = call(a.admin, "GET", f"/plans/{plan['id']}/execution").json()
    assert ex["lots"][0]["consumed_kg"] == "1000.000" and ex["lots"][0]["remaining_kg"] == "200.000"
    assert balance(a, lots[0]["id"])["reserved_kg"] == "200.000"


def test_a_sale_and_a_release_at_the_same_moment_leave_a_consistent_ledger(
    two: tuple[Fpo, Fpo], market: dict[str, Any]
) -> None:
    a, _ = two
    plan, lots = approved(a, market)
    s1, s2 = Session(a.admin_name), Session(a.admin_name)
    out = run_parallel(
        [
            lambda: ("sale", post_sale(s1, sale_body(lots[0], market, ref(plan, market), "800.000")).status_code),
            lambda: ("release", call(s2, "POST", f"/plans/{plan['id']}/release", {"reason_code": "OTHER"}).status_code),
        ]
    )
    bal = balance(a, lots[0]["id"])
    sold = Decimal(bal["sold_kg"])
    assert Decimal(bal["on_hand_kg"]) >= 0 and Decimal(bal["reserved_kg"]) >= 0 and Decimal(bal["available_kg"]) >= 0
    assert Decimal(bal["on_hand_kg"]) + sold == Decimal("1250.000")
    done = dict(out)
    if done["sale"] == 201:
        assert (
            sold == Decimal("800.000") and Decimal(bal["reserved_kg"]) == 0
            if done["release"] == 201
            else Decimal(bal["reserved_kg"]) == Decimal("400.000")
        )
    else:
        assert sold == 0 and done["sale"] == 409


# ---- the report ----
def test_the_report_puts_planned_and_actual_side_by_side_with_exact_arithmetic(
    two: tuple[Fpo, Fpo], market: dict[str, Any]
) -> None:
    a, _ = two
    plan, lots = approved(a, market, ("1250.000", "800.000"))
    m0, m1 = lots[0]["member_id"], lots[1]["member_id"]
    post_sale(a.admin, sale_body(lots[0], market, ref(plan, market), "500.000", "4400.00"))
    post_sale(a.admin, sale_body(lots[0], market, ref(plan, market), "700.000", "4500.00"))
    post_sale(a.admin, sale_body(lots[1], market, ref(plan, market), "400.000", "4300.00"))
    rep = call(a.admin, "GET", f"/plans/{plan['id']}/report").json()
    valid("sales", "report", rep)
    planned_price = Decimal(market["bases"][-1] + 50)  # the plan priced Alpha at the newest index plus its basis
    l0, l1 = rep["lines"][0], rep["lines"][1]
    assert Decimal(l0["planned_price_inr_per_quintal"]) == planned_price
    avg0 = ((Decimal("500") * 4400 + Decimal("700") * 4500) / Decimal("1200")).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_EVEN
    )  # 4458.33
    assert l0["sold_kg"] == "1200.000" and Decimal(l0["actual_avg_price_inr_per_quintal"]) == avg0 == Decimal("4458.33")
    assert Decimal(l0["price_gap_inr_per_quintal"]) == avg0 - planned_price
    assert l0["actual_value_inr"] == "53500.00" and l0["planned_value_inr"] == money("1200.000", str(planned_price))
    assert (
        l1["sold_kg"] == "400.000"
        and l1["actual_value_inr"] == "17200.00"
        and Decimal(l1["actual_avg_price_inr_per_quintal"]) == Decimal("4300.00")
    )
    t = rep["totals"]
    assert (
        t["planned_kg"] == "2000.000"
        and t["sold_kg"] == "1600.000"
        and t["actual_value_inr"] == "70700.00"
        and t["off_plan_kg"] == "0.000"
    )
    f = {x["farmer_id"]: x for x in rep["farmers"]}
    assert (
        f[m0]["participating_kg"] == "1250.000"
        and f[m0]["planned_ratio"]["display"] == "0.960000"
        and f[m0]["actual_ratio"]["display"] == "0.960000"
    )
    assert f[m1]["planned_ratio"]["display"] == "1.000000" and f[m1]["actual_ratio"]["display"] == "0.500000"
    assert rep["lowest_planned_display"] == "0.960000" and rep["lowest_actual_display"] == "0.500000"
    assert "separate records" in rep["label"]
    pend, _ = make_plan(a, market, ("300.000",))
    assert call(a.admin, "GET", f"/plans/{pend['id']}/report").status_code == 409
    assert call(a.admin, "GET", f"/plans/{pend['id']}/execution").status_code == 409


def test_outcomes_keep_synthetic_and_real_rows_apart(
    two: tuple[Fpo, Fpo], market: dict[str, Any], engine: Engine
) -> None:
    a, _ = two
    plan, lots = approved(a, market, ("1250.000", "800.000"))
    with engine.begin() as c:  # the second farmer is a real person (this FPO's rows are fictional by default)
        c.execute(text("UPDATE members SET origin = 'REAL' WHERE id = :m"), {"m": lots[1]["member_id"]})
    post_sale(a.admin, sale_body(lots[0], market, ref(plan, market), "500.000", "4400.00"))
    post_sale(a.admin, sale_body(lots[1], market, ref(plan, market), "400.000", "4300.00"))
    out = call(a.admin, "GET", "/forecast-outcomes").json()
    valid("sales", "outcomes", out)
    planned = Decimal(market["bases"][-1] + 50)
    assert out["synthetic"]["pairs"] == 1 and out["real"]["pairs"] == 1
    assert Decimal(out["synthetic"]["mean_error_inr_per_quintal"]) == Decimal(4400) - planned
    assert Decimal(out["real"]["mean_error_inr_per_quintal"]) == Decimal(4300) - planned
    assert Decimal(out["real"]["mean_abs_error_inr_per_quintal"]) == abs(Decimal(4300) - planned)
    assert {p["origin"] for p in out["pairs"]} == {"SYNTHETIC", "REAL"} and "not evidence" in out["label"]
    fresh = call(Session(a.admin_name), "GET", "/forecast-outcomes").json()
    assert fresh["real"]["pairs"] == 1


def test_no_sales_means_no_outcomes_and_voided_sales_do_not_count(two: tuple[Fpo, Fpo], market: dict[str, Any]) -> None:
    a, _ = two
    plan, lots = approved(a, market)
    empty = call(a.admin, "GET", "/forecast-outcomes").json()
    assert (
        empty["real"] == {"pairs": 0, "mean_abs_error_inr_per_quintal": None, "mean_error_inr_per_quintal": None}
        and empty["synthetic"]["pairs"] == 0
    )
    s = post_sale(a.admin, sale_body(lots[0], market, ref(plan, market), "100.000")).json()
    call(a.admin, "POST", f"/sales/{s['id']}/void", {"reason_code": "ENTRY_ERROR"})
    assert call(a.admin, "GET", "/forecast-outcomes").json()["synthetic"]["pairs"] == 0
    rep = call(a.admin, "GET", f"/plans/{plan['id']}/report").json()
    assert rep["totals"]["sold_kg"] == "0.000" and rep["lines"][0]["actual_avg_price_inr_per_quintal"] is None


# ---- how sales meet the messages ----
def test_a_fully_sold_plan_is_not_a_cancelled_plan(two: tuple[Fpo, Fpo], market: dict[str, Any]) -> None:
    a, _ = two
    plan, lots = approved(a, market)
    ready(a, lots[0]["member_id"], "9000000001")
    ins = prepare(a.admin, plan["id"]).json()[0]
    assert send(a.admin, ins["id"]).json()["status"] == "PREVIEWED"
    post_sale(a.admin, sale_body(lots[0], market, ref(plan, market), "1200.000"))
    late = send(a.admin, ins["id"]).json()
    assert late["status"] == "BLOCKED" and late["issues"] == ["COMMITMENT_NOT_ACTIVE"]  # nothing is committed any more
    r = prepare(a.admin, plan["id"], "CANCELLATION")
    assert r.status_code == 409 and r.json()["issues"][0]["code"] == "NOT_RELEASED"  # it was used, not cancelled


# ---- replies ----
def test_a_reply_is_stored_and_shown_but_never_acted_on(
    two: tuple[Fpo, Fpo], market: dict[str, Any], engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    a, _ = two
    plan, lots = approved(a, market)
    mid = lots[0]["member_id"]
    ready(a, mid, "9000000001")
    ins = prepare(a.admin, plan["id"]).json()[0]
    secret = "ਮੈਂ ਸਹਿਮਤ ਨਹੀਂ, ਮੇਰਾ ਨੰਬਰ 9876543210 ਹੈ"
    r = call(a.admin, "POST", f"/instructions/{ins['id']}/replies", {"text": secret})
    assert r.status_code == 201, r.text
    rep = r.json()
    valid("sales", "reply", rep)
    assert (
        rep["text"] == secret
        and rep["source"] == "STAFF_ENTERED"
        and rep["member_id"] == mid
        and rep["instruction_id"] == ins["id"]
    )
    assert call(a.admin, "GET", f"/instructions/{ins['id']}/replies").json() == [rep]
    assert call(a.admin, "GET", "/replies").json() == [rep]
    consents_before = call(a.admin, "GET", f"/members/{mid}/consents").json()
    assert consents_before["current"][1]["state"] == "GRANTED"  # an angry reply does not withdraw consent by itself
    assert (
        state(a.admin, plan["id"])["status"] == "APPROVED"
        and balance(a, lots[0]["id"])["reserved_kg"] == "1200.000"
        and balance(a, lots[0]["id"])["sold_kg"] == "0.000"
    )
    assert call(a.admin, "GET", f"/instructions/{ins['id']}").json()["status"] == "PREPARED"
    assert "9876543210" not in caplog.text and "ਸਹਿਮਤ" not in caplog.text
    assert as_app(engine, a.fpo_id, "SELECT count(*) FROM audit_log WHERE event::text LIKE '%9876543210%'")[0][0] == 0


def test_replies_need_message_rights_and_obey_the_limits(two: tuple[Fpo, Fpo], market: dict[str, Any]) -> None:
    a, _ = two
    plan, lots = approved(a, market)
    ready(a, lots[0]["member_id"], "9000000001")
    ins = prepare(a.admin, plan["id"]).json()[0]
    _, _, staff = add_user(a, "STAFF")
    _, _, sender = add_user(a, "STAFF", ["messages.send"])
    _, _, viewer = add_user(a, "VIEWER")
    for s in (staff, viewer):
        assert call(s, "POST", f"/instructions/{ins['id']}/replies", {"text": "ok"}).status_code == 403
        assert call(s, "GET", "/replies").status_code == 403
    assert call(sender, "POST", f"/instructions/{ins['id']}/replies", {"text": "ok"}).status_code == 201
    assert call(a.admin, "POST", f"/instructions/{ins['id']}/replies", {"text": ""}).status_code == 422
    assert call(a.admin, "POST", f"/instructions/{ins['id']}/replies", {"text": "x" * 501}).status_code == 422
    assert call(a.admin, "POST", f"/instructions/{ins['id']}/replies", {"text": "x" * 500}).status_code == 201
    for extra in ({"consent": "WITHDRAWN"}, {"member_id": lots[0]["member_id"]}, {"accepted": True}):
        assert call(a.admin, "POST", f"/instructions/{ins['id']}/replies", {"text": "ok", **extra}).status_code == 422


# ---- isolation and immutability ----
def test_another_fpo_sees_and_touches_nothing_of_ours(
    two: tuple[Fpo, Fpo], market: dict[str, Any], engine: Engine
) -> None:
    a, b = two
    plan, lots = approved(a, market)
    ready(a, lots[0]["member_id"], "9000000001")
    ins = prepare(a.admin, plan["id"]).json()[0]
    sale = post_sale(a.admin, sale_body(lots[0], market, ref(plan, market), "100.000")).json()
    call(a.admin, "POST", f"/instructions/{ins['id']}/replies", {"text": "ok"})
    ghost_s, ghost_p, ghost_i = "sal_" + "0" * 26, "pln_" + "0" * 26, "ins_" + "0" * 26
    probes = [
        ("GET", f"/sales/{sale['id']}", None, f"/sales/{ghost_s}"),
        ("POST", f"/sales/{sale['id']}/void", {"reason_code": "OTHER"}, f"/sales/{ghost_s}/void"),
        ("GET", f"/plans/{plan['id']}/execution", None, f"/plans/{ghost_p}/execution"),
        ("GET", f"/plans/{plan['id']}/report", None, f"/plans/{ghost_p}/report"),
        ("POST", f"/instructions/{ins['id']}/replies", {"text": "x"}, f"/instructions/{ghost_i}/replies"),
        ("GET", f"/instructions/{ins['id']}/replies", None, f"/instructions/{ghost_i}/replies"),
    ]
    for method, path, body, ghost in probes:
        theirs, nothing = call(b.admin, method, path, body), call(b.admin, method, ghost, body)
        assert theirs.status_code == nothing.status_code == 404, (method, path)
        assert theirs.json()["detail"].split()[0] == nothing.json()["detail"].split()[0]
    assert "CROSS_TENANT_REFERENCE" in audit_events(engine, actor_user_id=b.admin_id)
    assert call(b.admin, "GET", "/sales").json() == [] and call(b.admin, "GET", "/replies").json() == []
    assert call(b.admin, "GET", "/forecast-outcomes").json()["synthetic"]["pairs"] == 0
    assert call(a.admin, "GET", f"/sales/{sale['id']}").json()["voided"] is None
    for t in ("actual_sales", "sale_releases", "sale_voids", "replies"):
        assert as_app(engine, None, f"SELECT 1 FROM {t}") == [] and as_app(engine, b.fpo_id, f"SELECT 1 FROM {t}") == []


def test_sales_releases_voids_and_replies_are_append_only_for_everyone(
    two: tuple[Fpo, Fpo], market: dict[str, Any], engine: Engine
) -> None:
    a, _ = two
    plan, lots = approved(a, market)
    ready(a, lots[0]["member_id"], "9000000001")
    ins = prepare(a.admin, plan["id"]).json()[0]
    s1 = post_sale(a.admin, sale_body(lots[0], market, ref(plan, market), "100.000")).json()
    post_sale(
        a.admin,
        sale_body(
            lots[0], market, None, "250.000", release_reservations=[{"plan_id": plan["id"], "quantity_kg": "200.000"}]
        ),
    )
    call(a.admin, "POST", f"/sales/{s1['id']}/void", {"reason_code": "OTHER"})
    call(a.admin, "POST", f"/instructions/{ins['id']}/replies", {"text": "ok"})
    for t in ("actual_sales", "sale_releases", "sale_voids", "replies"):
        assert as_app(engine, a.fpo_id, f"SELECT count(*) FROM {t}")[0][0] >= 1, t
        for sql in (f"UPDATE {t} SET fpo_id = fpo_id", f"DELETE FROM {t}"):
            with pytest.raises(DBAPIError):
                as_app(engine, a.fpo_id, sql)
            with pytest.raises(DBAPIError), engine.begin() as c:
                c.execute(text(sql))


def test_unauthenticated_requests_are_refused(two: tuple[Fpo, Fpo]) -> None:
    from fastapi.testclient import TestClient

    from kisaniti_api.main import app

    c = TestClient(app)
    for path in (
        "/sales",
        f"/sales/sal_{'0' * 26}",
        f"/plans/pln_{'0' * 26}/execution",
        f"/plans/pln_{'0' * 26}/report",
        "/forecast-outcomes",
        "/replies",
    ):
        assert c.get(f"/api/v1{path}").status_code == 401
    assert c.post("/api/v1/sales", json={}).status_code == 401

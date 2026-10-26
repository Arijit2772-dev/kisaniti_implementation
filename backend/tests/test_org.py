# ruff: noqa: F811, F401  (fixtures imported from test_access by name: pytest uses them as arguments)
"""Organisation slice (contract org/v1) on real PostgreSQL: members, locations, lots, placements, exact stock ledger.

Fictional FPOs and farmers only. The ledger invariants are enforced by a database trigger, so several tests write
through SQL as the restricted role (what a future approval or sale writer will do) and with many writers at once.
"""

from __future__ import annotations

import json
import threading
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from kisaniti_core.ids import new_id
from referencing import Registry, Resource
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from tests.test_access import (
    Fpo,
    Session,
    access_env,
    add_user,
    as_app,
    audit_events,
    clean_up_access_data,
    make_fpo,
    strip,
    two,
    uname,
)

CONTRACTS = Path(__file__).resolve().parents[2] / "contracts"


def valid(name: str, doc: Any) -> None:
    reg: Registry = Registry()
    for d in (CONTRACTS / "org" / "v1", CONTRACTS / "shared" / "v1"):
        for f in d.glob("*.schema.json"):
            body = json.loads(f.read_text())
            reg = reg.with_resource(body["$id"], Resource.from_contents(body))
    sid = json.loads((CONTRACTS / "org" / "v1" / f"{name}.schema.json").read_text())["$id"]
    errors = [e.message for e in Draft202012Validator({"$ref": sid}, registry=reg).iter_errors(doc)]
    assert errors == [], errors


def call(s: Session, method: str, path: str, body: Any = None, key: str | None = None) -> Any:
    headers = {**s.csrf, **({"Idempotency-Key": key} if key else {})}
    return s.client.request(method, f"/api/v1{path}", json=body, headers=headers)


@pytest.fixture()
def commodity(engine: Engine) -> str:
    with engine.begin() as c:
        row = c.execute(text("SELECT id FROM commodities WHERE source_name = 'Paddy(Basmati)'")).first()
        if row:
            return str(row[0])
        cid = new_id("cmd")
        c.execute(
            text(
                "INSERT INTO commodities (id, source_name, crop, msp_applies) "
                "VALUES (:i, 'Paddy(Basmati)', 'PADDY', false)"
            ),
            {"i": cid},
        )
        return cid


def member(f: Fpo, name: str = "Gurpreet (fictional)", lang: str = "pa") -> dict[str, Any]:
    r = call(f.admin, "POST", "/members", {"display_name": name, "village": "Patran", "preferred_language": lang})
    assert r.status_code == 201, r.text
    out: dict[str, Any] = r.json()
    return out


def location(f: Fpo, name: str = "Centre (fictional)") -> dict[str, Any]:
    r = call(
        f.admin,
        "POST",
        "/locations",
        {"name": name, "kind": "COLLECTION_CENTRE", "latitude": 30.27, "longitude": 76.05},
    )
    assert r.status_code == 201, r.text
    return dict(r.json())


def lot(f: Fpo, m: dict[str, Any], cmd: str, kg: str = "1000.000", **extra: Any) -> dict[str, Any]:
    r = call(
        f.admin, "POST", "/lots", {"member_id": m["id"], "commodity_id": cmd, "registered_quantity_kg": kg, **extra}
    )
    assert r.status_code == 201, r.text
    return dict(r.json())


def iso(d: datetime) -> str:
    return d.strftime("%Y-%m-%dT%H:%M:%S.") + f"{d.microsecond // 1000:03d}Z"


def event_count(engine: Engine, lot_id: str) -> int:
    with engine.connect() as c:
        return int(c.execute(text("SELECT count(*) FROM stock_events WHERE lot_id = :l"), {"l": lot_id}).scalar_one())


def sql_event(engine: Engine, fpo_id: str, lot_id: str, kind: str, grams: int, reason: str | None = None) -> None:
    """Write a ledger event as the restricted tenant role, in its own transaction (what a later slice's writer does)."""
    with engine.begin() as c:
        c.execute(text("SET LOCAL ROLE kisaniti_app"))
        c.execute(text("SELECT set_config('app.fpo_id', :f, true)"), {"f": fpo_id})
        c.execute(
            text(
                "INSERT INTO stock_events (lot_id, fpo_id, kind, quantity_g, reason_code) VALUES (:l, :f, :k, :q, :r)"
            ),
            {"l": lot_id, "f": fpo_id, "k": kind, "q": grams, "r": reason},
        )


def balance(f: Fpo, lot_id: str) -> dict[str, str]:
    return dict(call(f.admin, "GET", f"/lots/{lot_id}").json()["balance"])


# ---- members, locations, lots --------------------------------------------------------------------------------------
def test_member_and_location_records_match_the_contract_and_carry_no_contact_data(two: tuple[Fpo, Fpo]) -> None:
    a, _ = two
    m, loc = member(a), location(a)
    valid("member", m)
    valid("location", loc)
    assert m["fpo_id"] == a.fpo_id and m["origin"] == "SYNTHETIC" and "phone" not in m
    assert (
        call(a.admin, "POST", "/members", {"display_name": "x", "preferred_language": "pa", "phone": "+91"}).status_code
        == 422
    )
    assert call(a.admin, "POST", "/members", {"display_name": "x", "preferred_language": "fr"}).status_code == 422
    assert (
        call(a.admin, "POST", "/locations", {"name": "x", "kind": "FARM", "latitude": 30.0}).status_code == 422
    )  # half a coordinate


def test_member_can_be_changed_and_deactivated_but_not_through_unknown_fields(two: tuple[Fpo, Fpo]) -> None:
    a, _ = two
    m = member(a)
    r = call(a.admin, "PATCH", f"/members/{m['id']}", {"preferred_language": "hi", "active": False})
    assert r.status_code == 200 and r.json()["preferred_language"] == "hi" and r.json()["active"] is False
    valid("member", r.json())
    assert call(a.admin, "PATCH", f"/members/{m['id']}", {}).status_code == 422
    assert call(a.admin, "PATCH", f"/members/{m['id']}", {"fpo_id": "x"}).status_code == 422


def test_lot_registration_creates_the_registered_event_and_a_derived_balance(
    engine: Engine, two: tuple[Fpo, Fpo], commodity: str
) -> None:
    a, _ = two
    m, loc = member(a), location(a)
    t = lot(
        a,
        m,
        commodity,
        "1250.000",
        source_variety="1121",
        harvested_on="2026-10-12",
        location_id=loc["id"],
        quantity_verified=True,
    )
    valid("lot", t)
    assert t["balance"] == {
        "on_hand_kg": "1250.000",
        "reserved_kg": "0.000",
        "available_kg": "1250.000",
        "sold_kg": "0.000",
    }
    assert t["current_location_id"] == loc["id"] and t["verified_by"] == a.admin_id and t["verified_at"]
    events = call(a.admin, "GET", f"/lots/{t['id']}/events").json()
    assert [e["kind"] for e in events] == ["REGISTERED"] and events[0]["quantity_kg"] == "1250.000"
    valid("stock_event", events[0])
    assert lot(a, m, commodity)["verified_by"] is None


@pytest.mark.parametrize(
    ("kg", "problem"),
    [
        ("10", "INVALID_DECIMAL"),
        ("10.5", "INVALID_DECIMAL"),
        ("10.5555", "PRECISION_EXCEEDED"),
        ("0.000", "QUANTITY_NOT_POSITIVE"),
        ("-5.000", "OUT_OF_RANGE"),
        ("1e3", "INVALID_DECIMAL"),
        ("1,000.000", "INVALID_DECIMAL"),
        ("", "INVALID_DECIMAL"),
    ],
)
def test_quantities_are_exact_decimal_strings_and_never_rounded(
    two: tuple[Fpo, Fpo], commodity: str, kg: str, problem: str
) -> None:
    a, _ = two
    m = member(a)
    r = call(a.admin, "POST", "/lots", {"member_id": m["id"], "commodity_id": commodity, "registered_quantity_kg": kg})
    assert r.status_code == 422 and r.json()["issues"][0]["code"] == problem, r.text


def test_a_float_quantity_and_a_client_supplied_remaining_total_are_rejected(
    two: tuple[Fpo, Fpo], commodity: str
) -> None:
    a, _ = two
    m = member(a)
    base = {"member_id": m["id"], "commodity_id": commodity}
    assert call(a.admin, "POST", "/lots", {**base, "registered_quantity_kg": 10.5}).status_code == 422
    assert (
        call(
            a.admin, "POST", "/lots", {**base, "registered_quantity_kg": "10.000", "available_kg": "10.000"}
        ).status_code
        == 422
    )
    assert (
        call(a.admin, "POST", "/lots", {**base, "registered_quantity_kg": "10.000", "fpo_id": a.fpo_id}).status_code
        == 422
    )


def test_unknown_commodity_is_a_validation_error(two: tuple[Fpo, Fpo]) -> None:
    a, _ = two
    r = call(
        a.admin,
        "POST",
        "/lots",
        {"member_id": member(a)["id"], "commodity_id": new_id("cmd"), "registered_quantity_kg": "1.000"},
    )
    assert r.status_code == 422 and r.json()["issues"][0]["code"] == "UNKNOWN_COMMODITY"


def test_a_placement_without_a_time_uses_the_databases_clock_so_a_fast_application_clock_cannot_hide_it(
    two: tuple[Fpo, Fpo], commodity: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BUG-008: the placement was stamped with the application's clock and read back against the database's clock; with
    the application 30 seconds ahead (two machines, a Docker VM) a new lot looked like it had no location."""
    import kisaniti_api.org.routes as routes

    class Ahead(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> Any:
            return super().now(tz) + timedelta(seconds=30)

    monkeypatch.setattr(routes, "datetime", Ahead)
    a, _ = two
    m, l1, l2 = member(a), location(a, "Shed 1"), location(a, "Shed 2")
    t = lot(a, m, commodity, location_id=l1["id"])
    assert call(a.admin, "GET", f"/lots/{t['id']}").json()["current_location_id"] == l1["id"]
    assert call(a.admin, "POST", f"/lots/{t['id']}/placements", {"location_id": l2["id"]}).status_code == 201
    assert call(a.admin, "GET", f"/lots/{t['id']}").json()["current_location_id"] == l2["id"]


# ---- placements ------------------------------------------------------------------------------------------------------
def test_placement_history_is_append_only_and_the_current_location_is_the_latest(
    two: tuple[Fpo, Fpo], commodity: str
) -> None:
    a, _ = two
    m, l1, l2 = member(a), location(a, "Shed 1"), location(a, "Shed 2")
    t = lot(a, m, commodity, location_id=l1["id"])
    now = datetime.now(UTC)
    r = call(
        a.admin,
        "POST",
        f"/lots/{t['id']}/placements",
        {"location_id": l2["id"], "placed_at": iso(now + timedelta(seconds=1))},
    )
    assert r.status_code == 201
    valid("placement", r.json())
    assert (
        call(
            a.admin,
            "POST",
            f"/lots/{t['id']}/placements",
            {"location_id": l2["id"], "placed_at": r.json()["placed_at"]},
        ).status_code
        == 409
    )
    assert (
        call(
            a.admin,
            "POST",
            f"/lots/{t['id']}/placements",
            {"location_id": l1["id"], "placed_at": iso(now + timedelta(hours=1))},
        ).status_code
        == 422
    )
    hist = call(a.admin, "GET", f"/lots/{t['id']}/placements").json()
    assert [h["location_id"] for h in hist] == [l2["id"], l1["id"]]
    assert call(a.admin, "PUT", f"/lots/{t['id']}/placements", {}).status_code == 405


# ---- the ledger: exact arithmetic and refusals ---------------------------------------------------------------------
def test_adjustments_change_the_balance_exactly_and_refuse_to_go_below_zero(
    engine: Engine, two: tuple[Fpo, Fpo], commodity: str
) -> None:
    a, _ = two
    t = lot(a, member(a), commodity, "1000.000")
    r = call(
        a.admin, "POST", f"/lots/{t['id']}/adjustments", {"quantity_delta_kg": "-12.500", "reason_code": "SPILLAGE"}
    )
    assert r.status_code == 201
    valid("stock_event", r.json())
    assert balance(a, t["id"]) == {
        "on_hand_kg": "987.500",
        "reserved_kg": "0.000",
        "available_kg": "987.500",
        "sold_kg": "0.000",
    }
    assert (
        call(
            a.admin,
            "POST",
            f"/lots/{t['id']}/adjustments",
            {"quantity_delta_kg": "0.001", "reason_code": "WEIGHING_CORRECTION"},
        ).status_code
        == 201
    )
    assert balance(a, t["id"])["on_hand_kg"] == "987.501"  # no float drift, to the gram
    before = event_count(engine, t["id"])
    over = call(
        a.admin, "POST", f"/lots/{t['id']}/adjustments", {"quantity_delta_kg": "-987.502", "reason_code": "OTHER"}
    )
    assert over.status_code == 409 and over.json()["issues"][0]["code"] == "STOCK_INVARIANT"
    assert event_count(engine, t["id"]) == before  # a refused event writes nothing
    assert (
        call(
            a.admin, "POST", f"/lots/{t['id']}/adjustments", {"quantity_delta_kg": "-987.501", "reason_code": "OTHER"}
        ).status_code
        == 201
    )
    assert balance(a, t["id"])["on_hand_kg"] == "0.000"  # exactly zero is allowed
    assert (
        call(
            a.admin, "POST", f"/lots/{t['id']}/adjustments", {"quantity_delta_kg": "-0.001", "reason_code": "OTHER"}
        ).status_code
        == 409
    )


@pytest.mark.parametrize(
    "body",
    [
        {"quantity_delta_kg": "0.000", "reason_code": "OTHER"},
        {"quantity_delta_kg": "-1.0001", "reason_code": "OTHER"},
        {"quantity_delta_kg": "-1.000"},
        {"quantity_delta_kg": "-1.000", "reason_code": "FUN"},
        {"quantity_delta_kg": -1.0, "reason_code": "OTHER"},
    ],
)
def test_bad_adjustments_are_rejected_before_the_ledger(
    engine: Engine, two: tuple[Fpo, Fpo], commodity: str, body: dict[str, Any]
) -> None:
    a, _ = two
    t = lot(a, member(a), commodity)
    assert call(a.admin, "POST", f"/lots/{t['id']}/adjustments", body).status_code == 422
    assert event_count(engine, t["id"]) == 1


def test_idempotency_key_replays_the_same_result_and_rejects_a_different_request(
    engine: Engine, two: tuple[Fpo, Fpo], commodity: str
) -> None:
    a, _ = two
    t = lot(a, member(a), commodity)
    key = uuid.uuid4().hex
    body = {"quantity_delta_kg": "-10.000", "reason_code": "SPILLAGE"}
    first = call(a.admin, "POST", f"/lots/{t['id']}/adjustments", body, key)
    again = call(a.admin, "POST", f"/lots/{t['id']}/adjustments", body, key)
    assert first.status_code == again.status_code == 201 and first.json() == again.json()
    assert balance(a, t["id"])["on_hand_kg"] == "990.000"  # applied once
    other = call(a.admin, "POST", f"/lots/{t['id']}/adjustments", {**body, "quantity_delta_kg": "-11.000"}, key)
    assert other.status_code == 409 and other.json()["code"] == "IDEMPOTENCY_KEY_REUSED"
    assert call(a.admin, "POST", f"/lots/{t['id']}/adjustments", body, "short").status_code == 422


def test_creating_a_lot_twice_with_the_same_key_makes_one_lot(two: tuple[Fpo, Fpo], commodity: str) -> None:
    a, _ = two
    m, key = member(a), uuid.uuid4().hex
    body = {"member_id": m["id"], "commodity_id": commodity, "registered_quantity_kg": "500.000"}
    r1, r2 = call(a.admin, "POST", "/lots", body, key), call(a.admin, "POST", "/lots", body, key)
    assert r1.status_code == r2.status_code == 201 and r1.json()["id"] == r2.json()["id"]
    assert len(call(a.admin, "GET", "/lots").json()) == 1
    assert call(a.admin, "POST", "/lots", {**body, "registered_quantity_kg": "501.000"}, key).status_code == 409


def test_twelve_simultaneous_adjustments_never_overdraw_a_lot(
    engine: Engine, two: tuple[Fpo, Fpo], commodity: str
) -> None:
    a, _ = two
    t = lot(a, member(a), commodity, "1000.000")
    sessions = [Session(a.admin_name) for _ in range(12)]
    codes: list[int] = []
    gate, lock = threading.Barrier(12), threading.Lock()

    def worker(s: Session) -> None:
        gate.wait()
        r = call(s, "POST", f"/lots/{t['id']}/adjustments", {"quantity_delta_kg": "-100.000", "reason_code": "OTHER"})
        with lock:
            codes.append(r.status_code)

    threads = [threading.Thread(target=worker, args=(s,)) for s in sessions]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert sorted(codes) == [201] * 10 + [409] * 2
    assert balance(a, t["id"])["on_hand_kg"] == "0.000" and event_count(engine, t["id"]) == 11


def test_simultaneous_reservations_cannot_reserve_the_same_stock_twice(
    engine: Engine, two: tuple[Fpo, Fpo], commodity: str
) -> None:
    """The database guards what slice 3's approval will write: five approvals of 400 kg on a 1000 kg lot."""
    a, _ = two
    t = lot(a, member(a), commodity, "1000.000")
    results: list[str] = []
    gate, lock = threading.Barrier(5), threading.Lock()

    def worker() -> None:
        gate.wait()
        try:
            sql_event(engine, a.fpo_id, t["id"], "RESERVED", 400_000)
            outcome = "ok"
        except DBAPIError as e:
            outcome = "refused" if "stock invariant" in str(e) else f"other: {e}"
        with lock:
            results.append(outcome)

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert sorted(results) == ["ok", "ok", "refused", "refused", "refused"]
    assert balance(a, t["id"]) == {
        "on_hand_kg": "1000.000",
        "reserved_kg": "800.000",
        "available_kg": "200.000",
        "sold_kg": "0.000",
    }


def test_ledger_rules_hold_for_every_kind_of_event(engine: Engine, two: tuple[Fpo, Fpo], commodity: str) -> None:
    a, _ = two
    t = lot(a, member(a), commodity, "100.000")
    refused = [
        ("REGISTERED", 1),
        ("SOLD", 100_001),
        ("RESERVED", 100_001),
        ("RESERVATION_RELEASED", 1),
        ("RESERVATION_CONSUMED", 1),
    ]
    for kind, grams in refused:
        with pytest.raises(DBAPIError, match="stock invariant"):
            sql_event(engine, a.fpo_id, t["id"], kind, grams)
    sql_event(engine, a.fpo_id, t["id"], "RESERVED", 60_000)
    assert balance(a, t["id"])["available_kg"] == "40.000"
    with pytest.raises(DBAPIError, match="stock invariant"):
        sql_event(
            engine, a.fpo_id, t["id"], "SOLD", 100_000
        )  # 100 kg on hand but 60 kg are reserved: sale of all is not available stock
    sql_event(engine, a.fpo_id, t["id"], "RESERVATION_RELEASED", 10_000)
    assert balance(a, t["id"]) == {
        "on_hand_kg": "100.000",
        "reserved_kg": "50.000",
        "available_kg": "50.000",
        "sold_kg": "0.000",
    }
    with engine.begin() as c:  # a sale that uses a reservation writes both events in one transaction
        c.execute(text("SET LOCAL ROLE kisaniti_app"))
        c.execute(text("SELECT set_config('app.fpo_id', :f, true)"), {"f": a.fpo_id})
        for kind in ("RESERVATION_CONSUMED", "SOLD"):
            c.execute(
                text("INSERT INTO stock_events (lot_id, fpo_id, kind, quantity_g) VALUES (:l, :f, :k, 50000)"),
                {"l": t["id"], "f": a.fpo_id, "k": kind},
            )
    assert balance(a, t["id"]) == {
        "on_hand_kg": "50.000",
        "reserved_kg": "0.000",
        "available_kg": "50.000",
        "sold_kg": "50.000",
    }


def test_the_first_event_of_a_lot_must_be_registered(engine: Engine, two: tuple[Fpo, Fpo], commodity: str) -> None:
    a, _ = two
    m = member(a)
    lid = new_id("lot")
    with engine.begin() as c:
        c.execute(
            text(
                "INSERT INTO lots (id, fpo_id, member_id, commodity_id, registered_grams, origin) "
                "VALUES (:l, :f, :m, :c, 1000, 'SYNTHETIC')"
            ),
            {"l": lid, "f": a.fpo_id, "m": m["id"], "c": commodity},
        )
    with pytest.raises(DBAPIError, match="first event of a lot must be REGISTERED"):
        sql_event(engine, a.fpo_id, lid, "ADJUSTED", 5, "OTHER")


def test_the_ledger_cannot_be_edited_or_deleted(engine: Engine, two: tuple[Fpo, Fpo], commodity: str) -> None:
    a, _ = two
    lot(a, member(a), commodity)
    for sql in (
        "UPDATE stock_events SET quantity_g = 1",
        "DELETE FROM stock_events",
        "UPDATE lot_placements SET placed_at = now()",
        "DELETE FROM lot_placements",
    ):
        with pytest.raises(DBAPIError, match="append-only"), engine.begin() as c:
            c.execute(text(sql))
        with pytest.raises(DBAPIError):
            as_app(engine, a.fpo_id, sql)


# ---- two FPOs: nothing crosses ---------------------------------------------------------------------------------------
def test_each_fpo_sees_only_its_own_members_locations_and_lots(two: tuple[Fpo, Fpo], commodity: str) -> None:
    a, b = two
    ma, mb = member(a, "A farmer"), member(b, "B farmer")
    la, lb = location(a, "A shed"), location(b, "B shed")
    ta, tb = lot(a, ma, commodity), lot(b, mb, commodity)
    for path, mine, theirs in (
        ("/members", ma["id"], mb["id"]),
        ("/locations", la["id"], lb["id"]),
        ("/lots", ta["id"], tb["id"]),
    ):
        ids_a = {x["id"] for x in call(a.admin, "GET", path).json()}
        assert mine in ids_a and theirs not in ids_a, path


def test_another_fpos_objects_are_404_like_missing_ones_nothing_is_written_and_it_is_audited(
    engine: Engine, two: tuple[Fpo, Fpo], commodity: str
) -> None:
    a, b = two
    ma, la = member(a), location(a)
    ta = lot(a, ma, commodity, "100.000")
    gone = {"m": new_id("mbr"), "l": new_id("lot"), "s": new_id("stl")}
    probes = [
        ("GET", "/members/{m}", None),
        ("PATCH", "/members/{m}", {"active": False}),
        ("GET", "/locations/{s}", None),
        ("GET", "/lots/{l}", None),
        ("GET", "/lots/{l}/events", None),
        ("GET", "/lots/{l}/placements", None),
        ("POST", "/lots/{l}/adjustments", {"quantity_delta_kg": "-1.000", "reason_code": "OTHER"}),
        ("POST", "/lots/{l}/placements", {"location_id": new_id("stl")}),
    ]
    for method, path, body in probes:
        mine = {"m": ma["id"], "l": ta["id"], "s": la["id"]}
        other = call(b.admin, method, path.format(**mine), body)
        missing = call(b.admin, method, path.format(**gone), body)
        assert other.status_code == missing.status_code == 404, path
        assert strip(other) == strip(missing), path
    assert event_count(engine, ta["id"]) == 1
    assert call(a.admin, "GET", f"/members/{ma['id']}").json()["active"] is True
    assert "CROSS_TENANT_REFERENCE" in audit_events(engine, actor_user_id=b.admin_id)


def test_a_lot_cannot_name_another_fpos_member_or_location(
    engine: Engine, two: tuple[Fpo, Fpo], commodity: str
) -> None:
    a, b = two
    ma, la, mb = member(a), location(a), member(b)
    r1 = call(
        b.admin, "POST", "/lots", {"member_id": ma["id"], "commodity_id": commodity, "registered_quantity_kg": "1.000"}
    )
    r2 = call(
        b.admin,
        "POST",
        "/lots",
        {"member_id": mb["id"], "commodity_id": commodity, "registered_quantity_kg": "1.000", "location_id": la["id"]},
    )
    assert r1.status_code == r2.status_code == 404
    assert call(b.admin, "GET", "/lots").json() == []
    with pytest.raises(IntegrityError), engine.begin() as c:  # even the owner cannot link across FPOs: composite key
        c.execute(
            text(
                "INSERT INTO lots (id, fpo_id, member_id, commodity_id, registered_grams, origin) "
                "VALUES (:l, :f, :m, :c, 1, 'SYNTHETIC')"
            ),
            {"l": new_id("lot"), "f": b.fpo_id, "m": ma["id"], "c": commodity},
        )


def test_restricted_role_sees_only_its_own_org_rows(engine: Engine, two: tuple[Fpo, Fpo], commodity: str) -> None:
    a, b = two
    for f in (a, b):
        lot(f, member(f), commodity, location_id=location(f)["id"])
    for table in ("members", "locations", "lots", "lot_placements", "stock_events", "lot_balances"):
        assert as_app(engine, None, f"SELECT 1 FROM {table}") == [], table
        assert {r[0] for r in as_app(engine, a.fpo_id, f"SELECT DISTINCT fpo_id FROM {table}")} == {a.fpo_id}, table


# ---- roles -------------------------------------------------------------------------------------------------------
def test_viewers_read_staff_write_and_nobody_without_a_login_does_anything(
    two: tuple[Fpo, Fpo], commodity: str
) -> None:
    a, _ = two
    m = member(a)
    _, _, viewer = add_user(a, "VIEWER")
    _, _, staff = add_user(a, "STAFF")
    assert call(viewer, "GET", "/members").status_code == 200 and call(viewer, "GET", "/lots").status_code == 200
    assert call(viewer, "POST", "/members", {"display_name": "x", "preferred_language": "en"}).status_code == 403
    assert (
        call(
            viewer,
            "POST",
            "/lots",
            {"member_id": m["id"], "commodity_id": commodity, "registered_quantity_kg": "1.000"},
        ).status_code
        == 403
    )
    assert (
        call(
            staff, "POST", "/members", {"display_name": "Staff-made (fictional)", "preferred_language": "en"}
        ).status_code
        == 201
    )
    from fastapi.testclient import TestClient

    from kisaniti_api.main import app

    anon = TestClient(app, raise_server_exceptions=False)
    assert anon.get("/api/v1/members").status_code == 401 and anon.get("/api/v1/lots").status_code == 401

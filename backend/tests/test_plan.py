# ruff: noqa: F811, F401  (fixtures imported from test_access by name: pytest uses them as arguments)
"""Plan slice (contract plan/v1) on real PostgreSQL: forecasts, sensor links and plan proposals.

The price releases built here are TEST FIXTURES with invented numbers and fictional mandis; nothing real is imported.
Expected values are worked out in the tests from the fixture's construction, never by calling the code under test
(the construction: every mandi's price is the day's base price plus a fixed offset, so the index equals the base and
each mandi's basis equals its offset exactly).
"""

from __future__ import annotations

import dataclasses
import json
import random
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from kisaniti_core.allocation import evaluate, parse_input, plan_from_json, solve, solve_oracle, validate_plan
from referencing import Registry, Resource
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

from kisaniti_api.main import app
from kisaniti_api.market import import_release as imp
from kisaniti_api.plan import builder
from tests.test_access import Fpo, access_env, add_user, as_app, audit_events, clean_up_access_data, two
from tests.test_devices import dev_id, register
from tests.test_market import make_release, row
from tests.test_org import call, location, lot, member

CONTRACTS = Path(__file__).resolve().parents[2] / "contracts"
ALPHA, BETA, GAMMA = "Alpha APMC", "Beta APMC", "Gamma APMC"
OFFSETS = {ALPHA: 50, BETA: -30, GAMMA: 0}
EXPLANATION_CODES = set(
    json.loads((CONTRACTS / "plan" / "v1" / "common.schema.json").read_text())["$defs"]["explanation_code"]["enum"]
)
WARNING_CODES = set(
    json.loads((CONTRACTS / "plan" / "v1" / "common.schema.json").read_text())["$defs"]["warning_code"]["enum"]
)


def valid(slice_name: str, name: str, doc: Any) -> None:
    reg: Registry = Registry()
    for d in (CONTRACTS / slice_name / "v1", CONTRACTS / "shared" / "v1"):
        for f in d.glob("*.schema.json"):
            body = json.loads(f.read_text())
            reg = reg.with_resource(body["$id"], Resource.from_contents(body))
    sid = json.loads((CONTRACTS / slice_name / "v1" / f"{name}.schema.json").read_text())["$id"]
    errors = [e.message for e in Draft202012Validator({"$ref": sid}, registry=reg).iter_errors(doc)]
    assert errors == [], errors


# ---- fixture data ----
class Counter:
    n = 0


def rows_for(
    start: date, bases: list[int], variety: str = "1121", mandis: tuple[str, ...] = (ALPHA, BETA, GAMMA)
) -> list[dict[str, str]]:
    """One price row per mandi per day: the day's base price plus the mandi's fixed offset."""
    rows = []
    for i, base in enumerate(bases):
        for m in mandis:
            Counter.n += 1
            rows.append(row(Counter.n, m, variety, (start + timedelta(days=i)).isoformat(), f"{base + OFFSETS[m]}.00"))
    return rows


def season_rows(
    start: date, days: int, seed: int, variety: str = "1121", mandis: tuple[str, ...] = (ALPHA, BETA, GAMMA)
) -> tuple[list[dict[str, str]], list[int]]:
    """A random-walk of base prices (whole rupees) and the rows built from it."""
    rnd = random.Random(seed)
    base, bases = rnd.randint(3000, 4000), []
    for _ in range(days):
        base += rnd.randint(-30, 32)
        bases.append(base)
    return rows_for(start, bases, variety, mandis), bases


def standard_rows() -> tuple[list[dict[str, str]], list[int]]:
    rows: list[dict[str, str]] = []
    for start, days, seed in ((date(2023, 9, 10), 80, 1), (date(2024, 9, 10), 80, 2), (date(2025, 9, 10), 60, 3)):
        r, bases = season_rows(start, days, seed)
        rows += r
    short, _ = season_rows(date(2025, 9, 10), 25, 4, variety="Basmati 1509")
    return rows + short, bases  # bases of the newest 1121 season


@pytest.fixture(autouse=True)
def fresh_market(engine: Engine) -> Any:
    sql = (
        "TRUNCATE plans, price_observations, dataset_status_log, dataset_versions, msp_entries, mandis, "
        "commodities CASCADE"
    )
    with engine.begin() as c:
        c.execute(text(sql))
    yield
    with engine.begin() as c:
        c.execute(text(sql))


def import_rows(engine: Engine, tmp_path: Path, rows: list[dict[str, str]]) -> None:
    imp.import_release(engine, make_release(tmp_path, obs=rows))


@pytest.fixture()
def market(engine: Engine, tmp_path: Path) -> dict[str, Any]:
    rows, bases = standard_rows()
    import_rows(engine, tmp_path, rows)
    with engine.connect() as c:
        ids = {r.name: r.id for r in c.execute(text("SELECT id, name FROM mandis"))}
        cmd = str(c.execute(text("SELECT id FROM commodities")).scalar_one())
    return {"bases": bases, "mandi": ids, "commodity": cmd, "origin": date(2025, 9, 10) + timedelta(days=59)}


def req(market: dict[str, Any], mandis: dict[str, str] | None = None, **over: Any) -> dict[str, Any]:
    chosen = mandis or {ALPHA: "35.00", BETA: "0.00"}
    body = {
        "variety": "1121",
        "horizons_days": [1, 7],
        "mandis": [{"mandi_id": market["mandi"][n], "transport_inr_per_quintal": t} for n, t in chosen.items()],
        "increment_kg": "100.000",
    }
    body.update(over)
    return body


def stock(
    f: Fpo, market: dict[str, Any], kg: str = "1250.000", variety: str | None = "1121", who: str = "G"
) -> dict[str, Any]:
    m = member(f, f"{who} (fictional)")
    extra = {} if variety is None else {"source_variety": variety}
    return lot(f, m, market["commodity"], kg, **extra)


# ---- the forecast endpoint ----
def test_forecast_set_is_exact_and_says_what_it_is(two: tuple[Fpo, Fpo], market: dict[str, Any]) -> None:
    a, _ = two
    r = call(a.admin, "GET", "/forecasts?variety=1121")
    assert r.status_code == 200, r.text
    fs = r.json()
    valid("plan", "forecast_set", fs)
    assert fs["label"] == "Baseline forecast, not validated on held-out data"
    assert fs["signal_verdict"] == "NOT_ASSESSED" and fs["calibration"] == "NOT_VERIFIED"
    assert fs["dataset"]["validation_status"] == "PENDING_HAND_CHECK"
    assert fs["origin_date"] == market["origin"].isoformat() and fs["season"] == "2025-26"
    last = market["bases"][-1]
    assert fs["series_tail"][-1] == {
        "date": market["origin"].isoformat(),
        "index_inr_per_quintal": f"{last}.000",
        "n_mandis": 3,
    }
    naive = next(m for m in fs["methods"] if m["method"] == "NAIVE")["horizons"]
    assert [h["h"] for h in naive] == [1, 7, 14]
    assert all(h["point_inr_per_quintal"] == f"{last}.00" for h in naive)  # naive repeats the last index value
    assert [h["target_date"] for h in naive] == [(market["origin"] + timedelta(days=h)).isoformat() for h in (1, 7, 14)]
    ma = next(m for m in fs["methods"] if m["method"] == "MOVING_AVERAGE_5")["horizons"]
    expect = Decimal(sum(market["bases"][-5:])) / 5
    assert Decimal(ma[0]["point_inr_per_quintal"]) == expect.quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)
    for h in naive:
        assert h["status"] == "RANGE_AVAILABLE" and h["residual_pairs"] >= 40
        lo80, hi80 = (Decimal(h["range80"][k]) for k in ("low_inr_per_quintal", "high_inr_per_quintal"))
        lo95, hi95 = (Decimal(h["range95"][k]) for k in ("low_inr_per_quintal", "high_inr_per_quintal"))
        assert lo95 <= lo80 <= Decimal(h["point_inr_per_quintal"]) <= hi80 <= hi95


def test_candidate_mandis_carry_the_basis_built_into_the_fixture(two: tuple[Fpo, Fpo], market: dict[str, Any]) -> None:
    a, _ = two
    cands = {c["name"]: c for c in call(a.admin, "GET", "/forecasts?variety=1121").json()["candidate_mandis"]}
    for name, off in OFFSETS.items():
        assert cands[name]["status"] == "ELIGIBLE" and cands[name]["days_used"] == 30
        assert cands[name]["basis_inr_per_quintal"] == f"{off}.00"


def test_a_short_history_gives_a_point_without_a_range_and_too_short_gives_nothing(
    two: tuple[Fpo, Fpo], market: dict[str, Any], engine: Engine, tmp_path: Path
) -> None:
    a, _ = two
    fs = call(a.admin, "GET", "/forecasts?variety=Basmati%201509").json()
    valid("plan", "forecast_set", fs)
    naive = next(m for m in fs["methods"] if m["method"] == "NAIVE")["horizons"]
    assert all(h["status"] == "TOO_FEW_PAIRS" and h["range80"] is None and h["range95"] is None for h in naive)
    assert all(h["residual_pairs"] < 40 for h in naive)
    rows, _ = season_rows(date(2025, 9, 10), 9, 5, variety="1121")  # nine index days: below the minimum of ten
    with engine.begin() as c:
        c.execute(
            text(
                "TRUNCATE plans, price_observations, dataset_status_log, dataset_versions, mandis, commodities CASCADE"
            )
        )
    import_rows(engine, tmp_path, rows)
    r = call(a.admin, "GET", "/forecasts?variety=1121")
    assert r.status_code == 422 and r.json()["issues"][0]["code"] == "INSUFFICIENT_HISTORY"


def test_forecast_refusals(two: tuple[Fpo, Fpo]) -> None:
    a, _ = two
    r = call(a.admin, "GET", "/forecasts?variety=Pusa%201718")
    assert r.status_code == 422 and r.json()["issues"][0]["code"] == "VARIETY_NOT_FORECAST"
    r = call(a.admin, "GET", "/forecasts?variety=1121")  # no dataset imported
    assert r.status_code == 404 and r.json()["issues"][0]["code"] == "NO_PRICE_DATA"
    assert TestClient(app).get("/api/v1/forecasts?variety=1121").status_code == 401  # not signed in


def test_a_rejected_dataset_gives_no_forecast(two: tuple[Fpo, Fpo], market: dict[str, Any], engine: Engine) -> None:
    a, _ = two
    with engine.begin() as c:
        name = c.execute(text("SELECT name FROM dataset_versions")).scalar_one()
    imp.set_status(engine, name, "REJECTED", "test: the hand check failed")
    r = call(a.admin, "GET", "/forecasts?variety=1121")
    assert r.status_code == 404 and r.json()["issues"][0]["code"] == "NO_PRICE_DATA"


# ---- sensor links ----
def test_link_unlink_and_history(two: tuple[Fpo, Fpo], market: dict[str, Any]) -> None:
    a, _ = two
    lt, d1, d2 = stock(a, market), dev_id(), dev_id()
    assert register(a, d1).status_code == 201 and register(a, d2).status_code == 201
    assert call(a.admin, "GET", "/sensor-links/current").json() == []
    r = call(a.admin, "POST", f"/lots/{lt['id']}/sensor-links", {"device_id": d1})
    assert r.status_code == 201
    valid("plan", "sensor_link", r.json())
    assert call(a.admin, "POST", f"/lots/{lt['id']}/sensor-links", {"device_id": d2}).status_code == 201
    cur = call(a.admin, "GET", "/sensor-links/current").json()
    assert [(c["lot_id"], c["device_id"]) for c in cur] == [(lt["id"], d2)]  # the latest link wins
    assert call(a.admin, "POST", f"/lots/{lt['id']}/sensor-links", {"device_id": None}).status_code == 201
    assert call(a.admin, "GET", "/sensor-links/current").json() == []  # unlinked: not listed
    hist = call(a.admin, "GET", f"/lots/{lt['id']}/sensor-links").json()
    assert [h["device_id"] for h in hist] == [None, d2, d1]
    for h in hist:
        valid("plan", "sensor_link", h)


def test_links_are_tenant_scoped_append_only_and_need_write_access(
    two: tuple[Fpo, Fpo], market: dict[str, Any], engine: Engine
) -> None:
    a, b = two
    lt, da, db = stock(a, market), dev_id(), dev_id()
    register(a, da)
    register(b, db)
    assert call(b.admin, "POST", f"/lots/{lt['id']}/sensor-links", {"device_id": db}).status_code == 404  # A's lot
    missing = call(b.admin, "POST", f"/lots/lot_{'0' * 26}/sensor-links", {"device_id": db})
    own = call(b.admin, "POST", f"/lots/{lt['id']}/sensor-links", {"device_id": db})
    assert missing.json()["detail"].replace("lot", "") == own.json()["detail"].replace("lot", "")  # same answer
    assert call(a.admin, "POST", f"/lots/{lt['id']}/sensor-links", {"device_id": db}).status_code == 404  # B's device
    assert call(b.admin, "GET", f"/lots/{lt['id']}/sensor-links").status_code == 404
    assert "CROSS_TENANT_REFERENCE" in audit_events(engine, actor_user_id=b.admin_id)
    assert (
        call(a.admin, "POST", f"/lots/{lt['id']}/sensor-links", {"device_id": da, "fpo_id": a.fpo_id}).status_code
        == 422
    )
    assert call(a.admin, "POST", f"/lots/{lt['id']}/sensor-links", {}).status_code == 422
    call(a.admin, "POST", f"/lots/{lt['id']}/sensor-links", {"device_id": da})
    assert as_app(engine, None, "SELECT 1 FROM sensor_links") == []
    assert as_app(engine, b.fpo_id, "SELECT 1 FROM sensor_links") == []
    assert len(as_app(engine, a.fpo_id, "SELECT 1 FROM sensor_links")) == 1
    for sql in ("UPDATE sensor_links SET device_id = NULL", "DELETE FROM sensor_links"):
        with pytest.raises(DBAPIError):
            as_app(engine, a.fpo_id, sql)
        with pytest.raises(DBAPIError), engine.begin() as c:
            c.execute(text(sql))
    _, _, viewer = add_user(a, "VIEWER")
    assert viewer.post(f"/lots/{lt['id']}/sensor-links", {"device_id": da}).status_code == 403
    assert viewer.get("/sensor-links/current").status_code == 200
    assert call(a.admin, "POST", f"/devices/{da}/deactivate").status_code == 204
    assert call(a.admin, "POST", f"/lots/{lt['id']}/sensor-links", {"device_id": da}).status_code == 409


# ---- a plan ----
def net_by_line(doc: dict[str, Any]) -> Decimal:
    """Expected net return, recomputed from the stored input and assignments: per line kg x (price - cost) per kg,
    rounded half-even to the paisa, then summed (the shared/v1 money rule)."""
    prices = {
        (c["lot_id"], c["mandi_id"], c["date"]): (Decimal(c["price_inr_per_kg"]), Decimal(c["cost_inr_per_kg"]))
        for c in doc["allocation_input"]["choices"]
    }
    total = Decimal(0)
    for a in doc["allocation_result"]["assignments"]:
        price, cost = prices[(a["lot_id"], a["mandi_id"], a["date"])]
        total += (Decimal(a["quantity_kg"]) * (price - cost)).quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)
    return total


def test_a_plan_is_proposed_valid_exact_and_does_not_touch_stock(two: tuple[Fpo, Fpo], market: dict[str, Any]) -> None:
    a, _ = two
    l1, l2 = stock(a, market, "1250.000", who="G"), stock(a, market, "800.000", who="H")
    r = call(a.admin, "POST", "/plans", req(market))
    assert r.status_code == 201, r.text
    doc = r.json()
    valid("plan", "plan_proposal", doc)
    valid("allocation", "allocation_input", doc["allocation_input"])
    valid("allocation", "allocation_result", doc["allocation_result"])
    valid("allocation", "plan_validation", doc["validation"])
    assert doc["status"] == "PROPOSED" and doc["not_proposed_reasons"] == [] and doc["fpo_id"] == a.fpo_id
    assert doc["origin"] == "SYNTHETIC" and doc["allocation_input"]["mode"] == "DEMO"
    res = doc["allocation_result"]
    assert res["status"]["optimality"] == "PROVEN_OPTIMAL" and res["status"]["allocation_outcome"] == "PARTIAL"
    assert res["validation"]["valid"] is True and doc["validation"]["valid"] is True
    # 1250 -> 1200 and 800 -> 800 planned; the 50 kg remainder of the first lot stays unallocated
    assert {x["lot_id"]: x["scheduled_kg"] for x in res["lots"]} == {l1["id"]: "1200.000", l2["id"]: "800.000"}
    assert res["totals"]["scheduled_kg"] == "2000.000" and res["totals"]["participating_kg"] == "2050.000"
    # prices: the forecast point + the mandi's basis, per kg; transport entered per quintal
    last = market["bases"][-1]
    by_mandi = {m: Decimal(last + OFFSETS[n]) / 100 for n, m in market["mandi"].items()}
    for c in doc["allocation_input"]["choices"]:
        assert Decimal(c["price_inr_per_kg"]) == by_mandi[c["mandi_id"]]
        assert Decimal(c["cost_inr_per_kg"]) == (Decimal("0.35") if c["mandi_id"] == market["mandi"][ALPHA] else 0)
    # Alpha nets 50 - 35 = +15 per quintal over Beta's -30: everything goes to Alpha, on the first date (tie-break)
    assert {x["mandi_id"] for x in res["assignments"]} == {market["mandi"][ALPHA]}
    assert {x["date"] for x in res["assignments"]} == {(market["origin"] + timedelta(days=1)).isoformat()}
    assert Decimal(res["totals"]["expected_net_return_inr"]) == net_by_line(doc)
    assert doc["allocation_input_sha256"] == builder._sha(doc["allocation_input"])
    # a proposal reserves nothing
    for lt in (l1, l2):
        assert call(a.admin, "GET", f"/lots/{lt['id']}").json()["balance"] == {
            "on_hand_kg": lt["registered_quantity_kg"],
            "reserved_kg": "0.000",
            "available_kg": lt["registered_quantity_kg"],
            "sold_kg": "0.000",
        }
    got = call(a.admin, "GET", f"/plans/{doc['id']}")
    assert got.status_code == 200 and got.json() == doc
    listed = call(a.admin, "GET", "/plans").json()
    assert [s["id"] for s in listed] == [doc["id"]]
    valid("plan", "plan_summary", listed[0])
    assert listed[0]["allocated_kg"] == "2000.000" and listed[0]["farmers"] == 2 and listed[0]["lots"] == 2


def test_codes_only_and_the_warnings_a_manager_needs(two: tuple[Fpo, Fpo], market: dict[str, Any]) -> None:
    a, _ = two
    stock(a, market)
    doc = call(a.admin, "POST", "/plans", req(market)).json()
    for item in doc["explanation"]:
        assert item["code"] in EXPLANATION_CODES
        assert all(isinstance(v, str) for v in item["params"].values())
    for w in doc["warnings"]:
        assert w["code"] in WARNING_CODES
    codes = {w["code"] for w in doc["warnings"]}
    assert {
        "PRICE_DATASET_UNCHECKED",
        "PRICE_DATA_OLDER_THAN_TODAY",
        "STORAGE_ALL_UNKNOWN",
        "SYNTHETIC_DEMO_DATA",
    } <= codes
    assert "PRICE_SAME_ON_ALL_DATES" in codes and "NOT_ALL_STOCK_ALLOCATED" in codes
    ex = {i["code"] for i in doc["explanation"]}
    assert {
        "FORECAST_METHOD",
        "FORECAST_NOT_VALIDATED",
        "WAITING_COST_NOT_MODELLED",
        "TRANSPORT_AS_ENTERED",
        "OUTCOME",
    } <= ex
    age = next(w for w in doc["warnings"] if w["code"] == "PRICE_DATA_OLDER_THAN_TODAY")["params"]
    assert int(age["days"]) == (datetime.now(ZoneInfo("Asia/Kolkata")).date() - market["origin"]).days
    transport = {
        i["params"]["mandi_id"]: i["params"]["inr_per_quintal"]
        for i in doc["explanation"]
        if i["code"] == "TRANSPORT_AS_ENTERED"
    }
    assert transport == {
        market["mandi"][ALPHA]: "35.00",
        market["mandi"][BETA]: "0.00",
    }  # shown as entered, 0.00 included


def test_the_proposal_equals_the_exact_oracle_on_a_small_plan(two: tuple[Fpo, Fpo], market: dict[str, Any]) -> None:
    a, _ = two
    stock(a, market, "300.000", who="G")
    stock(a, market, "200.000", who="H")
    doc = call(
        a.admin, "POST", "/plans", req(market, {ALPHA: "35.00"}, horizons_days=[1, 7], increment_kg="100.000")
    ).json()
    inp = parse_input(doc["allocation_input"])
    oracle = solve_oracle(inp)
    stored = plan_from_json(doc["allocation_result"]["assignments"])
    assert validate_plan(inp, stored).valid
    assert evaluate(inp, stored).key()[:4] == oracle.best.key()[:4]  # same allocation, fairness, storage and net return


def test_the_same_inputs_give_the_same_plan(two: tuple[Fpo, Fpo], market: dict[str, Any]) -> None:
    a, _ = two
    stock(a, market, "1250.000", who="G")
    stock(a, market, "800.000", who="H")
    d1 = call(a.admin, "POST", "/plans", req(market)).json()
    d2 = call(a.admin, "POST", "/plans", req(market)).json()
    assert d1["id"] != d2["id"]
    assert d1["allocation_result"]["assignments"] == d2["allocation_result"]["assignments"]
    assert d1["allocation_result"]["totals"] == d2["allocation_result"]["totals"]
    assert [m["basis_inr_per_quintal"] for m in d1["mandis"]] == [m["basis_inr_per_quintal"] for m in d2["mandis"]]


def test_lots_that_cannot_be_planned_are_listed_with_their_reason(two: tuple[Fpo, Fpo], market: dict[str, Any]) -> None:
    a, _ = two
    ok = stock(a, market, "500.000", who="G")
    other = stock(a, market, "500.000", variety="Basmati 1509", who="H")
    nolabel = stock(a, market, "500.000", variety=None, who="I")
    small = stock(a, market, "60.000", who="J")
    doc = call(a.admin, "POST", "/plans", req(market)).json()
    reasons = {r["lot_id"]: r["excluded_reason"] for r in doc["lots"]}
    assert reasons == {
        ok["id"]: None,
        other["id"]: "VARIETY_NOT_MATCHING",
        nolabel["id"]: "VARIETY_NOT_MATCHING",
        small["id"]: "LESS_THAN_ONE_INCREMENT",
    }
    assert [x["id"] for x in doc["allocation_input"]["lots"]] == [ok["id"]]
    ex = {(i["params"]["lot_id"], i["params"]["reason"]) for i in doc["explanation"] if i["code"] == "LOT_EXCLUDED"}
    assert (other["id"], "VARIETY_NOT_MATCHING") in ex and (small["id"], "LESS_THAN_ONE_INCREMENT") in ex


def test_reserved_stock_is_not_planned(two: tuple[Fpo, Fpo], market: dict[str, Any], engine: Engine) -> None:
    a, _ = two
    lt = stock(a, market, "1000.000")
    from tests.test_org import sql_event

    sql_event(engine, a.fpo_id, lt["id"], "RESERVED", 700_000)  # 700 kg reserved elsewhere: 300 kg available
    doc = call(a.admin, "POST", "/plans", req(market)).json()
    assert doc["lots"][0]["available_kg"] == "300.000" and doc["lots"][0]["planned_kg"] == "300.000"
    assert doc["allocation_result"]["totals"]["scheduled_kg"] == "300.000"


# ---- storage from the linked sensor ----
def add_assessment(
    engine: Engine, f: Fpo, device: str, category: str, age: timedelta, reasons: list[str] | None = None
) -> None:
    at = datetime.now(UTC) - age
    with engine.begin() as c:
        c.execute(
            text(
                "INSERT INTO storage_assessments (device_id, fpo_id, assessed_at, category, doc) "
                "VALUES (:d, :f, :t, :c, CAST(:doc AS jsonb))"
            ),
            {"d": device, "f": f.fpo_id, "t": at, "c": category, "doc": json.dumps({"unknown_reasons": reasons or []})},
        )


def test_storage_category_comes_only_from_a_fresh_assessment_of_the_linked_sensor(
    two: tuple[Fpo, Fpo], market: dict[str, Any], engine: Engine
) -> None:
    a, _ = two
    lots = {k: stock(a, market, "500.000", who=k) for k in ("fresh", "old", "none", "nolink", "unk")}
    devs = {k: dev_id() for k in ("fresh", "old", "none", "unk")}
    for k, d in devs.items():
        register(a, d)
        call(a.admin, "POST", f"/lots/{lots[k]['id']}/sensor-links", {"device_id": d})
    add_assessment(engine, a, devs["fresh"], "HIGH", timedelta(minutes=10))
    add_assessment(engine, a, devs["old"], "HIGH", timedelta(hours=3))
    add_assessment(engine, a, devs["unk"], "UNKNOWN", timedelta(minutes=10), ["SETTLING", "STALE"])
    doc = call(a.admin, "POST", "/plans", req(market)).json()
    got = {r["lot_id"]: (r["storage"], r["storage_reason"]) for r in doc["lots"]}
    assert got[lots["fresh"]["id"]] == ("HIGH", None)
    assert got[lots["old"]["id"]] == ("UNKNOWN", "ASSESSMENT_OLD")  # three hours old: no priority from stale data
    assert got[lots["none"]["id"]] == ("UNKNOWN", "NO_ASSESSMENT")
    assert got[lots["nolink"]["id"]] == ("UNKNOWN", "NO_SENSOR_LINK")
    assert got[lots["unk"]["id"]] == ("UNKNOWN", "SETTLING")
    stored = {x["id"]: x["storage"] for x in doc["allocation_input"]["lots"]}
    assert (
        stored[lots["fresh"]["id"]] == "HIGH"
        and [v for k, v in stored.items() if k != lots["fresh"]["id"]] == ["UNKNOWN"] * 4
    )
    assert doc["allocation_result"]["storage"]["HIGH"]["scheduled_kg"] == "500.000"
    assert "STORAGE_ALL_UNKNOWN" not in {w["code"] for w in doc["warnings"]}
    assert any(i["code"] == "STORAGE_PRIORITY" and i["params"]["category"] == "HIGH" for i in doc["explanation"])


def test_storage_resolution_boundaries() -> None:
    now = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)
    s = builder.SensorState("kn-x-001", "MEDIUM", (), now - timedelta(hours=2))
    assert builder.resolve_storage(s, now) == ("MEDIUM", None)  # exactly two hours: still fresh
    s_old = dataclasses.replace(s, assessed_at=now - timedelta(hours=2, seconds=1))
    assert builder.resolve_storage(s_old, now) == ("UNKNOWN", "ASSESSMENT_OLD")
    assert builder.resolve_storage(builder.SensorState(None, None, (), None), now) == ("UNKNOWN", "NO_SENSOR_LINK")
    unk = builder.SensorState("kn-x-001", "UNKNOWN", ("LOW_COVERAGE",), now)
    assert builder.resolve_storage(unk, now) == ("UNKNOWN", "LOW_COVERAGE")
    assert builder.resolve_storage(dataclasses.replace(unk, unknown_reasons=()), now) == ("UNKNOWN", "NO_ASSESSMENT")


# ---- refusals ----
@pytest.mark.parametrize(
    ("mutate", "issue"),
    [
        (lambda b: b.update(variety="Pusa 1718"), "VARIETY_NOT_FORECAST"),
        (lambda b: b.update(horizons_days=[30]), "HORIZON_NOT_ALLOWED"),
        (lambda b: b.update(horizons_days=[7, 7]), "HORIZON_NOT_ALLOWED"),
        (lambda b: b.update(horizons_days=[]), "HORIZON_NOT_ALLOWED"),
        (lambda b: b.update(mandis=[]), "NO_MANDI_SELECTED"),
        (lambda b: b["mandis"][0].update(transport_inr_per_quintal="-5.00"), "TRANSPORT_INVALID"),
        (lambda b: b["mandis"][0].update(transport_inr_per_quintal="5.001"), "TRANSPORT_INVALID"),
        (lambda b: b["mandis"][0].update(transport_inr_per_quintal="5"), "TRANSPORT_INVALID"),
        (lambda b: b.update(increment_kg="0.000"), "INCREMENT_INVALID"),
        (lambda b: b.update(increment_kg="100"), "INCREMENT_INVALID"),
        (lambda b: b["mandis"].append(dict(b["mandis"][0])), "MANDI_NOT_ELIGIBLE"),
    ],
)
def test_bad_requests_are_refused_with_their_issue_code(
    two: tuple[Fpo, Fpo], market: dict[str, Any], mutate: Any, issue: str
) -> None:
    a, _ = two
    stock(a, market)
    body = req(market)
    mutate(body)
    r = call(a.admin, "POST", "/plans", body)
    assert r.status_code == 422, r.text
    assert r.json()["issues"][0]["code"] == issue
    assert call(a.admin, "GET", "/plans").json() == []  # nothing stored for a refused request


def test_unknown_fields_unknown_mandi_no_lots_and_viewers(two: tuple[Fpo, Fpo], market: dict[str, Any]) -> None:
    a, _ = two
    body = req(market)
    assert call(a.admin, "POST", "/plans", {**body, "fpo_id": a.fpo_id}).status_code == 422
    r = call(a.admin, "POST", "/plans", body)  # no lots yet
    assert r.status_code == 422 and r.json()["issues"][0]["code"] == "NO_ELIGIBLE_LOTS"
    stock(a, market)
    bad = req(market)
    bad["mandis"][0]["mandi_id"] = "mnd_" + "0" * 26
    r = call(a.admin, "POST", "/plans", bad)
    assert r.status_code == 422 and r.json()["issues"][0]["code"] == "MANDI_NOT_ELIGIBLE"
    _, _, viewer = add_user(a, "VIEWER")
    assert viewer.post("/plans", body).status_code == 403
    assert viewer.get("/plans").status_code == 200


def test_a_mandi_with_too_few_recent_days_is_excluded_and_if_none_remain_the_plan_is_refused(
    two: tuple[Fpo, Fpo], market: dict[str, Any], engine: Engine, tmp_path: Path
) -> None:
    a, _ = two
    # Delta reports on only 3 of the last 30 index days
    rows, _ = season_rows(date(2025, 9, 10), 60, 3)
    extra = [
        row(900_000 + i, "Delta APMC", "1121", (date(2025, 9, 10) + timedelta(days=57 + i)).isoformat(), "3500.00")
        for i in range(3)
    ]
    with engine.begin() as c:
        c.execute(
            text(
                "TRUNCATE plans, price_observations, dataset_status_log, dataset_versions, mandis, commodities CASCADE"
            )
        )
    old, _ = season_rows(date(2024, 9, 10), 80, 2)
    old2, _ = season_rows(date(2023, 9, 10), 80, 1)
    import_rows(engine, tmp_path, old2 + old + rows + extra)
    with engine.connect() as c:
        ids = {r.name: r.id for r in c.execute(text("SELECT id, name FROM mandis"))}
        cmd = str(c.execute(text("SELECT id FROM commodities")).scalar_one())
    m2 = {"mandi": ids, "commodity": cmd}
    stock(a, m2)
    r = call(a.admin, "POST", "/plans", req(m2, {"Delta APMC": "0.00"}))
    assert r.status_code == 422 and r.json()["issues"][0]["code"] == "MANDI_NOT_ELIGIBLE"
    doc = call(a.admin, "POST", "/plans", req(m2, {ALPHA: "10.00", "Delta APMC": "0.00"})).json()
    rows_by = {m["name"]: m for m in doc["mandis"]}
    assert (
        rows_by["Delta APMC"]["eligible"] is False and rows_by["Delta APMC"]["excluded_reason"] == "BASIS_TOO_FEW_DAYS"
    )
    assert rows_by["Delta APMC"]["days_used"] == 3 and rows_by[ALPHA]["eligible"] is True
    assert [m["id"] for m in doc["allocation_input"]["mandis"]] == [ids[ALPHA]]
    assert any(i["code"] == "MANDI_EXCLUDED" for i in doc["explanation"])


def test_a_price_that_would_not_be_positive_is_refused(two: tuple[Fpo, Fpo], engine: Engine, tmp_path: Path) -> None:
    a, _ = two
    old, _ = season_rows(date(2024, 9, 10), 80, 2)
    # the index sits at 3300 for 25 days and then falls to 3200 for the last 5; "Low APMC" always reports 10.00, so its
    # basis (median of 10 - index over the window) is -3290 and the forecast 3200 + (-3290) is below zero
    bases = [3300] * 25 + [3200] * 5
    start = date(2025, 9, 10)
    low = [row(800_000 + i, "Low APMC", "1121", (start + timedelta(days=i)).isoformat(), "10.00") for i in range(30)]
    import_rows(engine, tmp_path, old + rows_for(start, bases) + low)
    with engine.connect() as c:
        ids = {r.name: r.id for r in c.execute(text("SELECT id, name FROM mandis"))}
        cmd = str(c.execute(text("SELECT id FROM commodities")).scalar_one())
    m2 = {"mandi": ids, "commodity": cmd}
    stock(a, m2)
    r = call(a.admin, "POST", "/plans", req(m2, {"Low APMC": "0.00"}))
    assert r.status_code == 422 and r.json()["issues"][0]["code"] == "PRICE_NOT_POSITIVE", r.text
    assert call(a.admin, "GET", "/plans").json() == []


# ---- idempotency and isolation ----
def test_idempotent_creation(two: tuple[Fpo, Fpo], market: dict[str, Any], engine: Engine) -> None:
    a, _ = two
    stock(a, market)
    body = req(market)
    r1 = call(a.admin, "POST", "/plans", body, key="plan-key-0001")
    r2 = call(a.admin, "POST", "/plans", body, key="plan-key-0001")
    assert r1.status_code == r2.status_code == 201 and r1.json() == r2.json()
    assert len(call(a.admin, "GET", "/plans").json()) == 1
    other = req(market, {ALPHA: "36.00"})
    assert call(a.admin, "POST", "/plans", other, key="plan-key-0001").status_code == 409
    assert call(a.admin, "POST", "/plans", body).status_code == 201  # no key: a new plan
    assert len(call(a.admin, "GET", "/plans").json()) == 2


def test_another_fpo_sees_and_plans_nothing_of_ours(
    two: tuple[Fpo, Fpo], market: dict[str, Any], engine: Engine
) -> None:
    a, b = two
    stock(a, market)
    doc = call(a.admin, "POST", "/plans", req(market)).json()
    missing = call(b.admin, "GET", f"/plans/pln_{'0' * 26}")
    theirs = call(b.admin, "GET", f"/plans/{doc['id']}")
    assert theirs.status_code == missing.status_code == 404
    assert theirs.json()["detail"] == missing.json()["detail"]
    assert "CROSS_TENANT_REFERENCE" in audit_events(engine, actor_user_id=b.admin_id)
    assert call(b.admin, "GET", "/plans").json() == []
    r = call(b.admin, "POST", "/plans", req(market))  # B has no stock; A's lots never leak into B's plan
    assert r.status_code == 422 and r.json()["issues"][0]["code"] == "NO_ELIGIBLE_LOTS"
    # the same key in two FPOs is two different keys
    stock(b, market)
    assert call(b.admin, "POST", "/plans", req(market), key="shared-key-001").status_code == 201
    assert call(a.admin, "POST", "/plans", req(market), key="shared-key-001").status_code == 201
    # the database walls
    assert as_app(engine, None, "SELECT 1 FROM plans") == []
    assert {r[0] for r in as_app(engine, b.fpo_id, "SELECT fpo_id FROM plans")} == {b.fpo_id}
    for sql in ("UPDATE plans SET status = 'PROPOSED'", "DELETE FROM plans"):
        with pytest.raises(DBAPIError):
            as_app(engine, a.fpo_id, sql)
        with pytest.raises(DBAPIError), engine.begin() as c:  # immutable even for the owner
            c.execute(text(sql))
    with pytest.raises(DBAPIError):
        as_app(
            engine,
            a.fpo_id,
            "INSERT INTO plans (id, fpo_id, commodity_id, variety, status, origin, input_sha256, doc, created_by) "
            "SELECT 'pln_"
            + "1" * 26
            + "', :other, commodity_id, variety, status, origin, input_sha256, doc, created_by FROM plans LIMIT 1",
            other=b.fpo_id,
        )


# ---- the optimiser did not prove optimality, or failed ----
def test_a_result_that_is_not_proven_optimal_is_stored_but_not_proposed(
    two: tuple[Fpo, Fpo], market: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    a, _ = two
    stock(a, market)
    real = solve

    def not_proven(inp: Any, **kw: Any) -> Any:
        return dataclasses.replace(real(inp, **kw), optimality="FEASIBLE_NOT_PROVEN", termination="TIME_LIMIT")

    monkeypatch.setattr("kisaniti_api.plan.builder.solve", not_proven)
    doc = call(a.admin, "POST", "/plans", req(market)).json()
    valid("plan", "plan_proposal", doc)
    assert doc["status"] == "NOT_PROPOSED" and doc["not_proposed_reasons"] == ["OPTIMALITY_NOT_PROVEN"]
    assert doc["allocation_result"]["status"]["optimality"] == "FEASIBLE_NOT_PROVEN"
    assert call(a.admin, "GET", f"/plans/{doc['id']}").json()["status"] == "NOT_PROPOSED"  # kept for the audit trail


def test_no_solution_and_a_crash_are_stored_visibly(
    two: tuple[Fpo, Fpo], market: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    a, _ = two
    stock(a, market)
    real = solve

    def no_plan(inp: Any, **kw: Any) -> Any:
        return dataclasses.replace(
            real(inp, **kw), plan=None, objective=None, optimality="NONE", termination="TIME_LIMIT"
        )

    monkeypatch.setattr("kisaniti_api.plan.builder.solve", no_plan)
    doc = call(a.admin, "POST", "/plans", req(market)).json()
    valid("plan", "plan_proposal", doc)
    assert doc["status"] == "NOT_PROPOSED" and doc["not_proposed_reasons"] == ["NO_SOLUTION"]
    assert doc["allocation_result"]["status"]["solution"] == "NONE" and doc["allocation_result"]["assignments"] == []

    def crash(inp: Any, **kw: Any) -> Any:
        raise RuntimeError("solver exploded")

    monkeypatch.setattr("kisaniti_api.plan.builder.solve", crash)
    doc = call(a.admin, "POST", "/plans", req(market)).json()
    valid("plan", "plan_proposal", doc)
    assert doc["status"] == "NOT_PROPOSED" and doc["not_proposed_reasons"] == ["TECHNICAL_FAILURE"]
    assert doc["allocation_result"]["status"]["termination"] == "TECHNICAL_FAILURE"
    assert "exploded" not in json.dumps(doc)  # no internal message leaks into a stored document


def test_a_live_mode_refuses_synthetic_input(
    two: tuple[Fpo, Fpo], market: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    a, _ = two
    stock(a, market)
    monkeypatch.setenv("KISANITI_MODE", "LIVE")
    r = call(a.admin, "POST", "/plans", req(market))
    assert r.status_code == 422 and r.json()["issues"][0]["code"] == "SYNTHETIC_INPUT_IN_LIVE", r.text

# ruff: noqa: F811, F401  (fixtures imported from test_access by name: pytest uses them as arguments)
"""Device registry slice (contract devices/v1) on real PostgreSQL: registry, assignments, tenant-scoped readings.

Fictional FPOs, fictional devices, SYNTHETIC readings from the software node. The end-to-end tests run the real
ingestion code (validate, store, acknowledge) with the registry read from the `devices` table.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from kisaniti_ingestion.conformance import run_receiver
from kisaniti_ingestion.registry import DbRegistry
from kisaniti_ingestion.service import handle
from kisaniti_ingestion.simulator import SCENARIOS, InProcessLink, SimDevice, run_simulation
from kisaniti_ingestion.store import ReadingStore, open_store
from referencing import Registry, Resource
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from tests.test_access import (
    Fpo,
    access_env,
    add_user,
    as_app,
    audit_events,
    clean_up_access_data,
    strip,
    two,
)

CONTRACTS = Path(__file__).resolve().parents[2] / "contracts"


def valid(name: str, doc: Any) -> None:
    reg: Registry = Registry()
    for d in (CONTRACTS / "devices" / "v1", CONTRACTS / "shared" / "v1"):
        for f in d.glob("*.schema.json"):
            body = json.loads(f.read_text())
            reg = reg.with_resource(body["$id"], Resource.from_contents(body))
    sid = json.loads((CONTRACTS / "devices" / "v1" / f"{name}.schema.json").read_text())["$id"]
    errors = [e.message for e in Draft202012Validator({"$ref": sid}, registry=reg).iter_errors(doc)]
    assert errors == [], errors


def dev_id(prefix: str = "kn-sim") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def register(f: Fpo, device_id: str, origin: str = "SIMULATOR") -> Any:
    return f.admin.post("/devices", {"device_id": device_id, "source_origin": origin})


@pytest.fixture()
def store(database_url: str) -> Any:
    s = open_store(database_url)
    yield s
    s.conn.close()


def db_registry(engine: Engine) -> DbRegistry:
    def fetch() -> list[tuple[str, str]]:
        with engine.connect() as c:
            return [(r[0], r[1]) for r in c.execute(text("SELECT device_id, source_origin FROM devices WHERE active"))]

    return DbRegistry(fetch, ttl_s=0, miss_s=0)  # always fresh in tests


def ingest(
    engine: Engine, store: ReadingStore, device_id: str, scenario: str = "stable_dry", steps: int = 30
) -> SimDevice:
    reg = db_registry(engine)
    now = datetime.now(UTC).replace(microsecond=0)
    link = InProcessLink(lambda t, p, at: handle(t, p, at, reg, store).ack, lambda: now)
    sim = SimDevice(device_id, link)
    run_simulation(sim, SCENARIOS[scenario], 1, uuid.uuid4().hex[:8], now - timedelta(minutes=5 * steps), steps)
    return sim


# ---- registry and roles --------------------------------------------------------------------------------------------
def test_admin_registers_a_device_and_only_its_own_fpo_sees_it(two: tuple[Fpo, Fpo]) -> None:
    a, b = two
    d = dev_id()
    r = register(a, d)
    assert r.status_code == 201, r.text
    valid("device", r.json())
    assert (
        r.json()["fpo_id"] == a.fpo_id
        and r.json()["current_assignment"] is None
        and r.json()["last_reading_at"] is None
    )
    assert d in {x["device_id"] for x in a.admin.get("/devices").json()}
    assert d not in {x["device_id"] for x in b.admin.get("/devices").json()}


def test_device_ids_are_unique_across_the_platform(two: tuple[Fpo, Fpo]) -> None:
    a, b = two
    d = dev_id()
    assert register(a, d).status_code == 201
    r = register(b, d)
    assert r.status_code == 409 and r.json()["code"] == "CONFLICT"


def test_request_cannot_name_an_fpo_or_a_bad_origin(two: tuple[Fpo, Fpo]) -> None:
    a, b = two
    assert (
        a.admin.post("/devices", {"device_id": dev_id(), "source_origin": "SIMULATOR", "fpo_id": b.fpo_id}).status_code
        == 422
    )
    assert a.admin.post("/devices", {"device_id": dev_id(), "source_origin": "HUMAN"}).status_code == 422
    assert a.admin.post("/devices", {"device_id": "Bad_ID!", "source_origin": "SIMULATOR"}).status_code == 422


def test_roles_decide_who_may_register_assign_and_read(two: tuple[Fpo, Fpo]) -> None:
    a, _ = two
    d = dev_id()
    register(a, d)
    _, _, staff = add_user(a, "STAFF")
    _, _, viewer = add_user(a, "VIEWER")
    body = {"device_id": dev_id(), "source_origin": "SIMULATOR"}
    assert staff.post("/devices", body).status_code == 403 and viewer.post("/devices", body).status_code == 403
    assert viewer.get("/devices").status_code == 200 and viewer.get(f"/devices/{d}/readings").status_code == 200
    asg = {"placement": "IN_GRAIN", "crop": "PADDY"}
    assert viewer.post(f"/devices/{d}/assignments", asg).status_code == 403
    assert staff.post(f"/devices/{d}/assignments", asg).status_code == 201


# ---- another FPO's device is not there ------------------------------------------------------------------------------
def test_another_fpos_device_is_a_404_like_a_missing_one_and_nothing_is_written(
    engine: Engine, two: tuple[Fpo, Fpo]
) -> None:
    a, b = two
    d = dev_id()
    register(a, d)
    missing = dev_id("kn-none")
    asg = {"placement": "IN_GRAIN", "crop": "PADDY"}
    calls = [
        ("get", "/devices/{}", None),
        ("get", "/devices/{}/assignments", None),
        ("get", "/devices/{}/readings", None),
        ("post", "/devices/{}/assignments", asg),
        ("post", "/devices/{}/deactivate", None),
    ]
    for method, path, body in calls:
        other = getattr(b.admin, method)(path.format(d), *([body] if method == "post" else []))
        gone = getattr(b.admin, method)(path.format(missing), *([body] if method == "post" else []))
        assert other.status_code == gone.status_code == 404, path
        assert strip(other) == strip(gone), path
    with engine.connect() as c:
        assert (
            c.execute(text("SELECT count(*) FROM device_assignments WHERE device_id = :d"), {"d": d}).scalar_one() == 0
        )
        assert c.execute(text("SELECT active FROM devices WHERE device_id = :d"), {"d": d}).scalar_one() is True
    assert "CROSS_TENANT_REFERENCE" in audit_events(engine, actor_user_id=b.admin_id)


# ---- assignments -------------------------------------------------------------------------------------------------
def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def test_assignment_history_is_append_only_and_the_current_one_is_the_latest_not_in_the_future(
    two: tuple[Fpo, Fpo],
) -> None:
    a, _ = two
    d = dev_id()
    register(a, d)
    now = datetime.now(UTC)
    first = a.admin.post(
        f"/devices/{d}/assignments",
        {"placement": "AMBIENT", "crop": "WHEAT", "assigned_at": iso(now - timedelta(hours=2))},
    )
    second = a.admin.post(
        f"/devices/{d}/assignments",
        {
            "placement": "IN_GRAIN",
            "crop": "PADDY",
            "assigned_at": iso(now - timedelta(hours=1)),
            "sample_interval_s": 600,
        },
    )
    soon = a.admin.post(
        f"/devices/{d}/assignments",
        {"placement": "AMBIENT", "crop": "PADDY", "assigned_at": iso(now + timedelta(minutes=2))},
    )
    assert (first.status_code, second.status_code, soon.status_code) == (201, 201, 201)
    for r in (first, second, soon):
        valid("assignment", r.json())
    dev = a.admin.get(f"/devices/{d}").json()
    valid("device", dev)
    assert (
        dev["current_assignment"]["placement"] == "IN_GRAIN" and dev["current_assignment"]["sample_interval_s"] == 600
    )
    history = a.admin.get(f"/devices/{d}/assignments").json()
    assert [h["assigned_at"] for h in history] == sorted((h["assigned_at"] for h in history), reverse=True) and len(
        history
    ) == 3
    assert a.admin.put(f"/devices/{d}/assignments", {}).status_code == 405  # nothing can edit the history


def test_assignment_rules_are_enforced(two: tuple[Fpo, Fpo]) -> None:
    a, b = two
    d = dev_id()
    register(a, d)
    now = datetime.now(UTC)
    ok = {"placement": "IN_GRAIN", "crop": "PADDY", "assigned_at": iso(now - timedelta(minutes=30))}
    assert a.admin.post(f"/devices/{d}/assignments", ok).status_code == 201
    dup = a.admin.post(f"/devices/{d}/assignments", ok)
    assert dup.status_code == 409
    future = a.admin.post(f"/devices/{d}/assignments", {**ok, "assigned_at": iso(now + timedelta(hours=1))})
    assert future.status_code == 422 and future.json()["issues"][0]["code"] == "ASSIGNED_AT_IN_FUTURE"
    bad_time = a.admin.post(f"/devices/{d}/assignments", {**ok, "assigned_at": "yesterday"})
    assert bad_time.status_code == 422
    assert a.admin.post(f"/devices/{d}/assignments", {**ok, "fpo_id": b.fpo_id}).status_code == 422
    assert a.admin.post(f"/devices/{d}/assignments", {"placement": "ROOF", "crop": "PADDY"}).status_code == 422
    assert a.admin.post(f"/devices/{d}/assignments", {**ok, "sample_interval_s": 0}).status_code == 422


def test_a_deactivated_device_takes_no_new_assignment_and_is_no_longer_accepted(
    engine: Engine, store: ReadingStore, two: tuple[Fpo, Fpo]
) -> None:
    a, _ = two
    d = dev_id()
    register(a, d)
    assert ingest(engine, store, d, steps=3).buffer == []  # accepted while active
    assert a.admin.post(f"/devices/{d}/deactivate").status_code == 204
    assert a.admin.get(f"/devices/{d}").json()["active"] is False
    assert a.admin.post(f"/devices/{d}/assignments", {"placement": "IN_GRAIN", "crop": "PADDY"}).status_code == 409
    again = ingest(engine, store, d, steps=3)
    assert len(again.buffer) == 3  # no acknowledgement any more, the device keeps its samples


# ---- the database enforces it as well ----------------------------------------------------------------------------
def test_restricted_role_sees_only_its_own_devices_assignments_and_readings(
    engine: Engine, store: ReadingStore, two: tuple[Fpo, Fpo]
) -> None:
    a, b = two
    da, db = dev_id(), dev_id()
    register(a, da)
    register(b, db)
    a.admin.post(f"/devices/{da}/assignments", {"placement": "IN_GRAIN", "crop": "PADDY"})
    b.admin.post(f"/devices/{db}/assignments", {"placement": "IN_GRAIN", "crop": "WHEAT"})
    ingest(engine, store, da, steps=4)
    ingest(engine, store, db, steps=6)
    for table in ("devices", "device_assignments"):
        assert as_app(engine, None, f"SELECT 1 FROM {table}") == []
        assert {r[0] for r in as_app(engine, a.fpo_id, f"SELECT fpo_id FROM {table}")} == {a.fpo_id}
    assert as_app(engine, None, "SELECT 1 FROM sensor_readings") == []
    assert {r[0] for r in as_app(engine, a.fpo_id, "SELECT DISTINCT device_id FROM sensor_readings")} == {da}
    assert len(as_app(engine, b.fpo_id, "SELECT 1 FROM sensor_readings WHERE device_id = :d", d=db)) == 6
    assert as_app(engine, a.fpo_id, "SELECT 1 FROM sensor_readings WHERE device_id = :d", d=db) == []


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT content_sha256 FROM sensor_readings",
        "SELECT * FROM sensor_readings",
        "SELECT * FROM sensor_reading_conflicts",
        "SELECT * FROM sensor_rejected_messages",
        "INSERT INTO sensor_readings (reading_id) VALUES ('x')",
        "UPDATE sensor_readings SET ingest_status = 'ACCEPTED'",
        "UPDATE devices SET fpo_id = NULL",
        "UPDATE devices SET source_origin = 'DEVICE'",
    ],
)
def test_restricted_role_cannot_read_raw_columns_or_change_what_it_must_not(
    engine: Engine, two: tuple[Fpo, Fpo], sql: str
) -> None:
    a, _ = two
    with pytest.raises(DBAPIError):
        as_app(engine, a.fpo_id, sql)


def test_restricted_role_cannot_register_a_device_for_another_fpo(engine: Engine, two: tuple[Fpo, Fpo]) -> None:
    a, b = two
    with pytest.raises(DBAPIError, match="row-level security"):
        as_app(
            engine,
            a.fpo_id,
            "INSERT INTO devices (device_id, fpo_id, source_origin) VALUES (:d, :f, 'DEVICE')",
            d=dev_id(),
            f=b.fpo_id,
        )


def test_an_assignment_must_carry_the_devices_own_fpo_and_cannot_be_changed(
    engine: Engine, two: tuple[Fpo, Fpo]
) -> None:
    a, b = two
    d = dev_id()
    register(a, d)
    ins = (
        "INSERT INTO device_assignments (device_id, fpo_id, placement, crop, assigned_at, sample_interval_s) "
        "VALUES (:d, :f, 'IN_GRAIN', 'PADDY', now(), 300)"
    )
    with pytest.raises(IntegrityError), engine.begin() as c:  # owner connection: still refused by the composite key
        c.execute(text(ins), {"d": d, "f": b.fpo_id})
    with engine.begin() as c:
        c.execute(text(ins), {"d": d, "f": a.fpo_id})
    for sql in ("UPDATE device_assignments SET crop = 'WHEAT'", "DELETE FROM device_assignments"):
        with pytest.raises(DBAPIError), engine.begin() as c:
            c.execute(text(sql))


# ---- end to end: API registration, real ingestion code, tenant-scoped readings ------------------------------------
def test_readings_of_a_registered_device_reach_only_its_own_fpo(
    engine: Engine, store: ReadingStore, two: tuple[Fpo, Fpo]
) -> None:
    a, b = two
    d = dev_id()
    register(a, d)
    sim = ingest(engine, store, d, "stable_dry", 30)
    assert sim.buffer == [] and sim.dropped == 0  # every sample acknowledged
    rows = a.admin.get(f"/devices/{d}/readings?limit=1000").json()
    assert len(rows) == 30
    for r in rows:
        valid("reading_view", r)
        assert r["origin"] == "SYNTHETIC" and r["ingest_status"] == "ACCEPTED"
    times = [r["measured_at"] for r in rows]
    assert times == sorted(times, reverse=True)  # newest first
    assert len(a.admin.get(f"/devices/{d}/readings?limit=5").json()) == 5
    assert a.admin.get(f"/devices/{d}/readings?limit=0").status_code == 422
    assert a.admin.get(f"/devices/{d}/readings?limit=1001").status_code == 422
    dev = a.admin.get(f"/devices/{d}").json()
    assert dev["last_reading_at"] == times[0]
    assert b.admin.get(f"/devices/{d}/readings").status_code == 404


def test_unregistered_devices_are_quarantined_and_visible_to_nobody(
    engine: Engine, store: ReadingStore, two: tuple[Fpo, Fpo]
) -> None:
    a, b = two
    ghost = dev_id("kn-ghost")
    sim = ingest(engine, store, ghost, steps=5)
    assert len(sim.buffer) == 5  # never acknowledged: the device keeps all five and keeps resending the oldest
    with engine.connect() as c:
        stored = c.execute(text("SELECT ingest_status FROM sensor_readings WHERE device_id = :d"), {"d": ghost}).all()
    assert [r[0] for r in stored] == ["QUARANTINED"]  # one stored copy; the resends were duplicates
    for f in (a, b):
        assert as_app(engine, f.fpo_id, "SELECT 1 FROM sensor_readings WHERE device_id = :d", d=ghost) == []


def test_a_device_whose_clock_never_syncs_has_no_trusted_last_reading_time(
    engine: Engine, store: ReadingStore, two: tuple[Fpo, Fpo]
) -> None:
    a, _ = two
    d = dev_id()
    register(a, d)
    ingest(engine, store, d, "never_synced", 8)
    rows = a.admin.get(f"/devices/{d}/readings").json()
    assert len(rows) == 8 and all(r["measured_at"] is None and r["measured_at_trusted"] is False for r in rows)
    assert a.admin.get(f"/devices/{d}").json()["last_reading_at"] is None


def test_receiver_conformance_passes_with_the_registry_read_from_the_database(
    engine: Engine, store: ReadingStore, two: tuple[Fpo, Fpo]
) -> None:
    a, _ = two
    d = dev_id()
    register(a, d)
    reg = db_registry(engine)
    now = datetime.now(UTC)
    results = run_receiver(lambda t, p: handle(t, p, now, reg, store).ack, d, "SIMULATOR", now)
    assert all(r.passed for r in results), [(r.case.case_id, r.got) for r in results if not r.passed]


def test_registering_while_the_service_runs_is_enough(
    engine: Engine, store: ReadingStore, two: tuple[Fpo, Fpo]
) -> None:
    a, _ = two
    d = dev_id()
    reg = db_registry(engine)
    assert d not in reg  # unknown: the service would quarantine it
    register(a, d)
    assert d in reg and reg[d].source_origin == "SIMULATOR"

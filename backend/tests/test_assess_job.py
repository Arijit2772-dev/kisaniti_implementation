# ruff: noqa: F811, F401  (fixtures imported from test_access by name: pytest uses them as arguments)
"""Scheduled storage assessment (devices/v1 amendment A1) on real PostgreSQL.

The stored results must equal the rule's own hourly replay (`assess_series`), the hysteresis state must survive a
restart in the middle of its 48-hour hold, and a reading with an untrusted time must never count.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from kisaniti_core.provenance import Origin
from kisaniti_core.storage import assess_series, assessment_to_contract
from kisaniti_core.storage.rule import WINDOW
from kisaniti_ingestion.service import handle
from kisaniti_ingestion.store import ReadingStore
from referencing import Registry, Resource
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from kisaniti_api.devices import assess_job
from kisaniti_api.devices.assess_job import (
    CATCH_UP_CAP,
    SLOT,
    assess_device,
    ceil_slot,
    floor_slot,
    load_history,
    load_readings,
    run_all,
)
from tests.test_access import (
    Fpo,
    access_env,
    as_app,
    clean_up_access_data,
    strip,
    two,
)
from tests.test_devices import db_registry, dev_id, ingest, iso, register, store

CONTRACTS = Path(__file__).resolve().parents[2] / "contracts"


def assessment_schema_ok(doc: Any) -> None:
    reg: Registry = Registry()
    for d in (CONTRACTS / "sensor" / "v1", CONTRACTS / "shared" / "v1"):
        for f in d.glob("*.schema.json"):
            body = json.loads(f.read_text())
            reg = reg.with_resource(body["$id"], Resource.from_contents(body))
    sid = json.loads((CONTRACTS / "sensor" / "v1" / "storage_assessment.schema.json").read_text())["$id"]
    errors = [e.message for e in Draft202012Validator({"$ref": sid}, registry=reg).iter_errors(doc)]
    assert errors == [], errors


def assign(
    f: Fpo, device_id: str, at: datetime, placement: str = "IN_GRAIN", crop: str = "PADDY", interval: int = 300
) -> None:
    r = f.admin.post(
        f"/devices/{device_id}/assignments",
        {"placement": placement, "crop": crop, "assigned_at": iso(at), "sample_interval_s": interval},
    )
    assert r.status_code == 201, r.text


def stored_docs(engine: Engine, device_id: str) -> list[dict[str, Any]]:
    with engine.connect() as c:
        return [
            dict(r.doc)
            for r in c.execute(
                text("SELECT doc FROM storage_assessments WHERE device_id = :d ORDER BY assessed_at"), {"d": device_id}
            )
        ]


def run_job(engine: Engine, device_id: str, fpo_id: str, origin: str, now: datetime) -> int:
    with engine.begin() as conn:
        return assess_device(conn, device_id, fpo_id, origin, now)


def reference(engine: Engine, device_id: str, now: datetime, origin: Origin) -> list[dict[str, Any]]:
    """The rule's own uninterrupted hourly replay over the same stored readings, for one-assignment devices."""
    with engine.connect() as c:
        history = load_history(c, device_id)
        assert len(history) == 1
        asg = history[0]
        start = max(ceil_slot(asg.assigned_at), floor_slot(now) - CATCH_UP_CAP)
        readings = load_readings(c, device_id, start - WINDOW)
    slots = []
    t = start
    while t <= floor_slot(now):
        slots.append(t)
        t += SLOT
    return [assessment_to_contract(a) for a in assess_series(readings, asg, slots, origin)]


def put_reading(
    store_: ReadingStore,
    engine: Engine,
    device_id: str,
    boot: str,
    seq: int,
    at: datetime,
    rh: float,
    temp: float = 20.0,
    received: datetime | None = None,
) -> None:
    doc = {
        "schema": "kisaniti.device_telemetry.v1",
        "device_id": device_id,
        "boot_id": boot,
        "seq": seq,
        "measured_at": iso(at),
        "time_quality": "NTP_SYNCED",
        "uptime_ms": seq * 1000,
        "temperature_c": temp,
        "humidity_pct": rh,
        "read_status": "OK",
        "sensor_model": "SHT30",
        "firmware_version": "test",
        "source_origin": "SIMULATOR",
        "sample_interval_s": 1800,
        "resent": False,
        "dropped_since_boot": 0,
    }
    out = handle(
        f"kisaniti/v1/devices/{device_id}/telemetry",
        json.dumps(doc).encode(),
        received or at,
        db_registry(engine),
        store_,
    )
    assert out.ack is not None and out.ack["status"] == "ACCEPTED", out


# ---- equivalence with the rule's own replay, restarts, idempotence --------------------------------------------------
def test_stored_assessments_equal_the_rules_uninterrupted_hourly_replay(
    engine: Engine, store: ReadingStore, two: tuple[Fpo, Fpo]
) -> None:
    a, _ = two
    d = dev_id()
    register(a, d)
    start = datetime.now(UTC) - timedelta(hours=60)
    assign(a, d, start)
    ingest(engine, store, d, "humid_rise", 720)  # 60 hours at 5 minutes, RH rising from 55 to 82
    now = datetime.now(UTC)
    n = run_job(engine, d, a.fpo_id, "SIMULATOR", now)
    docs = stored_docs(engine, d)
    ref = reference(engine, d, now, Origin.SYNTHETIC)
    assert n == len(docs) == len(ref) and 55 <= n <= 62
    assert docs == ref  # every field, every slot
    cats = [x["category"] for x in docs]
    rank = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}
    known = [rank[c] for c in cats if c != "UNKNOWN"]
    assert cats[0] == "UNKNOWN" and cats[-1] == "HIGH" and known == sorted(known)  # rising humidity never lowers it
    assert docs[0]["unknown_reasons"] and docs[-1]["provenance"]["origin"] == "SYNTHETIC"
    for x in docs[::10]:
        assessment_schema_ok(x)


def test_running_again_stores_nothing_and_a_late_second_run_continues_the_same_series(
    engine: Engine, store: ReadingStore, two: tuple[Fpo, Fpo]
) -> None:
    a, _ = two
    d = dev_id()
    register(a, d)
    start = datetime.now(UTC) - timedelta(hours=40)
    assign(a, d, start)
    ingest(engine, store, d, "humid_rise", 480)
    now = datetime.now(UTC)
    early = floor_slot(now) - timedelta(hours=15)
    first = run_job(engine, d, a.fpo_id, "SIMULATOR", early)  # the service was down after this slot
    assert first > 0
    assert run_job(engine, d, a.fpo_id, "SIMULATOR", early) == 0  # idempotent
    second = run_job(engine, d, a.fpo_id, "SIMULATOR", now)  # catch-up of the 15 missed hours
    assert 14 <= second <= 16
    assert run_job(engine, d, a.fpo_id, "SIMULATOR", now) == 0
    assert stored_docs(engine, d) == reference(engine, d, now, Origin.SYNTHETIC)


# ---- persisted hysteresis ------------------------------------------------------------------------------------------
def test_the_hysteresis_hold_survives_a_restart_in_the_middle_of_its_48_hours(
    engine: Engine, store: ReadingStore, two: tuple[Fpo, Fpo]
) -> None:
    a, _ = two
    d = dev_id()
    register(a, d)
    end = floor_slot(datetime.now(UTC))
    t0 = end - timedelta(hours=110)
    drop = t0 + timedelta(hours=30)
    assign(a, d, t0, interval=1800)
    boot = uuid.uuid4().hex[:8]
    k, t = 0, t0
    while t <= end:  # one reading every 30 minutes: RH 72 (HIGH) for 30 h, then RH 66 (MEDIUM) for 80 h
        put_reading(store, engine, d, boot, k, t, 72.0 if t < drop else 66.0)
        k, t = k + 1, t + timedelta(minutes=30)

    mid = drop + timedelta(hours=40)  # the job runs, then the process "restarts"
    run_job(engine, d, a.fpo_id, "SIMULATOR", mid)
    before = {x["assessed_at"]: x for x in stored_docs(engine, d)}
    at_mid = before[iso(mid)]
    assert at_mid["category"] == "HIGH" and at_mid["hysteresis"]["level"] == "HIGH"
    assert at_mid["hysteresis"]["below_since"] is not None  # the 48 hours are running
    held_since = at_mid["hysteresis"]["below_since"]

    run_job(engine, d, a.fpo_id, "SIMULATOR", end)  # a new process picks up the stored state
    docs = {x["assessed_at"]: x for x in stored_docs(engine, d)}
    after_restart = docs[iso(mid + SLOT)]
    assert after_restart["hysteresis"]["below_since"] == held_since  # carried over, not restarted
    assert after_restart["category"] == "HIGH"
    release = datetime.fromisoformat(held_since.replace("Z", "+00:00")) + timedelta(hours=48)
    assert docs[iso(floor_slot(release) - SLOT)]["category"] == "HIGH"  # still held just before 48 hours
    assert docs[iso(end)]["category"] == "MEDIUM"  # released afterwards
    assert stored_docs(engine, d) == reference(engine, d, end, Origin.SYNTHETIC)


def test_a_new_assignment_resets_the_state_and_old_readings_do_not_count(
    engine: Engine, store: ReadingStore, two: tuple[Fpo, Fpo]
) -> None:
    a, _ = two
    d = dev_id()
    register(a, d)
    end = floor_slot(datetime.now(UTC))
    t0 = end - timedelta(hours=60)
    swap = t0 + timedelta(hours=30)
    assign(a, d, t0, interval=1800)
    assign(a, d, swap, interval=1800)  # the probe is put into other grain
    boot = uuid.uuid4().hex[:8]
    k, t = 0, t0
    while t <= end:
        put_reading(store, engine, d, boot, k, t, 72.0 if t < swap else 50.0)
        k, t = k + 1, t + timedelta(minutes=30)
    run_job(engine, d, a.fpo_id, "SIMULATOR", swap - SLOT)  # the job was running before the probe was moved
    run_job(engine, d, a.fpo_id, "SIMULATOR", end)
    docs = {x["assessed_at"]: x for x in stored_docs(engine, d)}
    assert docs[iso(swap - SLOT)]["category"] == "HIGH"
    assert docs[iso(end)]["category"] == "LOW"  # judged on the new grain only, the HIGH readings are not carried over
    assert docs[iso(end)]["hysteresis"]["below_since"] is None
    assert docs[iso(swap + timedelta(hours=2))]["category"] == "UNKNOWN"  # settling after the swap
    assert "SETTLING" in docs[iso(swap + timedelta(hours=2))]["unknown_reasons"]


# ---- untrusted time, no assignment, catch-up cap ------------------------------------------------------------------
def test_readings_with_an_untrusted_time_never_count(engine: Engine, store: ReadingStore, two: tuple[Fpo, Fpo]) -> None:
    a, _ = two
    d = dev_id()
    register(a, d)
    assign(a, d, datetime.now(UTC) - timedelta(hours=30))
    ingest(engine, store, d, "never_synced", 300)  # the clock never syncs: measured_at is null
    run_job(engine, d, a.fpo_id, "SIMULATOR", datetime.now(UTC))
    docs = stored_docs(engine, d)
    assert docs and all(x["category"] == "UNKNOWN" and x["samples_used"] == 0 for x in docs)
    assert all("STALE" in x["unknown_reasons"] for x in docs)


def test_a_device_with_no_assignment_gets_one_no_assignment_result(engine: Engine, two: tuple[Fpo, Fpo]) -> None:
    a, _ = two
    d = dev_id()
    register(a, d)
    now = datetime.now(UTC)
    assert run_job(engine, d, a.fpo_id, "SIMULATOR", now) == 1
    assert run_job(engine, d, a.fpo_id, "SIMULATOR", now) == 0
    doc = a.admin.get(f"/devices/{d}/assessment").json()
    assessment_schema_ok(doc)
    assert doc["category"] == "UNKNOWN" and doc["unknown_reasons"] == ["NO_ASSIGNMENT"]


def test_catch_up_is_capped_at_14_days(engine: Engine, two: tuple[Fpo, Fpo]) -> None:
    a, _ = two
    d = dev_id()
    register(a, d)
    now = datetime.now(UTC)
    assign(a, d, now - timedelta(days=40))
    n = run_job(engine, d, a.fpo_id, "SIMULATOR", now)
    docs = stored_docs(engine, d)
    assert n == len(docs) <= 14 * 24 + 1
    assert docs[0]["assessed_at"] >= iso(floor_slot(now) - CATCH_UP_CAP)


def test_one_failing_device_does_not_stop_the_others(
    engine: Engine, two: tuple[Fpo, Fpo], monkeypatch: pytest.MonkeyPatch
) -> None:
    a, b = two
    good, bad = dev_id("kn-good"), dev_id("kn-bad")
    register(a, good)
    register(b, bad)
    real = assess_job.assess_device

    def flaky(conn: Any, device_id: str, fpo_id: str, origin: str, now: datetime) -> int:
        if device_id == bad:
            raise RuntimeError("boom")
        return real(conn, device_id, fpo_id, origin, now)

    monkeypatch.setattr(assess_job, "assess_device", flaky)
    summary = run_all(engine)
    assert summary["failed"] == 1 and summary["stored"] >= 1
    assert stored_docs(engine, good) and stored_docs(engine, bad) == []


# ---- API and walls -----------------------------------------------------------------------------------------------
def test_assessment_endpoints_are_tenant_scoped_and_match_the_contract(engine: Engine, two: tuple[Fpo, Fpo]) -> None:
    a, b = two
    d = dev_id()
    register(a, d)
    own_empty = a.admin.get(f"/devices/{d}/assessment")
    assert own_empty.status_code == 404 and own_empty.json()["detail"] == "no assessment yet"
    assign(a, d, datetime.now(UTC) - timedelta(hours=10))
    run_job(engine, d, a.fpo_id, "SIMULATOR", datetime.now(UTC))
    latest = a.admin.get(f"/devices/{d}/assessment")
    assert latest.status_code == 200
    assessment_schema_ok(latest.json())
    history = a.admin.get(f"/devices/{d}/assessments?limit=5").json()
    assert 1 <= len(history) <= 5 and history[0] == latest.json()
    assert [h["assessed_at"] for h in history] == sorted((h["assessed_at"] for h in history), reverse=True)
    assert a.admin.get(f"/devices/{d}/assessments?limit=0").status_code == 422
    missing = dev_id("kn-none")
    for path in ("assessment", "assessments"):
        other, gone = b.admin.get(f"/devices/{d}/{path}"), b.admin.get(f"/devices/{missing}/{path}")
        assert other.status_code == gone.status_code == 404 and strip(other) == strip(
            gone
        )  # the same, even the message


def test_database_walls_around_stored_assessments(engine: Engine, two: tuple[Fpo, Fpo]) -> None:
    a, b = two
    da, db = dev_id(), dev_id()
    register(a, da)
    register(b, db)
    for f, d in ((a, da), (b, db)):
        assign(f, d, datetime.now(UTC) - timedelta(hours=8))
        run_job(engine, d, f.fpo_id, "SIMULATOR", datetime.now(UTC))
    assert as_app(engine, None, "SELECT 1 FROM storage_assessments") == []
    assert {r[0] for r in as_app(engine, a.fpo_id, "SELECT DISTINCT device_id FROM storage_assessments")} == {da}
    for sql in (
        "INSERT INTO storage_assessments (device_id, fpo_id, assessed_at, category, doc) "
        "VALUES ('x','y',now(),'LOW','{}')",
        "UPDATE storage_assessments SET category = 'LOW'",
        "DELETE FROM storage_assessments",
    ):
        with pytest.raises(DBAPIError):
            as_app(engine, a.fpo_id, sql)
    for sql in ("UPDATE storage_assessments SET category = 'LOW'", "DELETE FROM storage_assessments"):
        with pytest.raises(DBAPIError), engine.begin() as c:  # append-only even for the owner
            c.execute(text(sql))
    with pytest.raises(IntegrityError), engine.begin() as c:  # an assessment must carry the device's own FPO
        c.execute(
            text(
                "INSERT INTO storage_assessments (device_id, fpo_id, assessed_at, category, doc) "
                "VALUES (:d, :f, now() + interval '1 day', 'LOW', '{}')"
            ),
            {"d": da, "f": b.fpo_id},
        )


def test_a_pass_of_the_job_leaves_a_heartbeat_for_the_container_health_check(
    engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    beat = tmp_path / "beat"
    monkeypatch.setenv("KN_HEARTBEAT", str(beat))
    assert not beat.exists()
    assert assess_job.main(["--once"]) == 0
    assert beat.exists()

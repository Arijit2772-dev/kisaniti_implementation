"""Sensor-reading tables (migration 0002) on real PostgreSQL, driven by the ingestion service's own code.

Sensor data here is SYNTHETIC (simulator). The point is the database behaviour: one stored copy per identity,
conflicts and rejected raw messages kept, the schema's own checks, and exact agreement with the contract.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from alembic import command
from kisaniti_ingestion.conformance import in_process_backend, run_receiver
from kisaniti_ingestion.simulator import SCENARIOS, InProcessLink, SimDevice, run_simulation
from kisaniti_ingestion.store import ReadingStore, SchemaMissingError, StoreResult, open_store
from kisaniti_ingestion.validate import Device, to_reading
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError

from kisaniti_api.db import alembic_config

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
DEV = "kn-sim-001"
REG = {DEV: Device(DEV, "SIMULATOR")}


@pytest.fixture()
def store(engine: Engine, database_url: str) -> Iterator[ReadingStore]:
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE sensor_reading_conflicts, sensor_rejected_messages, sensor_readings CASCADE"))
    s = open_store(database_url)
    yield s
    s.conn.close()


def telemetry(seq: int, **over: Any) -> bytes:
    doc: dict[str, Any] = {
        "schema": "kisaniti.device_telemetry.v1",
        "device_id": DEV,
        "boot_id": "a1b2c3d4",
        "seq": seq,
        "measured_at": "2026-10-24T11:00:00.000Z",
        "time_quality": "NTP_SYNCED",
        "uptime_ms": 1000,
        "temperature_c": 26.0,
        "humidity_pct": 60.0,
        "read_status": "OK",
        "sensor_model": "SHT30",
        "firmware_version": "test",
        "source_origin": "SIMULATOR",
        "sample_interval_s": 300,
        "resent": False,
        "dropped_since_boot": 0,
    }
    doc.update(over)
    return json.dumps(doc).encode()


def reading(seq: int, **over: Any) -> dict[str, Any]:
    r = to_reading(f"kisaniti/v1/devices/{DEV}/telemetry", telemetry(seq, **over), NOW, REG)
    assert isinstance(r, dict)
    return r


def test_migration_creates_the_tables_and_head_is_ready(engine: Engine, client: Any) -> None:
    with engine.connect() as c:
        names = {
            r[0]
            for r in c.execute(text("SELECT table_name FROM information_schema.tables WHERE table_schema='public'"))
        }
    assert {"sensor_readings", "sensor_reading_conflicts", "sensor_rejected_messages"} <= names
    assert client.get("/health/ready").status_code == 200


def test_store_refuses_an_unmigrated_database(database_url: str, engine: Engine) -> None:
    command.downgrade(alembic_config(database_url), "0001_fpo_tenant")
    try:
        with pytest.raises(SchemaMissingError):
            open_store(database_url)
    finally:
        command.upgrade(alembic_config(database_url), "head")


def test_new_duplicate_and_conflict_on_postgresql(store: ReadingStore) -> None:
    assert store.put(reading(1)) is StoreResult.NEW
    assert store.put(reading(1, resent=True)) is StoreResult.DUPLICATE  # resent is not measurement content
    assert store.put(reading(1, humidity_pct=61.0)) is StoreResult.CONFLICT
    kept = store.stored(f"{DEV}:a1b2c3d4:1")
    assert kept is not None and kept["humidity_pct"] == 60.0  # first copy wins
    assert store.counts() == {"sensor_readings": 1, "sensor_reading_conflicts": 1, "sensor_rejected_messages": 0}


def test_readings_come_back_in_measurement_time_order_as_stored(store: ReadingStore) -> None:
    for seq, at in ((3, "2026-10-24T11:15:00.000Z"), (1, "2026-10-24T11:05:00.000Z"), (2, "2026-10-24T11:10:00.000Z")):
        store.put(reading(seq, measured_at=at))
    got = store.readings_for(DEV)
    assert [r["seq"] for r in got] == [1, 2, 3]
    assert got[0]["provenance"]["origin"] == "SYNTHETIC" and got[0]["measured_at"] == "2026-10-24T11:05:00.000Z"


def test_unsynced_reading_with_null_time_is_stored(store: ReadingStore) -> None:
    r = reading(4, time_quality="UNSYNCED", measured_at=None)
    assert store.put(r) is StoreResult.NEW
    stored = store.stored(r["reading_id"])
    assert stored is not None and stored["measured_at"] is None and stored["measured_at_trusted"] is False


def test_rejected_message_is_kept_byte_for_byte(store: ReadingStore) -> None:
    from kisaniti_ingestion.service import handle

    junk = b"\xff\xfe not json"
    out = handle(f"kisaniti/v1/devices/{DEV}/telemetry", junk, NOW, REG, store)
    assert out.result.startswith("REJECTED") and out.ack is None
    row = store.conn.execute("SELECT raw, reason FROM sensor_rejected_messages").fetchone()
    assert row is not None and bytes(row[0]) == junk and row[1] == "NOT_JSON"


@pytest.mark.parametrize(
    ("reading_id", "boot", "seq", "status", "reason"),
    [
        pytest.param("kn-sim-001:a1b2c3d4:9", "a1b2c3d4", 8, "ACCEPTED", None, id="id-does-not-match-its-parts"),
        pytest.param("kn-sim-001:a1b2c3d4:8", "a1b2c3d4", 8, "MAYBE", None, id="unknown-status"),
        pytest.param("kn-sim-001:a1b2c3d4:8", "a1b2c3d4", 8, "QUARANTINED", None, id="quarantined-without-reason"),
        pytest.param("kn-sim-001:a1b2c3d4:8", "a1b2c3d4", 8, "ACCEPTED", "X", id="reason-on-accepted"),
        pytest.param("kn-sim-001:ZZZ:8", "ZZZ", 8, "ACCEPTED", None, id="boot-id-wrong-form"),
        pytest.param("kn-sim-001:a1b2c3d4:-1", "a1b2c3d4", -1, "ACCEPTED", None, id="negative-sequence"),
    ],
)
def test_database_checks_reject_malformed_rows(
    engine: Engine, store: ReadingStore, reading_id: str, boot: str, seq: int, status: str, reason: str | None
) -> None:
    cols = "reading_id, device_id, boot_id, seq, received_at, ingest_status, quarantine_reason, content_sha256, doc"
    sql = f"INSERT INTO sensor_readings ({cols}) VALUES (:i, 'kn-sim-001', :b, :s, now(), :st, :r, :h, '{{}}')"
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(text(sql), {"i": reading_id, "b": boot, "s": seq, "st": status, "r": reason, "h": "a" * 64})


def test_a_well_formed_row_passes_the_same_checks(engine: Engine, store: ReadingStore) -> None:
    """The negative cases above would pass vacuously if the insert were broken for every row."""
    assert store.put(reading(8)) is StoreResult.NEW


def test_sixteen_concurrent_writers_store_one_copy(database_url: str, store: ReadingStore) -> None:
    results: list[StoreResult] = []
    lock = threading.Lock()
    barrier = threading.Barrier(16)
    r = reading(21)

    def writer() -> None:
        s = open_store(database_url)
        try:
            barrier.wait()
            res = s.put(dict(r))
            with lock:
                results.append(res)
        finally:
            s.conn.close()

    threads = [threading.Thread(target=writer) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count(StoreResult.NEW) == 1 and results.count(StoreResult.DUPLICATE) == 15
    assert store.counts()["sensor_readings"] == 1 and store.counts()["sensor_reading_conflicts"] == 0


def test_concurrent_devices_with_outage_and_duplicates_end_consistent(database_url: str, store: ReadingStore) -> None:
    devices = {f"kn-sim-{i:03d}": Device(f"kn-sim-{i:03d}", "SIMULATOR") for i in range(1, 5)}
    errors: list[BaseException] = []

    def run(device_id: str, k: int) -> None:
        try:
            s = open_store(database_url)
            from kisaniti_ingestion.service import handle

            link = InProcessLink(lambda t, p, at: handle(t, p, at, devices, s).ack, lambda: NOW, duplicate_every=4)
            dev = SimDevice(device_id, link)

            def on_step(step: int, _t: datetime) -> None:
                link.down = 10 <= step < 20 and k % 2 == 0

            first = NOW - timedelta(minutes=5 * 60)
            run_simulation(dev, SCENARIOS["sensor_fault"], k, f"{k:08x}", first, 60, on_step=on_step)
            link.down = False
            dev.flush()
            assert dev.buffer == []
            s.conn.close()
        except BaseException as e:
            errors.append(e)

    threads = [threading.Thread(target=run, args=(d, i)) for i, d in enumerate(devices, start=1)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert store.counts() == {"sensor_readings": 4 * 60, "sensor_reading_conflicts": 0, "sensor_rejected_messages": 0}
    for d in devices:
        assert [r["seq"] for r in store.readings_for(d)] == list(range(60))


def test_receiver_conformance_passes_on_postgresql(store: ReadingStore) -> None:
    from kisaniti_ingestion.service import handle

    results = run_receiver(lambda t, p: handle(t, p, NOW, REG, store).ack, DEV, "SIMULATOR", NOW)
    assert all(r.passed for r in results), [(r.case.case_id, r.got) for r in results if not r.passed]
    c = store.counts()
    # stored: R01, R04, R07 (quarantined), R08, R09, R10, R11; rejected raw: R05, R06, R12, R13; conflict: R03
    assert c == {"sensor_readings": 7, "sensor_rejected_messages": 4, "sensor_reading_conflicts": 1}


def test_in_process_and_postgresql_backends_agree_on_every_receiver_case(store: ReadingStore) -> None:
    from kisaniti_ingestion.service import handle

    handle_mem, _ = in_process_backend(REG)
    mem = run_receiver(lambda t, p: handle_mem(t, p, NOW), DEV, "SIMULATOR", NOW, boot_id="a1b2c3d4")
    pg = run_receiver(lambda t, p: handle(t, p, NOW, REG, store).ack, DEV, "SIMULATOR", NOW, boot_id="a1b2c3d4")
    assert [(r.case.case_id, r.got) for r in mem] == [(r.case.case_id, r.got) for r in pg]

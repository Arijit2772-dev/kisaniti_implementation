# ruff: noqa: F811, F401  (fixtures imported from test_access by name: pytest uses them as arguments)
"""HW-S3 on real PostgreSQL: the server ends the worker's connection (what a restart does) in the middle of a device's
run. The reading in flight gets no ack, the device resends it, the store reconnects, and every reading is stored
exactly once. The full restart of the database container is checked on the running stack by hw_stack_check.sh."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from kisaniti_ingestion.service import handle
from kisaniti_ingestion.simulator import SCENARIOS, InProcessLink, SimDevice, run_simulation
from kisaniti_ingestion.store import ReadingStore
from sqlalchemy import Engine, text

from tests.test_access import Fpo, access_env, clean_up_access_data, two
from tests.test_devices import db_registry, dev_id, register, store


def end_store_connection(engine: Engine, s: ReadingStore) -> None:
    pid = s.conn.info.backend_pid  # type: ignore[attr-defined]
    with engine.begin() as c:
        assert c.execute(text("SELECT pg_terminate_backend(:p)"), {"p": pid}).scalar() is True


def test_the_database_ending_the_connection_loses_no_reading_and_stores_none_twice(
    engine: Engine, store: ReadingStore, two: tuple[Fpo, Fpo]
) -> None:
    a, _ = two
    d = dev_id()
    register(a, d)
    reg = db_registry(engine)
    now = datetime.now(UTC).replace(microsecond=0)
    results: list[str] = []

    def receive(t: str, p: bytes, at: datetime) -> Any:
        out = handle(t, p, at, reg, store)
        results.append(out.result)
        return out.ack

    sim = SimDevice(d, InProcessLink(receive, lambda: now))
    pids: list[int] = []

    def on_step(seq: int, _at: datetime) -> None:
        if seq in (10, 20):  # two separate outages during the run
            pids.append(store.conn.info.backend_pid)  # type: ignore[attr-defined]
            end_store_connection(engine, store)

    run_simulation(
        sim, SCENARIOS["stable_dry"], 1, uuid.uuid4().hex[:8], now - timedelta(minutes=150), 30, 300, on_step
    )
    sim.flush()

    assert results.count("STORE_UNAVAILABLE") == 2, results  # one reading in flight per outage, nothing else
    assert results.count("NEW") == 30 and "DUPLICATE" not in results
    assert sim.buffer == [] and sim.dropped == 0  # every sample finally acknowledged
    assert any(status == "NO_ACK" for _, status in sim.log)  # the device really saw the outage
    # a new connection, not the dead one
    assert store.conn.info.backend_pid not in pids  # type: ignore[attr-defined]
    rows = a.admin.get(f"/devices/{d}/readings?limit=1000").json()
    ids = [r["reading_id"] for r in rows]
    assert len(ids) == 30 and len(set(ids)) == 30

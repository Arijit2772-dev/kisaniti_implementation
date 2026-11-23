"""HW-S3: a lost database connection is an explicit STORE_UNAVAILABLE outcome with no ack, never a crash or a guess.

A fake connection stands in for PostgreSQL so this runs without a database; the real restart is checked on
PostgreSQL in backend/tests/test_ingest_reconnect.py and on the running stack by scripts/hw_stack_check.sh.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from kisaniti_ingestion.registry import DbRegistry
from kisaniti_ingestion.service import handle
from kisaniti_ingestion.store import SQLITE_DDL, ReadingStore, StoreUnavailableError
from kisaniti_ingestion.validate import Device

EXAMPLES = Path(__file__).resolve().parents[3] / "contracts" / "sensor" / "v1" / "examples" / "valid"
RECEIVED = datetime(2026, 10, 22, 10, 5, 1, 204000, tzinfo=UTC)
REG = {"kn-node-001": Device("kn-node-001", "DEVICE")}
TOPIC = "kisaniti/v1/devices/kn-node-001/telemetry"


class Lost(Exception):
    """Stands in for psycopg.OperationalError."""


class Flaky:
    """A SQLite connection that can be cut, like a PostgreSQL connection when the server restarts."""

    def __init__(self, db: sqlite3.Connection) -> None:
        self.db, self.cut, self.closed = db, False, False

    def execute(self, sql: str, params: Any = ()) -> Any:
        if self.cut or self.closed:
            raise Lost("terminating connection due to administrator command")
        return self.db.execute(sql, params)

    def commit(self) -> None:
        if self.cut or self.closed:
            raise Lost("connection lost")
        self.db.commit()

    def close(self) -> None:
        self.closed = True


def payload(seq: int) -> bytes:
    doc: dict[str, Any] = json.loads((EXAMPLES / "telemetry_ok.json").read_text())
    doc["seq"] = seq
    return json.dumps(doc).encode()


@pytest.fixture
def db() -> sqlite3.Connection:
    d = sqlite3.connect(":memory:")
    d.executescript(SQLITE_DDL)
    return d


def make(db: sqlite3.Connection, reopen_fails: int = 0) -> tuple[ReadingStore, list[Flaky]]:
    opened: list[Flaky] = []
    fails = {"left": reopen_fails}

    def reopen() -> Flaky:
        if fails["left"] > 0:
            fails["left"] -= 1
            raise Lost("could not connect: the database system is starting up")
        opened.append(Flaky(db))
        return opened[-1]

    first = Flaky(db)
    opened.append(first)
    return ReadingStore(first, reopen=reopen, lost_errors=(Lost,)), opened


def count(db: sqlite3.Connection) -> int:
    return int(db.execute("SELECT COUNT(*) FROM sensor_readings").fetchone()[0])


def test_a_lost_connection_gives_no_ack_and_the_resend_is_stored_once(db: sqlite3.Connection) -> None:
    store, opened = make(db)
    assert handle(TOPIC, payload(1), RECEIVED, REG, store).ack is not None
    opened[0].cut = True  # the server restarts
    out = handle(TOPIC, payload(2), RECEIVED, REG, store)
    assert (out.result, out.ack_topic, out.ack) == ("STORE_UNAVAILABLE", None, None)
    assert opened[0].closed, "the dead connection is closed, not reused"
    again = handle(TOPIC, payload(2), RECEIVED, REG, store)  # the device resends: a new connection is opened
    assert again.result == "NEW" and again.ack is not None and again.ack["status"] == "ACCEPTED"
    assert len(opened) == 2 and count(db) == 2


def test_while_the_database_stays_down_every_reading_is_unavailable_then_it_recovers(db: sqlite3.Connection) -> None:
    store, opened = make(db, reopen_fails=3)
    opened[0].cut = True
    results = [handle(TOPIC, payload(7), RECEIVED, REG, store).result for _ in range(4)]
    assert results == ["STORE_UNAVAILABLE"] * 4  # the cut, then three failed reconnects
    assert handle(TOPIC, payload(7), RECEIVED, REG, store).result == "NEW"
    assert count(db) == 1


def test_a_reading_committed_just_before_the_cut_is_a_duplicate_on_resend(db: sqlite3.Connection) -> None:
    store, opened = make(db)
    first = handle(TOPIC, payload(3), RECEIVED, REG, store)
    assert first.result == "NEW"  # stored; suppose the ack was lost with the connection
    opened[0].cut = True
    assert handle(TOPIC, payload(4), RECEIVED, REG, store).result == "STORE_UNAVAILABLE"
    dup = handle(TOPIC, payload(3), RECEIVED, REG, store)
    assert dup.result == "DUPLICATE" and dup.ack is not None and dup.ack["status"] == "DUPLICATE"
    assert count(db) == 1


def test_a_rejected_message_during_an_outage_is_unavailable_too(db: sqlite3.Connection) -> None:
    store, opened = make(db)
    opened[0].cut = True
    out = handle(TOPIC, b"not json", RECEIVED, REG, store)
    assert (out.result, out.ack) == ("STORE_UNAVAILABLE", None)
    assert handle(TOPIC, b"not json", RECEIVED, REG, store).result.startswith("REJECTED:")


def test_an_unreadable_device_registry_is_unavailable_not_a_crash(db: sqlite3.Connection) -> None:
    store, _ = make(db)

    def fetch() -> list[tuple[str, str]]:
        raise StoreUnavailableError("device registry unreadable (OperationalError)")

    out = handle(TOPIC, payload(5), RECEIVED, DbRegistry(fetch), store)
    assert (out.result, out.ack) == ("STORE_UNAVAILABLE", None)
    assert count(db) == 0


def test_without_a_reopen_function_the_store_stays_unavailable(db: sqlite3.Connection) -> None:
    conn = Flaky(db)
    store = ReadingStore(conn, lost_errors=(Lost,))
    conn.cut = True
    assert handle(TOPIC, payload(6), RECEIVED, REG, store).result == "STORE_UNAVAILABLE"
    conn.cut = False  # even if the old connection came back, a lost one is never reused
    assert handle(TOPIC, payload(6), RECEIVED, REG, store).result == "STORE_UNAVAILABLE"
    assert count(db) == 0


def test_other_errors_are_not_hidden(db: sqlite3.Connection) -> None:
    store, _ = make(db)
    with pytest.raises(sqlite3.Error):  # a programming or data error is not an outage and must stay visible
        store.conn.execute("SELECT * FROM no_such_table")
    with pytest.raises(KeyError):
        store.put({})

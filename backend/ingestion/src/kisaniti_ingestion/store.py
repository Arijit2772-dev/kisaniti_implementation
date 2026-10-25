"""Durable store for readings (SQLite for local runs and tests; PostgreSQL in the product).

On PostgreSQL the tables belong to the API's Alembic migration 0002; this module never creates them there.

De-duplication key: reading_id = device_id:boot_id:seq (contract §2). The first copy wins; a later copy with
different measurement content is recorded in reading_conflicts, never silently merged.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from .validate import Rejected

# Fields that describe the measurement itself; `resent` and `dropped_since_boot` may change between copies.
CONTENT_FIELDS = ("measured_at", "time_quality", "uptime_ms", "temperature_c", "humidity_pct", "read_status")

SQLITE_DDL = """
CREATE TABLE IF NOT EXISTS sensor_readings (
  reading_id TEXT PRIMARY KEY,
  device_id TEXT NOT NULL,
  boot_id TEXT NOT NULL,
  seq INTEGER NOT NULL,
  measured_at TEXT,
  received_at TEXT NOT NULL,
  ingest_status TEXT NOT NULL,
  quarantine_reason TEXT,
  content_sha256 TEXT NOT NULL,
  doc TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS sensor_readings_device_time ON sensor_readings (device_id, measured_at);
CREATE TABLE IF NOT EXISTS sensor_reading_conflicts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  reading_id TEXT NOT NULL,
  received_at TEXT NOT NULL,
  doc TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sensor_rejected_messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  received_at TEXT NOT NULL,
  topic TEXT NOT NULL,
  reason TEXT NOT NULL,
  detail TEXT NOT NULL,
  device_id TEXT,
  boot_id TEXT,
  seq INTEGER,
  raw_sha256 TEXT NOT NULL,
  raw BLOB NOT NULL
);
"""


class StoreResult(StrEnum):
    NEW = "NEW"
    DUPLICATE = "DUPLICATE"
    CONFLICT = "CONFLICT"  # same identity, different content: first copy kept, conflict recorded


class Connection(Protocol):
    def execute(self, sql: str, params: Any = ...) -> Any: ...
    def commit(self) -> None: ...
    def close(self) -> None: ...


def content_sha256(doc: dict[str, Any]) -> str:
    body = json.dumps({k: doc.get(k) for k in CONTENT_FIELDS}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode()).hexdigest()


@dataclass
class ReadingStore:
    conn: Connection
    placeholder: str = "?"  # "%s" for psycopg

    def _q(self, sql: str) -> str:
        return sql.replace("?", self.placeholder)

    def put(self, reading: dict[str, Any]) -> StoreResult:
        """Insert once and commit before returning, so an ack sent afterwards means durable storage."""
        csha = content_sha256(reading)
        cur = self.conn.execute(
            self._q(
                "INSERT INTO sensor_readings (reading_id, device_id, boot_id, seq, measured_at, received_at,"
                " ingest_status, quarantine_reason, content_sha256, doc) VALUES (?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT (reading_id) DO NOTHING"
            ),
            (
                reading["reading_id"],
                reading["device_id"],
                reading["boot_id"],
                reading["seq"],
                reading["measured_at"],
                reading["received_at"],
                reading["ingest_status"],
                reading["quarantine_reason"],
                csha,
                json.dumps(reading, separators=(",", ":")),
            ),
        )
        if cur.rowcount == 1:
            self.conn.commit()
            return StoreResult.NEW
        row = self.conn.execute(
            self._q("SELECT content_sha256 FROM sensor_readings WHERE reading_id = ?"), (reading["reading_id"],)
        ).fetchone()
        if row is not None and row[0] == csha:
            self.conn.commit()
            return StoreResult.DUPLICATE
        self.conn.execute(
            self._q("INSERT INTO sensor_reading_conflicts (reading_id, received_at, doc) VALUES (?,?,?)"),
            (reading["reading_id"], reading["received_at"], json.dumps(reading, separators=(",", ":"))),
        )
        self.conn.commit()
        return StoreResult.CONFLICT

    def reject(self, topic: str, payload: bytes, received_at: str, r: Rejected) -> None:
        self.conn.execute(
            self._q(
                "INSERT INTO sensor_rejected_messages (received_at, topic, reason, detail, device_id, boot_id, seq,"
                " raw_sha256, raw) VALUES (?,?,?,?,?,?,?,?,?)"
            ),
            (received_at, topic, r.reason, r.detail, r.device_id, r.boot_id, r.seq, r.raw_sha256, payload),
        )
        self.conn.commit()

    def stored(self, reading_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            self._q("SELECT doc FROM sensor_readings WHERE reading_id = ?"), (reading_id,)
        ).fetchone()
        return None if row is None else _doc(row[0])

    def readings_for(self, device_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            self._q("SELECT doc FROM sensor_readings WHERE device_id = ? ORDER BY measured_at"), (device_id,)
        ).fetchall()
        return [_doc(r[0]) for r in rows]

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for table in ("sensor_readings", "sensor_reading_conflicts", "sensor_rejected_messages"):
            out[table] = int(self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        return out


def _doc(value: Any) -> dict[str, Any]:
    """jsonb columns come back as dicts (PostgreSQL), TEXT columns as strings (SQLite)."""
    return dict(value) if isinstance(value, dict) else dict(json.loads(value))


class SchemaMissingError(RuntimeError):
    pass


def open_store(target: str) -> ReadingStore:
    """`target` is a SQLite file path, ':memory:', or a PostgreSQL DSN (postgresql:// or postgresql+psycopg://).

    A PostgreSQL target must already be migrated (`alembic upgrade head` in backend/)."""
    if target.startswith(("postgresql://", "postgres://", "postgresql+psycopg://")):
        import psycopg  # optional dependency: kisaniti-ingestion[postgres]

        pg = psycopg.connect(target.replace("postgresql+psycopg://", "postgresql://", 1))
        present = pg.execute("SELECT to_regclass('public.sensor_readings') IS NOT NULL").fetchone()
        if present is None or not present[0]:
            pg.close()
            raise SchemaMissingError("table sensor_readings is missing: run `alembic upgrade head` in backend/ first")
        return ReadingStore(pg, "%s")
    conn = sqlite3.connect(target)
    conn.executescript(SQLITE_DDL)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    return ReadingStore(conn)

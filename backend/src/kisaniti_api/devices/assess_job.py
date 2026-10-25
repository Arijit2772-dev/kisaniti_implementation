"""The scheduled storage-assessment job (devices/v1 amendment A1, CHAT-42).

For every active device, assess the hour slots not yet assessed, in order, carrying the persisted hysteresis state, and
store each assessment. Run once (`--once`) or as a loop (`--every 3600`). A system process: it uses the owner
connection and writes each assessment with the device's own FPO. One failing device never stops the others.

    python -m kisaniti_api.devices.assess_job --once
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from kisaniti_core.provenance import Origin
from kisaniti_core.storage import (
    Assignment,
    Crop,
    HysteresisState,
    Level,
    Placement,
    Reading,
    assess,
    assessment_to_contract,
    reading_from_contract,
)
from kisaniti_core.storage.rule import WINDOW, ReadingIndex
from kisaniti_core.timeutil import format_instant
from sqlalchemy import Connection, Engine, text

from ..config import load_settings
from ..db import get_engine
from .placement import assignment_at, readings_for

log = logging.getLogger("kn-assess-job")
SLOT = timedelta(hours=1)
CATCH_UP_CAP = timedelta(days=14)


def floor_slot(t: datetime) -> datetime:
    return t.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


def ceil_slot(t: datetime) -> datetime:
    f = floor_slot(t)
    return f if f == t.astimezone(UTC) else f + SLOT


def load_history(conn: Connection, device_id: str) -> list[Assignment]:
    rows = conn.execute(
        text(
            "SELECT device_id, placement, crop, assigned_at, sample_interval_s FROM device_assignments "
            "WHERE device_id = :d ORDER BY assigned_at"
        ),
        {"d": device_id},
    ).all()
    return [
        Assignment(r.device_id, Placement(r.placement), Crop(r.crop), r.assigned_at, r.sample_interval_s) for r in rows
    ]


def load_readings(conn: Connection, device_id: str, since: datetime) -> list[Reading]:
    rows = conn.execute(
        text("SELECT doc FROM sensor_readings WHERE device_id = :d AND measured_at >= :s"), {"d": device_id, "s": since}
    ).all()
    return [reading_from_contract(r.doc) for r in rows]


def last_stored(conn: Connection, device_id: str) -> tuple[datetime, HysteresisState] | None:
    row = conn.execute(
        text(
            "SELECT assessed_at, hysteresis_level, hysteresis_below_since, hysteresis_assigned_at "
            "FROM storage_assessments WHERE device_id = :d ORDER BY assessed_at DESC LIMIT 1"
        ),
        {"d": device_id},
    ).first()
    if row is None:
        return None
    level = Level(row.hysteresis_level) if row.hysteresis_level else None
    return row.assessed_at, HysteresisState(level, row.hysteresis_below_since, row.hysteresis_assigned_at)


def _store(conn: Connection, device_id: str, fpo_id: str, a: Any) -> int:
    doc = assessment_to_contract(a)
    asg = a.assignment
    res = conn.execute(
        text(
            "INSERT INTO storage_assessments (device_id, fpo_id, assessed_at, assigned_at, category, hysteresis_level, "
            "hysteresis_below_since, hysteresis_assigned_at, doc) VALUES (:d, :f, :t, :ga, :c, :hl, :hb, :ha, "
            "CAST(:doc AS jsonb)) ON CONFLICT (device_id, assessed_at) DO NOTHING"
        ),
        {
            "d": device_id,
            "f": fpo_id,
            "t": a.assessed_at,
            "ga": asg.assigned_at if asg else None,
            "c": a.category.value,
            "hl": a.state.level.value if a.state.level else None,
            "hb": a.state.below_since,
            "ha": a.state.assigned_at,
            "doc": json.dumps(doc),
        },
    )
    return int(res.rowcount)


def assess_device(conn: Connection, device_id: str, fpo_id: str, source_origin: str, now: datetime) -> int:
    """Assess the missing slots of one device inside the caller's transaction. Returns the number of rows stored."""
    conn.execute(text("SELECT pg_advisory_xact_lock(hashtext(:d))"), {"d": device_id})
    origin = Origin.REAL if source_origin == "DEVICE" else Origin.SYNTHETIC
    latest = floor_slot(now)
    history = load_history(conn, device_id)
    last = last_stored(conn, device_id)
    current = assignment_at(history, latest)
    if current is None:  # never assigned (or only in the future): one NO_ASSIGNMENT result for the latest slot
        return _store(conn, device_id, fpo_id, assess([], None, latest, last[1] if last else None, origin))
    start = max(ceil_slot(current.assigned_at), latest - CATCH_UP_CAP)
    if last is not None:
        start = max(start, last[0] + SLOT)
    slots = []
    t = start
    while t <= latest:
        slots.append(t)
        t += SLOT
    if not slots:
        return 0
    readings = load_readings(conn, device_id, slots[0] - WINDOW)
    indexes: dict[datetime, ReadingIndex] = {}
    state = last[1] if last else None
    stored = 0
    for slot in slots:
        asg = assignment_at(history, slot)
        if asg is None:
            a = assess([], None, slot, state, origin)
        else:
            if asg.assigned_at not in indexes:  # only readings placed by this assignment at their own time count
                indexes[asg.assigned_at] = ReadingIndex(readings_for(history, asg, readings))
            a = assess(indexes[asg.assigned_at].window(slot - WINDOW, slot), asg, slot, state, origin)
        state = a.state
        stored += _store(conn, device_id, fpo_id, a)
    return stored


def run_all(engine: Engine, now: datetime | None = None) -> dict[str, int]:
    now = now or datetime.now(UTC)
    with engine.connect() as c:
        devices = c.execute(
            text("SELECT device_id, fpo_id, source_origin FROM devices WHERE active ORDER BY device_id")
        ).all()
    summary = {"devices": len(devices), "stored": 0, "failed": 0}
    for d in devices:
        try:
            with engine.begin() as conn:
                summary["stored"] += assess_device(conn, d.device_id, d.fpo_id, d.source_origin, now)
        except Exception:  # one device must not stop the others; the failure is logged with its device id
            summary["failed"] += 1
            log.exception("assessment failed for device %s", d.device_id)
    return summary


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="kisaniti_api.devices.assess_job")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true")
    mode.add_argument("--every", type=int, metavar="SECONDS")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s", stream=sys.stdout)
    engine = get_engine(load_settings().database_url)
    heartbeat = Path(os.environ.get("KN_HEARTBEAT", "/tmp/assessor.heartbeat"))
    while True:
        summary = run_all(engine)
        heartbeat.touch()  # "a pass completed": the container health check looks at this file's age
        print(json.dumps({"at": format_instant(datetime.now(UTC)), **summary}), flush=True)
        if a.once:
            return 1 if summary["failed"] else 0
        time.sleep(a.every or 3600)


if __name__ == "__main__":
    sys.exit(main())

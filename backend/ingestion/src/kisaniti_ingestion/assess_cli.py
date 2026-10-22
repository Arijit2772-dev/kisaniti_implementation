"""Print the current StorageAssessmentV1 for one device from the stored readings.

Usage: uv run kn-assess --store readings.sqlite3 --assignment assignment.json [--at 2026-10-22T10:00:00.000Z]
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from kisaniti_core.provenance import Origin
from kisaniti_core.storage import assess_series, assessment_to_contract, assignment_from_contract, reading_from_contract
from kisaniti_core.timeutil import parse_instant

from .store import open_store


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--store", required=True)
    ap.add_argument("--assignment", required=True, type=Path)
    ap.add_argument("--at", help="assessment instant (default: now)")
    ap.add_argument("--synthetic-source", action="store_true", help="the registered device is a twin/simulator")
    args = ap.parse_args()
    assignment = assignment_from_contract(json.loads(args.assignment.read_text()))
    docs = open_store(args.store).readings_for(assignment.device_id)
    at = parse_instant(args.at) if args.at else datetime.now(UTC)
    origin = Origin.SYNTHETIC if args.synthetic_source else Origin.REAL
    readings = [reading_from_contract(d) for d in docs]
    # Replay hourly from the assignment so the hysteresis state is the one a scheduled job would have carried.
    times, t = [], assignment.assigned_at
    while t < at:
        times.append(t)
        t += timedelta(hours=1)
    result = assess_series(readings, assignment, [*times, at], origin)[-1]
    print(json.dumps(assessment_to_contract(result), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()

"""Delivery report for bench tests (outage tests H5, one-week run H6): per boot, how many samples arrived, which
sequence numbers are missing, how late they arrived, and how many conflicts or rejects were stored.

Usage: uv run kn-report --store readings.sqlite3 --device kn-node-001 [--since 2026-10-22T00:00:00.000Z]
"""

from __future__ import annotations

import argparse
import json
import statistics
from collections import defaultdict
from typing import Any

from kisaniti_core.timeutil import parse_instant

from .store import open_store


def boot_report(docs: list[dict[str, Any]]) -> dict[str, Any]:
    seqs = sorted(d["seq"] for d in docs)
    missing = sorted(set(range(seqs[0], seqs[-1] + 1)) - set(seqs)) if seqs else []
    delays = [
        (parse_instant(d["received_at"]) - parse_instant(d["measured_at"])).total_seconds()
        for d in docs
        if d["measured_at"] is not None
    ]
    timed = [d["measured_at"] for d in docs if d["measured_at"] is not None]
    return {
        "readings": len(docs),
        "first_seq": seqs[0] if seqs else None,
        "last_seq": seqs[-1] if seqs else None,
        "missing_seq_count": len(missing),
        "missing_seq_ranges": _ranges(missing),
        "first_measured_at": min(timed) if timed else None,
        "last_measured_at": max(timed) if timed else None,
        "untrusted_time": sum(not d["measured_at_trusted"] for d in docs),
        "failed_reads": sum(d["read_status"] != "OK" for d in docs),
        "quarantined": sum(d["ingest_status"] == "QUARANTINED" for d in docs),
        "max_dropped_since_boot": max((d["dropped_since_boot"] for d in docs), default=0),
        "delay_s_median": round(statistics.median(delays), 1) if delays else None,
        "delay_s_max": round(max(delays), 1) if delays else None,
        "arrived_late_over_15min": sum(x > 900 for x in delays),
    }


def _ranges(xs: list[int]) -> list[str]:
    out: list[str] = []
    for x in xs:
        if out and int(out[-1].split("-")[-1]) == x - 1:
            out[-1] = f"{out[-1].split('-')[0]}-{x}"
        else:
            out.append(str(x))
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--store", required=True)
    ap.add_argument("--device", required=True)
    ap.add_argument("--since", help="only readings received at or after this instant")
    a = ap.parse_args()
    store = open_store(a.store)
    docs = store.readings_for(a.device)
    if a.since:
        docs = [d for d in docs if d["received_at"] >= a.since]
    by_boot: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for d in docs:
        by_boot[d["boot_id"]].append(d)
    boots = sorted(by_boot.items(), key=lambda kv: min(x["received_at"] for x in kv[1]))
    print(
        json.dumps(
            {"device": a.device, "store_counts": store.counts(), "boots": {b: boot_report(ds) for b, ds in boots}},
            indent=2,
        )
    )


if __name__ == "__main__":
    main()

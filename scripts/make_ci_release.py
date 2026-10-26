"""Writes a tiny price release with INVENTED numbers, only so that the CI smoke test has a commodity to register lots
against. Never run this against a stack that holds real prices: the release is named as a fixture, but it would still
appear on the prices page. Usage: python3 scripts/make_ci_release.py DIR"""

import csv
import hashlib
import json
import sys
from datetime import date, timedelta
from pathlib import Path

COLS = [
    "row_id", "source_file", "line_no", "market_raw", "variety_raw", "date", "date_inferred", "arrivals_t",
    "arrivals_shared", "min_price", "max_price", "modal_price", "flags",
]  # fmt: skip


def main(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    # 3 mandis x 40 consecutive days of one season: enough index days (10) and recent days (5) for a forecast and a plan
    rows = []
    for day in range(40):
        for k, (mandi, offset) in enumerate(
            (("CI Mandi A APMC", 50), ("CI Mandi B APMC", -30), ("CI Mandi C APMC", 0))
        ):
            n = len(rows) + 1
            rows.append({
                "row_id": f"ci.csv:{n}", "source_file": "ci.csv", "line_no": str(n), "market_raw": mandi,
                "variety_raw": "1121", "date": (date(2025, 9, 10) + timedelta(days=day)).isoformat(),
                "date_inferred": "0", "arrivals_t": "10.000", "arrivals_shared": "0", "min_price": "",
                "max_price": "", "modal_price": f"{3000 + day * 5 + offset}.00", "flags": "",
            })  # fmt: skip
    with open(out / "observations.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLS, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    with open(out / "mandis.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(
            f,
            fieldnames=[
                "name",
                "town",
                "state",
                "latitude",
                "longitude",
                "coordinate_source",
            ],
            lineterminator="\n",
        )
        w.writeheader()
        for mandi in ("CI Mandi A APMC", "CI Mandi B APMC", "CI Mandi C APMC"):
            w.writerow(
                {
                    "name": mandi,
                    "town": "CI",
                    "state": "Punjab",
                    "latitude": "",
                    "longitude": "",
                    "coordinate_source": "",
                }
            )
    manifest = {
        "release_name": "CI FIXTURE (invented numbers, not real prices)",
        "commodity": "Paddy(Basmati)",
        "crop": "PADDY",
        "msp_applies": False,
        "state": "Punjab",
        "rows": len(rows),
        "dated_rows": len(rows),
        "blank_date_rows": 0,
        "flagged_rows": 0,
        "files": {
            n: hashlib.sha256((out / n).read_bytes()).hexdigest()
            for n in ("observations.csv", "mandis.csv")
        },
        "raw_files": {"ci.csv": {"sha256": "0" * 64, "data_rows": len(rows)}},
        "validation_status": "PENDING_HAND_CHECK",
        "validation_note": "CI fixture: invented numbers",
    }
    (out / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
    )


if __name__ == "__main__":
    main(Path(sys.argv[1]))

"""Import of a price dataset release and of the MSP table; status control of a dataset (contract market/v1).

    python -m kisaniti_api.market.import_release import-release --dir PATH_TO_RELEASE
    python -m kisaniti_api.market.import_release import-msp --csv PATH/msp_schedule.csv
    python -m kisaniti_api.market.import_release set-status --dataset NAME --to VALIDATED \
        --note "hand check done by ..."

The import is all or nothing and refuses a release that does not reconcile: every file's SHA-256 must match the
manifest, the row counts must be exactly what the manifest says and rows = dated + blank-date, and every price and
arrival must be an exact decimal string. A release always arrives as PENDING_HAND_CHECK, whatever its manifest
says; only an explicit, logged `set-status` changes that. Nothing is ever updated or deleted: a corrected release
is a new version.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

from kisaniti_core.ids import new_id
from sqlalchemy import Connection, Engine, text

from ..config import load_settings
from ..db import get_engine

NOT_PROBLEMS = {"", "BLANK_DATE_HAS_OWN_ARRIVALS", "ARRIVALS_ROUNDED_3DP"}
PRICE_RE = re.compile(r"^[0-9]+\.[0-9]{2}$")
ARRIVALS_RE = re.compile(r"^[0-9]+\.[0-9]{3}$")
BATCH = 1000


class ImportRefused(Exception):
    """The release does not meet the contract; nothing was written."""


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_release(directory: Path) -> tuple[dict[str, Any], list[dict[str, str]], list[dict[str, str]], str]:
    """Everything that can be checked without a database. Returns manifest, observations, mandis, manifest sha."""
    mpath = directory / "manifest.json"
    if not mpath.exists():
        raise ImportRefused(f"{mpath} is missing")
    manifest = json.loads(mpath.read_text(encoding="utf-8"))
    for name, expected in manifest["files"].items():
        f = directory / name
        if not f.exists() or _sha(f) != expected:
            raise ImportRefused(f"{name} does not match its checksum in the manifest")
    with open(directory / "observations.csv", newline="", encoding="utf-8") as fh:
        obs = list(csv.DictReader(fh))
    with open(directory / "mandis.csv", newline="", encoding="utf-8") as fh:
        mandis = list(csv.DictReader(fh))
    dated = sum(1 for r in obs if r["date_inferred"] == "0")
    blank = sum(1 for r in obs if r["date_inferred"] == "1")
    flagged = sum(1 for r in obs if set(r["flags"].split(";")) - NOT_PROBLEMS)
    got = {"rows": len(obs), "dated_rows": dated, "blank_date_rows": blank, "flagged_rows": flagged}
    for k, v in got.items():
        if manifest[k] != v:
            raise ImportRefused(f"{k}: the manifest says {manifest[k]}, the file has {v}")
    if len(obs) != dated + blank:
        raise ImportRefused("rows are not dated + blank-date rows")
    if sum(f["data_rows"] for f in manifest["raw_files"].values()) != len(obs):
        raise ImportRefused("the rows of the raw files do not add up to the release")
    if len({r["row_id"] for r in obs}) != len(obs):
        raise ImportRefused("duplicate row IDs")
    names = {m["name"] for m in mandis}
    for r in obs:
        if r["market_raw"] not in names:
            raise ImportRefused(f"market {r['market_raw']!r} has no entry in mandis.csv")
        for k in ("min_price", "max_price", "modal_price"):
            if r[k] and not (PRICE_RE.match(r[k]) and Decimal(r[k]) > 0):
                raise ImportRefused(f"{r['row_id']}: {k} {r[k]!r} is not an exact positive decimal with two digits")
        if r["arrivals_t"] and not ARRIVALS_RE.match(r["arrivals_t"]):
            raise ImportRefused(
                f"{r['row_id']}: arrivals {r['arrivals_t']!r} is not an exact decimal with three digits"
            )
        if not r["date"]:
            raise ImportRefused(f"{r['row_id']}: no date")
    return manifest, obs, mandis, _sha(mpath)


def _get_or_create(conn: Connection, table: str, key: str, value: str, new_kind: str, cols: dict[str, Any]) -> str:
    assert table in {"commodities", "mandis"} and key in {"source_name", "name"}
    row = conn.execute(text(f"SELECT id FROM {table} WHERE {key} = :v"), {"v": value}).first()
    if row:
        return str(row[0])
    new = new_id(new_kind)
    names = ", ".join(["id", key, *cols])
    params = ", ".join([":id", ":v", *(f":{c}" for c in cols)])
    conn.execute(text(f"INSERT INTO {table} ({names}) VALUES ({params})"), {"id": new, "v": value, **cols})
    return new


def import_release(engine: Engine, directory: Path) -> dict[str, Any]:
    manifest, obs, mandis, msha = verify_release(directory)
    name = manifest["release_name"]
    with engine.begin() as conn:
        prior = conn.execute(
            text("SELECT id, manifest_sha256 FROM dataset_versions WHERE name = :n"), {"n": name}
        ).first()
        if prior is not None:
            if prior.manifest_sha256 == msha:
                return {"status": "already imported", "dataset_id": prior.id, "name": name}
            raise ImportRefused(
                f"a dataset named {name!r} exists with a different manifest; a corrected release needs a new name"
            )
        cmd = _get_or_create(
            conn,
            "commodities",
            "source_name",
            manifest["commodity"],
            "cmd",
            {"crop": manifest["crop"], "msp_applies": manifest["msp_applies"]},
        )
        mandi_ids: dict[str, str] = {}
        for m in mandis:
            lat = float(m["latitude"]) if m["latitude"] else None
            lon = float(m["longitude"]) if m["longitude"] else None
            mandi_ids[m["name"]] = _get_or_create(
                conn,
                "mandis",
                "name",
                m["name"],
                "mnd",
                {
                    "town": m["town"] or None,
                    "state": m["state"],
                    "latitude": lat,
                    "longitude": lon,
                    "coordinate_source": m["coordinate_source"] or None,
                },
            )
        dsv = new_id("dsv")
        conn.execute(
            text(
                "INSERT INTO dataset_versions (id, name, rows, dated_rows, blank_date_rows, flagged_rows, "
                "manifest_sha256, "
                "manifest, note) VALUES (:i, :n, :r, :d, :b, :f, :s, CAST(:m AS jsonb), :note)"
            ),
            {
                "i": dsv,
                "n": name,
                "r": manifest["rows"],
                "d": manifest["dated_rows"],
                "b": manifest["blank_date_rows"],
                "f": manifest["flagged_rows"],
                "s": msha,
                "m": json.dumps(manifest),
                "note": manifest.get("validation_note"),
            },
        )
        sql = text(
            "INSERT INTO price_observations (dataset_version_id, mandi_id, commodity_id, variety_label, observed_on, "
            "min_price, max_price, modal_price, arrivals_tonnes, date_inferred, arrivals_shared, flags, "
            "source_file, source_line) "
            "VALUES (:ds, :m, :c, :v, :d, :mn, :mx, :mo, :a, :di, :ash, :fl, :sf, :sl)"
        )
        batch: list[dict[str, Any]] = []
        for r in obs:
            batch.append(
                {
                    "ds": dsv,
                    "m": mandi_ids[r["market_raw"]],
                    "c": cmd,
                    "v": r["variety_raw"],
                    "d": r["date"],
                    "mn": Decimal(r["min_price"]) if r["min_price"] else None,
                    "mx": Decimal(r["max_price"]) if r["max_price"] else None,
                    "mo": Decimal(r["modal_price"]) if r["modal_price"] else None,
                    "a": Decimal(r["arrivals_t"]) if r["arrivals_t"] else None,
                    "di": r["date_inferred"] == "1",
                    "ash": r["arrivals_shared"] == "1",
                    "fl": [f for f in r["flags"].split(";") if f],
                    "sf": r["source_file"],
                    "sl": int(r["line_no"]),
                }
            )
            if len(batch) == BATCH:
                conn.execute(sql, batch)
                batch = []
        if batch:
            conn.execute(sql, batch)
        n = conn.execute(
            text("SELECT count(*) FROM price_observations WHERE dataset_version_id = :d"), {"d": dsv}
        ).scalar_one()
        if n != manifest["rows"]:
            raise ImportRefused(f"stored {n} rows, expected {manifest['rows']}")  # rolls the whole import back
    return {
        "status": "imported",
        "dataset_id": dsv,
        "name": name,
        "rows": n,
        "mandis": len(mandi_ids),
        "validation_status": "PENDING_HAND_CHECK",
    }


def import_msp(engine: Engine, csv_path: Path) -> dict[str, int]:
    with open(csv_path, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    added = same = 0
    with engine.begin() as conn:
        for r in rows:
            if r["verified"].strip().lower() != "yes":
                raise ImportRefused(
                    f"{r['marketing_season']} {r['crop']} {r['grade']} is not verified; refusing to import it"
                )
            vals = {
                "c": r["crop"].upper(),
                "g": r["grade"],
                "s": r["marketing_season"],
                "p": Decimal(r["msp_rs_per_quintal"]).quantize(Decimal("0.01")),
                "a": r["announced_on"],
                "u": r["source_url"],
            }
            old = conn.execute(
                text(
                    "SELECT msp_inr_per_quintal FROM msp_entries "
                    "WHERE crop = :c AND grade = :g AND marketing_season = :s"
                ),
                vals,
            ).first()
            if old is None:
                conn.execute(
                    text(
                        "INSERT INTO msp_entries (crop, grade, marketing_season, msp_inr_per_quintal, announced_on, "
                        "source_url, verified) "
                        "VALUES (:c, :g, :s, :p, :a, :u, true)"
                    ),
                    vals,
                )
                added += 1
            elif old[0] != vals["p"]:
                raise ImportRefused(
                    f"{r['marketing_season']} {r['crop']} {r['grade']}: stored {old[0]}, file says {vals['p']}"
                )
            else:
                same += 1
    return {"added": added, "unchanged": same}


ALLOWED = {("PENDING_HAND_CHECK", "VALIDATED"), ("PENDING_HAND_CHECK", "REJECTED"), ("VALIDATED", "REJECTED")}


def set_status(engine: Engine, dataset_name: str, to: str, note: str) -> dict[str, str]:
    if not note.strip():
        raise ImportRefused("a status change needs a note saying who checked what")
    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT id, validation_status FROM dataset_versions WHERE name = :n FOR UPDATE"), {"n": dataset_name}
        ).first()
        if row is None:
            raise ImportRefused(f"no dataset named {dataset_name!r}")
        if (row.validation_status, to) not in ALLOWED:
            raise ImportRefused(f"{row.validation_status} -> {to} is not an allowed change")
        conn.execute(
            text("UPDATE dataset_versions SET validation_status = :t, note = :note WHERE id = :i"),
            {"t": to, "note": note, "i": row.id},
        )
        conn.execute(
            text(
                "INSERT INTO dataset_status_log (dataset_version_id, from_status, to_status, note) "
                "VALUES (:i, :f, :t, :note)"
            ),
            {"i": row.id, "f": row.validation_status, "t": to, "note": note},
        )
    return {"dataset": dataset_name, "from": row.validation_status, "to": to}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="kisaniti_api.market.import_release")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("import-release").add_argument("--dir", type=Path, required=True)
    sub.add_parser("import-msp").add_argument("--csv", type=Path, required=True)
    s = sub.add_parser("set-status")
    s.add_argument("--dataset", required=True)
    s.add_argument("--to", choices=["VALIDATED", "REJECTED"], required=True)
    s.add_argument("--note", required=True)
    a = ap.parse_args(argv)
    engine = get_engine(load_settings().database_url)
    try:
        out = (
            import_release(engine, a.dir)
            if a.cmd == "import-release"
            else import_msp(engine, a.csv)
            if a.cmd == "import-msp"
            else set_status(engine, a.dataset, a.to, a.note)
        )
    except ImportRefused as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return 2
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())

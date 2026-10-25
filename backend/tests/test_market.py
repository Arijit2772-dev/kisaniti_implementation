# ruff: noqa: F811, F401  (fixtures imported from test_access by name: pytest uses them as arguments)
"""Market reference data (contract market/v1) on real PostgreSQL: the release import and its refusals, the MSP import,
dataset status control, and the read-only API. The releases built here are TEST FIXTURES with invented numbers; they
only exercise the mechanics and are named as such. Real prices are imported by the live script, not by these tests."""

from __future__ import annotations

import csv
import hashlib
import json
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from kisaniti_api.main import app
from kisaniti_api.market import import_release as imp
from tests.test_access import Fpo, access_env, as_app, clean_up_access_data, two
from tests.test_org import call

CONTRACTS = Path(__file__).resolve().parents[2] / "contracts"
OBS_COLS = [
    "row_id",
    "source_file",
    "line_no",
    "market_raw",
    "variety_raw",
    "date",
    "date_inferred",
    "arrivals_t",
    "arrivals_shared",
    "min_price",
    "max_price",
    "modal_price",
    "flags",
]


def valid(name: str, doc: Any) -> None:
    reg: Registry = Registry()
    for d in (CONTRACTS / "market" / "v1", CONTRACTS / "shared" / "v1"):
        for f in d.glob("*.schema.json"):
            body = json.loads(f.read_text())
            reg = reg.with_resource(body["$id"], Resource.from_contents(body))
    sid = json.loads((CONTRACTS / "market" / "v1" / f"{name}.schema.json").read_text())["$id"]
    errors = [e.message for e in Draft202012Validator({"$ref": sid}, registry=reg).iter_errors(doc)]
    assert errors == [], errors


@pytest.fixture(autouse=True, scope="module")
def clean_market(engine: Engine) -> Any:
    sql = "TRUNCATE price_observations, dataset_status_log, dataset_versions, msp_entries, mandis, commodities CASCADE"
    with engine.begin() as c:
        c.execute(text(sql))
    yield
    with engine.begin() as c:
        c.execute(text(sql))


def row(n: int, mandi: str, variety: str, day: str, modal: str, **kw: Any) -> dict[str, str]:
    base = {
        "row_id": f"2025-11_punjab.csv:{n}",
        "source_file": "2025-11_punjab.csv",
        "line_no": str(n),
        "market_raw": mandi,
        "variety_raw": variety,
        "date": day,
        "date_inferred": "0",
        "arrivals_t": "100.000",
        "arrivals_shared": "0",
        "min_price": "",
        "max_price": "",
        "modal_price": modal,
        "flags": "",
    }
    base.update({k: str(v) for k, v in kw.items()})
    return base


def fixture_rows() -> list[dict[str, str]]:
    return [
        row(1, "Alpha APMC", "1121", "2024-11-05", "3500.00", min_price="3300.00", max_price="3700.00"),
        row(
            2,
            "Alpha APMC",
            "Basmati 1509",
            "2024-11-05",
            "2800.00",
            date_inferred="1",
            arrivals_shared="1",
            flags="",
            arrivals_t="150.500",
        ),
        row(
            3,
            "Alpha APMC",
            "1121",
            "2025-11-12",
            "3800.00",
            min_price="3300.00",
            max_price="4030.00",
            arrivals_t="7159.000",
        ),
        row(4, "Beta (Yard 2) APMC", "1121", "2025-11-12", "3790.00", max_price="", flags="MAX_MISSING"),
        row(
            5,
            "Beta (Yard 2) APMC",
            "1121",
            "2025-11-13",
            "3810.00",
            flags="ARRIVALS_ROUNDED_3DP",
            arrivals_t="1400.000",
        ),
        row(
            6, "Beta APMC", "Sugandha", "2025-11-13", "4560.00", date_inferred="1", flags="BLANK_DATE_HAS_OWN_ARRIVALS"
        ),
    ]


def make_release(tmp: Path, name: str | None = None, obs: list[dict[str, str]] | None = None, **tamper: Any) -> Path:
    rows = obs if obs is not None else fixture_rows()
    name = name or f"TEST FIXTURE {uuid.uuid4().hex[:8]}"
    d = tmp / uuid.uuid4().hex
    d.mkdir()
    with open(d / "observations.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=OBS_COLS, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    names = sorted({r["market_raw"] for r in rows})
    with open(d / "mandis.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(
            f, fieldnames=["name", "town", "state", "latitude", "longitude", "coordinate_source"], lineterminator="\n"
        )
        w.writeheader()
        for n in names:
            w.writerow(
                {
                    "name": n,
                    "town": n.split()[0],
                    "state": "Punjab",
                    "latitude": "30.5" if n.startswith("Alpha") else "",
                    "longitude": "76.1" if n.startswith("Alpha") else "",
                    "coordinate_source": "test fixture" if n.startswith("Alpha") else "",
                }
            )
    dated = sum(1 for r in rows if r["date_inferred"] == "0")
    blank = len(rows) - dated
    flagged = sum(1 for r in rows if set(r["flags"].split(";")) - imp.NOT_PROBLEMS)
    manifest = {
        "release_name": name,
        "commodity": "Paddy(Basmati)",
        "crop": "PADDY",
        "msp_applies": False,
        "state": "Punjab",
        "rows": len(rows),
        "dated_rows": dated,
        "blank_date_rows": blank,
        "flagged_rows": flagged,
        "files": {n: hashlib.sha256((d / n).read_bytes()).hexdigest() for n in ("observations.csv", "mandis.csv")},
        "raw_files": {"2025-11_punjab.csv": {"sha256": "a" * 64, "data_rows": len(rows)}},
        "validation_status": "VALIDATED",
        "validation_note": "test fixture: invented numbers",
    }
    manifest.update(tamper)
    (d / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    return d


def counts(engine: Engine) -> dict[str, int]:
    with engine.connect() as c:
        return {
            t: int(c.execute(text(f"SELECT count(*) FROM {t}")).scalar_one())
            for t in ("price_observations", "dataset_versions", "mandis", "commodities")
        }


def sign_in(f: Fpo) -> Any:
    class S:
        def get(self, path: str) -> Any:
            return f.admin.client.get(f"/api/v1{path}")

    return S()


# ---- import -------------------------------------------------------------------------------------------------------
def test_a_release_imports_all_or_nothing_and_always_arrives_unvalidated(engine: Engine, tmp_path: Path) -> None:
    out = imp.import_release(engine, make_release(tmp_path))
    assert (
        out["status"] == "imported" and out["rows"] == 6 and out["validation_status"] == "PENDING_HAND_CHECK"
    )  # the manifest claimed VALIDATED
    n = counts(engine)
    assert n["price_observations"] == 6 and n["dataset_versions"] == 1 and n["mandis"] == 3 and n["commodities"] == 1
    again = imp.import_release(engine, tmp_path / sorted(p.name for p in tmp_path.iterdir())[0])
    assert again["status"] == "already imported" and counts(engine) == n  # idempotent


@pytest.mark.parametrize(
    ("what", "break_it"),
    [
        (
            "a file that does not match its checksum",
            lambda d: (d / "observations.csv").write_text((d / "observations.csv").read_text() + "x\n"),
        ),
        ("a missing manifest", lambda d: (d / "manifest.json").unlink()),
    ],
)
def test_tampered_or_incomplete_releases_are_refused_and_nothing_is_written(
    engine: Engine, tmp_path: Path, what: str, break_it: Any
) -> None:
    before = counts(engine)
    d = make_release(tmp_path)
    break_it(d)
    with pytest.raises(imp.ImportRefused):
        imp.import_release(engine, d)
    assert counts(engine) == before, what


@pytest.mark.parametrize(
    ("tamper", "message"),
    [
        ({"rows": 7}, "rows"),
        ({"dated_rows": 5}, "dated_rows"),
        ({"flagged_rows": 3}, "flagged_rows"),
        ({"raw_files": {"x": {"sha256": "a" * 64, "data_rows": 5}}}, "raw files"),
    ],
)
def test_counts_that_do_not_reconcile_are_refused(
    engine: Engine, tmp_path: Path, tamper: dict[str, Any], message: str
) -> None:
    before = counts(engine)
    with pytest.raises(imp.ImportRefused, match=message):
        imp.import_release(engine, make_release(tmp_path, **tamper))
    assert counts(engine) == before


@pytest.mark.parametrize(
    ("column", "value", "message"),
    [
        ("modal_price", "3800.000", "exact positive decimal"),
        ("modal_price", "0.00", "exact positive decimal"),
        ("modal_price", "-5.00", "exact positive decimal"),
        ("min_price", "3300.5", "exact positive decimal"),
        ("arrivals_t", "100.00", "three digits"),
        ("date", "", "no date"),
    ],
)
def test_prices_and_arrivals_must_be_exact_decimals_and_a_missing_price_is_never_zero(
    engine: Engine, tmp_path: Path, column: str, value: str, message: str
) -> None:
    rows = fixture_rows()
    rows[0][column] = value
    before = counts(engine)
    with pytest.raises(imp.ImportRefused, match=message):
        imp.import_release(engine, make_release(tmp_path, obs=rows))
    assert counts(engine) == before


def test_a_row_for_an_unlisted_market_and_a_changed_release_under_the_same_name_are_refused(
    engine: Engine, tmp_path: Path
) -> None:
    d = make_release(tmp_path)
    with open(d / "mandis.csv", newline="", encoding="utf-8") as f:
        kept = [r for r in csv.DictReader(f) if not r["name"].startswith("Beta APMC")]
    with open(d / "mandis.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(kept[0]), lineterminator="\n")
        w.writeheader()
        w.writerows(kept)
    m = json.loads((d / "manifest.json").read_text())
    m["files"]["mandis.csv"] = hashlib.sha256((d / "mandis.csv").read_bytes()).hexdigest()
    (d / "manifest.json").write_text(json.dumps(m))
    with pytest.raises(imp.ImportRefused, match=r"no entry in mandis\.csv"):
        imp.import_release(engine, d)
    name = f"TEST FIXTURE same name {uuid.uuid4().hex[:6]}"
    imp.import_release(engine, make_release(tmp_path, name=name))
    with pytest.raises(imp.ImportRefused, match="different manifest"):
        imp.import_release(engine, make_release(tmp_path, name=name, obs=fixture_rows()[:4]))


# ---- the API -----------------------------------------------------------------------------------------------------
def test_prices_come_back_exact_with_their_doubts_attached(
    engine: Engine, tmp_path: Path, two: tuple[Fpo, Fpo]
) -> None:
    a, _ = two
    imp.import_release(engine, make_release(tmp_path))
    rows = call(a.admin, "GET", "/prices?limit=100").json()
    assert len(rows) >= 6
    for r in rows:
        valid("price", r)
    mine = [r for r in rows if r["observed_on"] == "2025-11-12" and r["variety_label"] == "1121"]
    assert {r["modal_inr_per_quintal"] for r in mine} == {"3800.00", "3790.00"}
    missing = next(r for r in rows if "MAX_MISSING" in r["flags"])
    assert (
        missing["max_inr_per_quintal"] is None and missing["min_inr_per_quintal"] is None
    )  # missing stays missing, never zero
    assert any(r["date_inferred"] and r["arrivals_shared"] for r in rows)
    dates = [r["observed_on"] for r in rows]
    assert dates == sorted(dates, reverse=True)
    assert all(
        Decimal(r["modal_inr_per_quintal"]) == Decimal(r["modal_inr_per_quintal"]).quantize(Decimal("0.01"))
        for r in rows
    )


def test_price_filters_and_paging(engine: Engine, tmp_path: Path, two: tuple[Fpo, Fpo]) -> None:
    a, _ = two
    imp.import_release(engine, make_release(tmp_path))
    mandis = {m["name"]: m["id"] for m in call(a.admin, "GET", "/mandis").json()}
    assert {"Alpha APMC", "Beta APMC", "Beta (Yard 2) APMC"} <= set(mandis)
    alpha = call(a.admin, "GET", f"/prices?mandi_id={mandis['Alpha APMC']}").json()
    assert alpha and {r["mandi_id"] for r in alpha} == {mandis["Alpha APMC"]}
    assert {r["variety_label"] for r in call(a.admin, "GET", "/prices?variety=Sugandha").json()} == {"Sugandha"}
    window = call(a.admin, "GET", "/prices?from=2025-11-01&to=2025-11-12").json()
    assert window and all("2025-11-01" <= r["observed_on"] <= "2025-11-12" for r in window)
    assert len(call(a.admin, "GET", "/prices?limit=2").json()) == 2
    assert (
        call(a.admin, "GET", "/prices?limit=0").status_code == 422
        and call(a.admin, "GET", "/prices?mandi_id=bad").status_code == 422
    )
    assert call(a.admin, "GET", "/mandis?q=yard").json()[0]["name"] == "Beta (Yard 2) APMC"


def test_reference_endpoints_match_the_contract_and_need_a_login(
    engine: Engine, tmp_path: Path, two: tuple[Fpo, Fpo]
) -> None:
    a, _ = two
    imp.import_release(engine, make_release(tmp_path))
    for path, schema in (("/commodities", "commodity"), ("/mandis", "mandi"), ("/datasets", "dataset")):
        data = call(a.admin, "GET", path).json()
        assert data
        for item in data:
            valid(schema, item)
    summary = call(a.admin, "GET", "/prices/summary").json()
    valid("price_summary", summary)
    assert summary["dataset"]["validation_status"] == "PENDING_HAND_CHECK" and summary["rows"] >= 6
    assert {"2024-25", "2025-26"} <= {g["season"] for g in summary["by_season_variety"]} and summary[
        "days_since_newest"
    ] > 0
    anon = TestClient(app, raise_server_exceptions=False)
    for path in ("/commodities", "/mandis", "/msp", "/datasets", "/prices", "/prices/summary"):
        assert anon.get(f"/api/v1{path}").status_code == 401, path


# ---- status control and rejected datasets -------------------------------------------------------------------------
def test_status_changes_need_a_note_follow_the_allowed_paths_and_are_logged(engine: Engine, tmp_path: Path) -> None:
    name = f"TEST FIXTURE status {uuid.uuid4().hex[:6]}"
    imp.import_release(engine, make_release(tmp_path, name=name))
    with pytest.raises(imp.ImportRefused, match="note"):
        imp.set_status(engine, name, "VALIDATED", "  ")
    assert imp.set_status(engine, name, "VALIDATED", "hand check of 56 rows done (test)")["to"] == "VALIDATED"
    assert imp.set_status(engine, name, "REJECTED", "later found wrong (test)")["to"] == "REJECTED"
    with pytest.raises(imp.ImportRefused, match="not an allowed change"):
        imp.set_status(engine, name, "VALIDATED", "reviving it")
    with engine.connect() as c:
        log = c.execute(
            text(
                "SELECT from_status, to_status FROM dataset_status_log l "
                "JOIN dataset_versions d ON d.id = l.dataset_version_id "
                "WHERE d.name = :n ORDER BY l.id"
            ),
            {"n": name},
        ).all()
    assert [tuple(x) for x in log] == [("PENDING_HAND_CHECK", "VALIDATED"), ("VALIDATED", "REJECTED")]
    with pytest.raises(DBAPIError, match="append-only"), engine.begin() as c:
        c.execute(text("DELETE FROM dataset_status_log"))


def test_a_rejected_dataset_is_hidden_from_prices_and_the_summary_even_at_the_database(
    engine: Engine, tmp_path: Path, two: tuple[Fpo, Fpo]
) -> None:
    a, _ = two
    name = f"TEST FIXTURE rejected {uuid.uuid4().hex[:6]}"
    imp.import_release(
        engine, make_release(tmp_path, name=name, obs=[row(1, "Zed APMC", "1121", "2026-01-05", "4999.00")])
    )
    assert any(r["modal_inr_per_quintal"] == "4999.00" for r in call(a.admin, "GET", "/prices?limit=2000").json())
    imp.set_status(engine, name, "REJECTED", "test: found wrong")
    assert not any(r["modal_inr_per_quintal"] == "4999.00" for r in call(a.admin, "GET", "/prices?limit=2000").json())
    assert as_app(engine, a.fpo_id, "SELECT 1 FROM price_observations WHERE modal_price = 4999.00") == []
    with engine.connect() as c:  # the owner still has the history
        assert c.execute(text("SELECT count(*) FROM price_observations WHERE modal_price = 4999.00")).scalar_one() == 1
    assert name in {
        d["name"] for d in call(a.admin, "GET", "/datasets").json()
    }  # the dataset list shows it, as REJECTED
    assert call(a.admin, "GET", "/prices/summary").json()["dataset"]["name"] != name


def test_tenants_cannot_change_reference_data_and_nothing_is_ever_edited(
    engine: Engine, tmp_path: Path, two: tuple[Fpo, Fpo]
) -> None:
    a, _ = two
    imp.import_release(engine, make_release(tmp_path))
    for sql in (
        "INSERT INTO mandis (id, name) VALUES ('mnd_00000000000000000000000009', 'x')",
        "UPDATE mandis SET name = 'y'",
        "DELETE FROM price_observations",
        "UPDATE price_observations SET modal_price = 1",
        "UPDATE dataset_versions SET validation_status = 'VALIDATED'",
        "INSERT INTO msp_entries VALUES ('PADDY','all','KMS 2030-31',1,now(),'x',true)",
    ):
        with pytest.raises(DBAPIError):
            as_app(engine, a.fpo_id, sql)
    for sql in ("UPDATE price_observations SET modal_price = 1", "DELETE FROM price_observations"):
        with pytest.raises(DBAPIError, match="append-only"), engine.begin() as c:
            c.execute(text(sql))
    with pytest.raises(IntegrityError), engine.begin() as c:  # a dataset whose counts do not add up cannot be stored
        c.execute(
            text(
                "INSERT INTO dataset_versions (id, name, rows, dated_rows, blank_date_rows, flagged_rows, "
                "manifest_sha256, manifest) "
                "VALUES (:i, 'bad', 10, 5, 4, 0, :s, '{}')"
            ),
            {"i": "dsv_00000000000000000000000009", "s": "a" * 64},
        )


# ---- MSP ----------------------------------------------------------------------------------------------------------
MSP_HEADER = "crop,grade,marketing_season,msp_rs_per_quintal,announced_on,source_url,verified\n"


def test_msp_import_is_exact_idempotent_and_refuses_unverified_or_conflicting_values(
    engine: Engine, tmp_path: Path, two: tuple[Fpo, Fpo]
) -> None:
    a, _ = two
    f = tmp_path / "msp.csv"
    f.write_text(
        MSP_HEADER
        + "paddy,common,KMS 2025-26,2369,2025-05-28,https://www.pib.gov.in/x,yes\nwheat,all,RMS 2026-27,2585,2025-10-01,https://www.pib.gov.in/y,yes\n"
    )
    assert imp.import_msp(engine, f) == {"added": 2, "unchanged": 0}
    assert imp.import_msp(engine, f) == {"added": 0, "unchanged": 2}
    rows = call(a.admin, "GET", "/msp").json()
    for r in rows:
        valid("msp", r)
    assert {(r["crop"], r["marketing_season"], r["msp_inr_per_quintal"]) for r in rows} >= {
        ("PADDY", "KMS 2025-26", "2369.00"),
        ("WHEAT", "RMS 2026-27", "2585.00"),
    }
    f.write_text(MSP_HEADER + "paddy,common,KMS 2025-26,2370,2025-05-28,https://www.pib.gov.in/x,yes\n")
    with pytest.raises(imp.ImportRefused, match="stored"):
        imp.import_msp(engine, f)
    f.write_text(MSP_HEADER + "paddy,grade_a,KMS 2025-26,2389,2025-05-28,https://www.pib.gov.in/x,no\n")
    with pytest.raises(imp.ImportRefused, match="not verified"):
        imp.import_msp(engine, f)

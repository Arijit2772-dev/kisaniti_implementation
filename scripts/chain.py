#!/usr/bin/env python3
"""The one-command chain (slice 7, docs: kisaniti/docs/design/SLICE7_ONE_COMMAND_CHAIN.md).

    python3 scripts/chain.py [--api http://localhost:8000] [--report chain_report.json] [--sabotage ledger]

Runs the five stages of the product flow on two new fictional FPOs through the frozen HTTP API, checks every step
against an independent recomputation (scripts/chainlib.py), stops at the first broken step and exits 1 with the step,
what was expected, what happened and the request ID. Standard library only. Needs `docker compose` for the operator
steps (creating an FPO, importing the fixture prices, feeding the simulator, running the assessor); run it from the
repository root with the stack up (`docker compose up --build -d --wait`)."""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
import chainlib as cl

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_CONTRACTS = [
    "shared/v1",
    "access/v1",
    "devices/v1",
    "org/v1",
    "market/v1",
    "plan/v1",
    "approval/v1",
    "messaging/v1",
    "sales/v1",
    "handover/v1",
]
CENT = Decimal("0.01")


class ChainFailure(Exception):
    def __init__(self, step: str, expected: str, got: str, request_id: str = "") -> None:
        super().__init__(f"{step}: expected {expected}; got {got}")
        self.step, self.expected, self.got, self.request_id = (
            step,
            expected,
            got,
            request_id,
        )


# ---- HTTP -----------------------------------------------------------------------------------------------------------
class Client:
    """One signed-in browser: keeps the cookie, sends the CSRF header on changes."""

    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self.csrf = ""
        self.step = "?"
        self.seen_text: list[str] = []  # every response body, to check that no phone number leaked

    def raw(self, method: str, path: str, body: Any = None, key: str | None = None) -> tuple[int, Any, str]:
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(f"{self.base}/api/v1{path}", data=data, method=method)
        if data is not None:
            req.add_header("content-type", "application/json")
        if method != "GET" and self.csrf:
            req.add_header("x-csrf-token", self.csrf)
        if key:
            req.add_header("idempotency-key", key)
        try:
            with self.opener.open(req, timeout=60) as r:
                text, status, rid = (
                    r.read().decode(),
                    r.status,
                    r.headers.get("x-request-id", ""),
                )
        except urllib.error.HTTPError as e:
            text, status, rid = (
                e.read().decode(),
                e.code,
                e.headers.get("x-request-id", ""),
            )
        self.seen_text.append(text)
        return status, (json.loads(text) if text else None), rid

    def call(
        self,
        method: str,
        path: str,
        body: Any = None,
        key: str | None = None,
        expect: int | tuple[int, ...] = 200,
        code: str | None = None,
    ) -> Any:
        status, doc, rid = self.raw(method, path, body, key)
        ok = status in (expect if isinstance(expect, tuple) else (expect,))
        issues = doc.get("issues") if isinstance(doc, dict) else None
        got_code = issues[0].get("code") if issues and isinstance(issues[0], dict) else None
        if not ok or (code is not None and got_code != code):
            want = f"{method} {path} -> {expect}" + (f" with issue {code}" if code else "")
            raise ChainFailure(
                self.step,
                want,
                f"{status}" + (f" issue {got_code}" if got_code else "") + (f" {str(doc)[:300]}" if not ok else ""),
                rid,
            )
        return doc

    def login(self, username: str, password: str) -> dict[str, Any]:
        self.csrf = ""
        doc = self.call("POST", "/auth/login", {"username": username, "password": password})
        self.csrf = doc["csrf_token"]
        return doc  # type: ignore[no-any-return]


def check(step: str, cond: bool, expected: str, got: Any = "") -> None:
    if not cond:
        raise ChainFailure(step, expected, str(got)[:400])


def eq(step: str, got: Any, want: Any, what: str) -> None:
    if got != want:
        raise ChainFailure(step, f"{what} = {want!r}", f"{got!r}")


# ---- operator steps (Docker) ----------------------------------------------------------------------------------------
def operator(
    args: list[str],
    stdin: str | None = None,
    env: dict[str, str] | None = None,
    service: str = "backend",
) -> str:
    cmd = ["docker", "compose", "exec", "-T"]
    for k, v in (env or {}).items():
        cmd += ["-e", f"{k}={v}"]
    r = subprocess.run([*cmd, service, *args], input=stdin, capture_output=True, text=True, cwd=ROOT)
    if r.returncode != 0:
        raise ChainFailure(
            "operator",
            f"`docker compose exec {service} {' '.join(args[:3])}` succeeds",
            f"exit {r.returncode}: {r.stderr.strip()[-300:]}",
        )
    return r.stdout


FEED = """
import os
from datetime import UTC, datetime, timedelta
from kisaniti_ingestion.registry import DbRegistry, postgres_fetcher
from kisaniti_ingestion.service import handle
from kisaniti_ingestion.simulator import SCENARIOS, InProcessLink, SimDevice, run_simulation
from kisaniti_ingestion.store import open_store
dsn = os.environ["KN_DSN"]
store, reg = open_store(dsn), DbRegistry(postgres_fetcher(dsn), ttl_s=0, miss_s=0)
now = datetime.now(UTC).replace(microsecond=0)
dev = SimDevice(os.environ["DEV"], InProcessLink(lambda t, p, at: handle(t, p, at, reg, store).ack, lambda: now))
run_simulation(dev, SCENARIOS["humid_rise"], 7, "d3a0b007", now - timedelta(hours=30), 360)
print(len(dev.buffer))
"""


def feed_readings(dev: str) -> str:
    """Send the simulator's readings through the real ingestion code. It runs on this machine, like
    scripts/demo_storage.sh: it needs jsonschema and psycopg (backend/.venv has them) and reaches PostgreSQL on the
    published port. Override with KN_FEED_PYTHON and KN_DSN."""
    venv = ROOT / "backend" / ".venv" / "bin" / "python"
    py = os.environ.get("KN_FEED_PYTHON") or (str(venv) if venv.exists() else sys.executable)
    dsn = os.environ.get("KN_DSN", "postgresql://kisaniti:kisaniti-local-only@localhost:5433/kisaniti")
    env = {**os.environ, "DEV": dev, "KN_DSN": dsn, "PYTHONPATH": f"{ROOT}/backend/ingestion/src:{ROOT}/core/src"}
    r = subprocess.run([py, "-"], input=FEED, capture_output=True, text=True, env=env, cwd=ROOT)
    if r.returncode != 0:
        raise ChainFailure("sensor", "the simulator feed succeeds", f"exit {r.returncode}: {r.stderr.strip()[-300:]}")
    return r.stdout.strip()


# ---- the chain ------------------------------------------------------------------------------------------------------
class Run:
    def __init__(self, base: str, sabotage: str | None) -> None:
        self.id = uuid.uuid4().hex[:8]
        self.base, self.sabotage = base, sabotage
        self.a, self.b = Client(base), Client(base)
        self.x: dict[str, Any] = {}  # what the steps hand to each other
        self.events: dict[
            str, list[tuple[str, int]]
        ] = {}  # the ledger events each lot must have, built as the chain acts
        self.today = datetime.now(ZoneInfo("Asia/Kolkata")).date().isoformat()
        self.jn = 0

    def expect_event(self, lot: str, kind: str, delta_g: int) -> None:
        self.events.setdefault(lot, []).append((kind, delta_g))

    def j(self) -> str:
        self.jn += 1
        return f"J/CHAIN/{self.id}/{self.jn}"


def step_preflight(r: Run) -> None:
    c = r.a
    req = urllib.request.Request(f"{r.base}/health/ready")
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = json.loads(resp.read().decode())
    eq("preflight", body.get("status"), "READY", "health/ready status")
    meta = c.call("GET", "/meta")
    missing = [x for x in EXPECTED_CONTRACTS if x not in meta["contracts"]]
    check(
        "preflight",
        not missing,
        f"contracts {EXPECTED_CONTRACTS} all listed",
        f"missing {missing}",
    )
    r.x["contracts"] = meta["contracts"]
    r.x["mode"] = meta["mode"]


def step_fpos(r: Run) -> None:
    for tag, client in (("a", r.a), ("b", r.b)):
        out = json.loads(
            operator(
                [
                    "python",
                    "-m",
                    "kisaniti_api.access.cli",
                    "create-fpo",
                    "--name",
                    f"Chain FPO {tag.upper()} (fictional) {r.id}",
                    "--admin-username",
                    f"chain.{tag}.{r.id}",
                    "--synthetic",
                ]
            )
        )
        pw = "chain-" + uuid.uuid4().hex
        first = Client(r.base)
        first.login(out["username"], out["temporary_password"])
        first.call(
            "POST",
            "/auth/change-password",
            {"current_password": out["temporary_password"], "new_password": pw},
            expect=204,
        )
        p = client.login(out["username"], pw)
        check(
            "fpos",
            p["role"] == "FPO_ADMIN" and p["fpo_id"] == out["fpo_id"],
            "an FPO admin of the new FPO",
            p,
        )
        r.x[f"fpo_{tag}"], r.x[f"admin_{tag}"] = out["fpo_id"], p["user_id"]
        r.x[f"login_{tag}"] = (out["username"], pw)
    check("fpos", r.x["fpo_a"] != r.x["fpo_b"], "two different FPOs")


def step_data(r: Run) -> None:
    c = r.a
    if not c.call("GET", "/commodities"):  # no price data in this stack: import the CI fixture (invented numbers)
        with tempfile.TemporaryDirectory() as d:
            subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "make_ci_release.py"),
                    f"{d}/release",
                ],
                check=True,
                cwd=ROOT,
            )
            subprocess.run(
                [
                    "docker",
                    "compose",
                    "cp",
                    f"{d}/release",
                    "backend:/tmp/chain_release",
                ],
                check=True,
                cwd=ROOT,
                capture_output=True,
            )
        operator(
            [
                "python",
                "-m",
                "kisaniti_api.market.import_release",
                "import-release",
                "--dir",
                "/tmp/chain_release",
            ]
        )
        r.x["seeded_fixture"] = True
    doc = c.call("GET", "/forecasts?variety=1121")
    eq(
        "data",
        doc["label"],
        "Baseline forecast, not validated on held-out data",
        "the forecast label",
    )
    eq(
        "data",
        (doc["signal_verdict"], doc["calibration"]),
        ("NOT_ASSESSED", "NOT_VERIFIED"),
        "what the forecast claims",
    )
    tail = doc["series_tail"][-1]
    eq("data", doc["origin_date"], tail["date"], "origin date = newest index day")
    naive = next(m for m in doc["methods"] if m["method"] == "NAIVE")["horizons"]
    last = Decimal(tail["index_inr_per_quintal"]).quantize(CENT, rounding=ROUND_HALF_EVEN)
    for h in naive:
        eq(
            "data",
            Decimal(h["point_inr_per_quintal"]),
            last,
            f"naive point at {h['h']} days (the newest index, rounded half-even)",
        )
        eq(
            "data",
            h["target_date"],
            (datetime.fromisoformat(doc["origin_date"]).date() + timedelta(days=h["h"])).isoformat(),
            "target date",
        )
    eligible = [m for m in doc["candidate_mandis"] if m["status"] == "ELIGIBLE"]
    check(
        "data",
        len(eligible) >= 2,
        "at least two mandis with enough recent prices",
        [m["name"] for m in doc["candidate_mandis"]],
    )
    r.x["forecast"] = doc
    r.x["mandis"] = [(m["mandi_id"], m["name"], Decimal(m["basis_inr_per_quintal"])) for m in eligible[:2]]
    r.x["dataset_status"] = doc["dataset"]["validation_status"]


def step_stock(r: Run) -> None:
    a, b = r.a, r.b
    cmd = next(x for x in a.call("GET", "/commodities") if x["source_name"] == "Paddy(Basmati)")["id"]
    members = []
    for i, lang in enumerate(("pa", "hi", "en"), start=1):
        m = a.call(
            "POST",
            "/members",
            {
                "display_name": f"Chain farmer {i} (fictional)",
                "village": "Patran",
                "preferred_language": lang,
            },
            expect=201,
        )
        members.append(m)
    loc = a.call(
        "POST",
        "/locations",
        {"name": "Chain centre (fictional)", "kind": "COLLECTION_CENTRE"},
        expect=201,
    )
    lots = []
    for m, kgs, variety in (
        (members[0], "1250.000", "1121"),
        (members[1], "800.000", "1121"),
        (members[2], "500.000", "1121"),
        (members[0], "300.000", "Basmati 1509"),
    ):
        lot = a.call(
            "POST",
            "/lots",
            {
                "member_id": m["id"],
                "commodity_id": cmd,
                "source_variety": variety,
                "registered_quantity_kg": kgs,
                "location_id": loc["id"],
            },
            key=f"chain-lot-{uuid.uuid4().hex}",
            expect=201,
        )
        eq(
            "stock",
            lot["balance"],
            {
                "on_hand_kg": kgs,
                "reserved_kg": "0.000",
                "available_kg": kgs,
                "sold_kg": "0.000",
            },
            "the balance of a new lot",
        )
        eq("stock", lot["origin"], "SYNTHETIC", "origin")
        r.expect_event(lot["id"], "REGISTERED", cl.grams(kgs))
        lots.append(lot)
    mb = b.call(
        "POST",
        "/members",
        {
            "display_name": f"Chain B farmer (fictional) {r.id}",
            "preferred_language": "en",
        },
        expect=201,
    )
    lb = b.call(
        "POST",
        "/lots",
        {
            "member_id": mb["id"],
            "commodity_id": cmd,
            "source_variety": "1121",
            "registered_quantity_kg": "1000.000",
        },
        expect=201,
    )
    r.x.update(
        commodity=cmd,
        members=members,
        lots=lots,
        lot_b=lb,
        name_of={m["id"]: m["display_name"] for m in members},
    )


def step_sensor(r: Run) -> None:
    a = r.a
    dev = f"kn-chain-{r.id}"
    a.call("POST", "/devices", {"device_id": dev, "source_origin": "SIMULATOR"}, expect=201)
    start = (datetime.now(UTC) - timedelta(hours=30)).replace(microsecond=0)
    a.call(
        "POST",
        f"/devices/{dev}/assignments",
        {
            "placement": "IN_GRAIN",
            "crop": "PADDY",
            "assigned_at": start.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        },
        expect=201,
    )
    unacked = feed_readings(dev)
    eq(
        "sensor",
        unacked,
        "0",
        "readings left unacknowledged by the real ingestion code",
    )
    operator(["python", "-m", "kisaniti_api.devices.assess_job", "--once"])
    ass = a.call("GET", f"/devices/{dev}/assessment")
    check(
        "sensor",
        ass["category"] in ("MEDIUM", "HIGH"),
        "a humid scenario is assessed MEDIUM or HIGH",
        ass["category"],
    )
    check(
        "sensor",
        ass["samples_used"] >= 276 and ass["provenance"]["origin"] == "SYNTHETIC",
        "at least 276 of 288 readings used (assessed on the hour: up to 12 not in yet), labelled synthetic",
        ass,
    )
    age = datetime.now(UTC) - datetime.fromisoformat(ass["assessed_at"].replace("Z", "+00:00"))
    check(
        "sensor",
        timedelta(0) <= age < timedelta(hours=2),
        "the assessment is less than two hours old",
        age,
    )
    a.call(
        "POST",
        f"/lots/{r.x['lots'][0]['id']}/sensor-links",
        {"device_id": dev},
        expect=201,
    )
    r.b.call("GET", f"/devices/{dev}", expect=404)
    r.x.update(device=dev, assessment=ass)


def step_plan(r: Run) -> None:
    a = r.a
    (m1, _n1, bas1), (m2, _n2, bas2) = r.x["mandis"]
    body = {
        "variety": "1121",
        "horizons_days": [1, 7],
        "mandis": [
            {"mandi_id": m1, "transport_inr_per_quintal": "35.00"},
            {"mandi_id": m2, "transport_inr_per_quintal": "10.00"},
        ],
        "increment_kg": "100.000",
    }
    key = f"chain-plan-{uuid.uuid4().hex}"
    plan = a.call("POST", "/plans", body, key=key, expect=201)
    check(
        "plan",
        plan["status"] == "PROPOSED" and plan["validation"]["valid"],
        "a proposed plan the independent validator accepts",
        plan["not_proposed_reasons"],
    )
    eq(
        "plan",
        plan["allocation_result"]["status"]["optimality"],
        "PROVEN_OPTIMAL",
        "optimality",
    )
    eq(
        "plan",
        a.call("POST", "/plans", body, key=key, expect=201)["id"],
        plan["id"],
        "the plan id on an idempotent replay",
    )
    lots = r.x["lots"]
    rows = {x["lot_id"]: x for x in plan["lots"]}
    eq(
        "plan",
        sorted(rows),
        sorted(x["id"] for x in lots),
        "the lots of this FPO and commodity (B's lot must not appear)",
    )
    eq(
        "plan",
        rows[lots[3]["id"]]["excluded_reason"],
        "VARIETY_NOT_MATCHING",
        "the other variety",
    )
    eq(
        "plan",
        rows[lots[0]["id"]]["storage"],
        r.x["assessment"]["category"],
        "the sensor lot's storage category = the assessment",
    )
    check(
        "plan",
        rows[lots[0]["id"]]["storage_reason"] is None and rows[lots[0]["id"]]["device_id"] == r.x["device"],
        "the sensor is named",
    )
    eq(
        "plan",
        [rows[x["id"]]["storage"] for x in lots[1:3]],
        ["UNKNOWN", "UNKNOWN"],
        "lots without a sensor",
    )
    # prices and costs from first principles: the naive point of that horizon + the mandi's basis, entered transport
    fc = {
        h["target_date"]: Decimal(h["point_inr_per_quintal"])
        for h in next(m for m in r.x["forecast"]["methods"] if m["method"] == "NAIVE")["horizons"]
    }
    basis = {m1: bas1, m2: bas2}
    transport = {m1: Decimal("35.00"), m2: Decimal("10.00")}
    inp = plan["allocation_input"]
    for ch in inp["choices"]:
        eq(
            "plan",
            Decimal(ch["price_inr_per_kg"]) * 100,
            fc[ch["date"]] + basis[ch["mandi_id"]],
            "price per quintal = forecast point + basis",
        )
        eq(
            "plan",
            Decimal(ch["cost_inr_per_kg"]) * 100,
            transport[ch["mandi_id"]],
            "transport as entered",
        )
    price = {
        (c["lot_id"], c["mandi_id"], c["date"]): (
            c["price_inr_per_kg"],
            c["cost_inr_per_kg"],
        )
        for c in inp["choices"]
    }
    net = sum(
        cl.line_net_paise(x["quantity_kg"], *price[(x["lot_id"], x["mandi_id"], x["date"])])
        for x in plan["allocation_result"]["assignments"]
    )
    eq(
        "plan",
        cl.paise(plan["allocation_result"]["totals"]["expected_net_return_inr"]),
        net,
        "net return recomputed line by line (paise)",
    )
    planned = {x["lot_id"]: x["scheduled_kg"] for x in plan["allocation_result"]["lots"]}
    eq(
        "plan",
        [planned[x["id"]] for x in lots[:3]],
        ["1200.000", "800.000", "500.000"],
        "whole selling steps of 100 kg",
    )
    for x in lots:
        eq(
            "plan",
            a.call("GET", f"/lots/{x['id']}")["balance"]["reserved_kg"],
            "0.000",
            "a proposal reserves nothing",
        )
    r.x["plan"] = plan
    r.x["plan_body"] = body


def step_review(r: Run) -> None:
    a, plan, lots = r.a, r.x["plan"], r.x["lots"]
    a1 = plan["allocation_result"]["assignments"]
    line = next(x for x in a1 if x["lot_id"] == lots[0]["id"])
    over = [{**x, "quantity_kg": "1300.000"} if x is line else x for x in a1]
    got = a.call(
        "POST",
        f"/plans/{plan['id']}/revisions",
        {"base_revision_no": 1, "assignments": over},
        expect=422,
        code="PLAN_INVALID",
    )
    check(
        "review",
        any("over-allocated" in i["message"] for i in got["issues"]),
        "the validator names the over-allocation",
        got["issues"],
    )
    # a valid edit: move 100 kg of farmer 2's lot to the other planned date
    l2 = next(x for x in a1 if x["lot_id"] == lots[1]["id"])
    d1, d2 = plan["allocation_input"]["dates"]
    other = d2 if l2["date"] == d1 else d1
    moved = [x for x in a1 if x is not l2] + [
        {**l2, "quantity_kg": cl.kg(cl.grams(l2["quantity_kg"]) - 100_000)},
        {**l2, "date": other, "quantity_kg": "100.000"},
    ]
    rev = a.call(
        "POST",
        f"/plans/{plan['id']}/revisions",
        {"base_revision_no": 1, "assignments": moved},
        expect=201,
    )
    eq(
        "review",
        (rev["revision_no"], rev["kind"], rev["validation"]["valid"]),
        (2, "MANAGER_EDIT", True),
        "the edit becomes revision 2",
    )
    eq("review", len(rev["changes"]), 2, "two changed lines")
    eq(
        "review",
        rev["allocation_result"]["totals"]["scheduled_kg"],
        plan["allocation_result"]["totals"]["scheduled_kg"],
        "the edit moved quantity, it did not create any",
    )
    st = a.call("GET", f"/plans/{plan['id']}/state")
    eq(
        "review",
        (st["current_revision_no"], [x["superseded"] for x in st["revisions"]]),
        (2, [True, False]),
        "revision state",
    )
    eq(
        "review",
        a.call("GET", f"/plans/{plan['id']}")["allocation_result"]["assignments"],
        a1,
        "the system's proposal is never edited",
    )
    r.x["rev"] = rev


def step_approval(r: Run) -> None:
    a, plan, lots, rev = r.a, r.x["plan"], r.x["lots"], r.x["rev"]
    pid = plan["id"]
    ctx = a.call("GET", f"/plans/{pid}/approval-context")
    need = ctx["required_acknowledgements"]
    check(
        "approval",
        ctx["can_approve"] and need,
        "an approvable plan with warnings to acknowledge",
        ctx,
    )
    a.call(
        "POST",
        f"/plans/{pid}/approve",
        {"revision_no": 2, "acknowledged_warnings": need[:-1]},
        expect=422,
        code="WARNINGS_NOT_ACKNOWLEDGED",
    )
    a.call(
        "POST",
        f"/plans/{pid}/approve",
        {"revision_no": 1, "acknowledged_warnings": need},
        expect=409,
        code="STALE_REVISION",
    )
    dec = a.call(
        "POST",
        f"/plans/{pid}/approve",
        {"revision_no": 2, "acknowledged_warnings": need},
        key=f"chain-ap-{uuid.uuid4().hex}",
        expect=201,
    )
    eq(
        "approval",
        (dec["decision"], dec["revision_no"], dec["decided_by"]),
        ("APPROVED", 2, r.x["admin_a"]),
        "the decision",
    )
    a.call(
        "POST",
        f"/plans/{pid}/approve",
        {"revision_no": 2, "acknowledged_warnings": need},
        expect=409,
        code="ALREADY_DECIDED",
    )
    per_lot: dict[str, int] = {}
    for x in rev["assignments"]:
        per_lot[x["lot_id"]] = per_lot.get(x["lot_id"], 0) + cl.grams(x["quantity_kg"])
    for x in lots:
        want = per_lot.get(x["id"], 0)
        bal = a.call("GET", f"/lots/{x['id']}")["balance"]
        eq(
            "approval",
            bal["reserved_kg"],
            cl.kg(want),
            "reserved = the approved revision's quantities, exactly",
        )
        eq(
            "approval",
            bal["available_kg"],
            cl.kg(cl.grams(x["registered_quantity_kg"]) - want),
            "available = on hand - reserved",
        )
        if want:
            r.expect_event(x["id"], "RESERVED", want)
    # the stock that is left is smaller than one selling step everywhere: nothing can be planned from it
    a.call(
        "POST",
        "/plans",
        r.x["plan_body"],
        key=f"chain-plan2-{uuid.uuid4().hex}",
        expect=422,
        code="NO_ELIGIBLE_LOTS",
    )
    r.x["per_lot"] = per_lot
    r.x["rev_assignments"] = rev["assignments"]


def step_messages(r: Run) -> None:
    a, b = r.a, r.b
    pid, lots, members = r.x["plan"]["id"], r.x["lots"], r.x["members"]
    phones = {
        members[0]["id"]: "9000000001",
        members[1]["id"]: "9000000002",
        members[2]["id"]: "9000000003",
    }
    for m in members[:2]:
        a.call(
            "POST",
            f"/members/{m['id']}/contact",
            {"phone": phones[m["id"]]},
            expect=201,
        )
        a.call(
            "POST",
            f"/members/{m['id']}/consents",
            {
                "kind": "WHATSAPP",
                "action": "GRANTED",
                "notice_version": "NOTICE-DEMO-0.1",
                "method": "STAFF_ASSISTED_ORAL",
            },
            expect=201,
        )
    docs = {
        d["member_id"]: d
        for d in a.call(
            "POST",
            f"/plans/{pid}/instructions",
            {"kind": "SALE_INSTRUCTION"},
            expect=201,
        )
    }
    eq(
        "messages",
        sorted(docs),
        sorted(m["id"] for m in members),
        "one instruction per farmer who has stock in the plan",
    )
    mandi_name = {m["mandi_id"]: m["name"] for m in r.x["plan"]["mandis"]}
    owner = {x["id"]: x["member_id"] for x in lots}
    lines_of: dict[str, list[tuple[str, str, str]]] = {}
    for x in sorted(
        r.x["rev_assignments"],
        key=lambda z: (z["date"], mandi_name[z["mandi_id"]], z["lot_id"]),
    ):
        lines_of.setdefault(owner[x["lot_id"]], []).append((x["quantity_kg"], mandi_name[x["mandi_id"]], x["date"]))
    for m, lang in zip(members[:2], ("pa", "hi"), strict=True):
        d = docs[m["id"]]
        eq(
            "messages",
            (d["status"], d["language"]),
            ("PREPARED", lang),
            "a farmer with contact and consent",
        )
        bad = cl.check_message(d["text"], "SALE_INSTRUCTION", lang, m["display_name"], lines_of[m["id"]])
        check("messages", not bad, f"the {lang} text is exact", bad)
        eq(
            "messages",
            d["template"]["review_status"],
            "UNREVIEWED",
            "the translations are labelled unreviewed",
        )
    third = docs[members[2]["id"]]
    eq(
        "messages",
        (third["status"], third["issues"], third["text"]),
        ("BLOCKED", ["NO_CONTACT", "NO_WHATSAPP_CONSENT"], None),
        "the farmer without contact or consent",
    )
    a.call(
        "POST",
        f"/instructions/{third['id']}/send",
        {"channel": "PREVIEW"},
        expect=409,
        code="INSTRUCTION_BLOCKED",
    )
    eq(
        "messages",
        [q["id"] for q in a.call("GET", "/instruction-queue")],
        [third["id"]],
        "the queue holds exactly the blocked instruction",
    )
    att = a.call(
        "POST",
        f"/instructions/{docs[members[0]['id']]['id']}/send",
        {"channel": "PREVIEW"},
        expect=201,
    )
    eq(
        "messages",
        (att["status"], att["simulated"], att["provider_message_id"]),
        ("PREVIEWED", True, None),
        "a preview is not a delivery",
    )
    eq(
        "messages",
        att["preview"]["recipient"],
        cl.mask_phone("+91" + phones[members[0]["id"]]),
        "the recipient is masked",
    )
    eq(
        "messages",
        att["preview"]["label"],
        "PREVIEW — nothing was sent",
        "the preview label",
    )
    eq(
        "messages",
        att["preview"]["text"],
        docs[members[0]["id"]]["text"],
        "the previewed text",
    )
    # fix the third farmer and prepare again: a new version that links the blocked one
    a.call(
        "POST",
        f"/members/{members[2]['id']}/contact",
        {"phone": phones[members[2]["id"]]},
        expect=201,
    )
    a.call(
        "POST",
        f"/members/{members[2]['id']}/consents",
        {
            "kind": "WHATSAPP",
            "action": "GRANTED",
            "notice_version": "NOTICE-DEMO-0.1",
            "method": "PAPER_FORM",
        },
        expect=201,
    )
    again = {
        d["member_id"]: d
        for d in a.call(
            "POST",
            f"/plans/{pid}/instructions",
            {"kind": "SALE_INSTRUCTION"},
            expect=201,
        )
    }
    eq(
        "messages",
        [again[m["id"]]["id"] for m in members[:2]],
        [docs[m["id"]]["id"] for m in members[:2]],
        "no duplicate instructions for farmers already prepared",
    )
    t3 = again[members[2]["id"]]
    eq(
        "messages",
        (t3["status"], t3["version"], t3["supersedes"], t3["language"]),
        ("PREPARED", 2, third["id"], "en"),
        "the fixed farmer",
    )
    check(
        "messages",
        not cl.check_message(
            t3["text"],
            "SALE_INSTRUCTION",
            "en",
            members[2]["display_name"],
            lines_of[members[2]["id"]],
        ),
        "the English text is exact",
    )
    eq(
        "messages",
        a.call("POST", f"/instructions/{t3['id']}/send", {"channel": "PREVIEW"}, expect=201)["status"],
        "PREVIEWED",
        "preview of the third",
    )
    eq(
        "messages",
        a.call("GET", "/instruction-queue"),
        [],
        "nothing needs attention any more",
    )
    # a reply is stored and shown, and changes nothing
    before = a.call("GET", f"/members/{members[0]['id']}/consents")["current"]
    a.call(
        "POST",
        f"/instructions/{docs[members[0]['id']]['id']}/replies",
        {"text": "ਮੈਂ ਸਹਿਮਤ ਨਹੀਂ"},
        expect=201,
    )
    eq(
        "messages",
        a.call("GET", f"/members/{members[0]['id']}/consents")["current"],
        before,
        "consent after a reply",
    )
    eq(
        "messages",
        a.call("GET", f"/plans/{pid}/state")["status"],
        "APPROVED",
        "the plan after a reply",
    )
    for m in members[:2]:
        b.call("GET", f"/instructions/{docs[m['id']]['id']}", expect=404)
    # a VIEWER (no contacts.view_phone) sees the contacts and the instructions, and no full number anywhere
    v = Client(r.base)
    v.step = "messages"
    created = a.call(
        "POST",
        "/users",
        {"username": f"chain.v.{r.id}", "display_name": "Chain viewer (fictional)", "role": "VIEWER"},
        expect=201,
    )
    pw = "chain-" + uuid.uuid4().hex
    first = Client(r.base)
    first.login(f"chain.v.{r.id}", created["temporary_password"])
    first.call(
        "POST",
        "/auth/change-password",
        {"current_password": created["temporary_password"], "new_password": pw},
        expect=204,
    )
    v.login(f"chain.v.{r.id}", pw)
    seen = v.call("GET", "/contacts")
    check(
        "messages",
        all(c["contact"] is not None for c in seen[:3]) and len(seen) == 3,
        "the viewer sees the three contacts",
        seen,
    )
    v.call("GET", f"/plans/{pid}/instructions")
    for d in again.values():
        v.call("GET", f"/instructions/{d['id']}")
        v.call("GET", f"/instructions/{d['id']}/attempts")
    v.call("POST", f"/instructions/{t3['id']}/send", {"channel": "PREVIEW"}, expect=403)
    v.call("POST", f"/members/{members[0]['id']}/contact", {"phone": "9000000009"}, expect=403)
    leaked = [n for n in phones.values() if any(n in t for t in v.seen_text + b.seen_text)]
    check("messages", not leaked, "no full phone number in any response to a viewer or to the other FPO", leaked)
    # the admin's own views of numbers were logged (who looked at which number)
    n_logged = a.call("GET", f"/members/{members[0]['id']}/contact/history")
    check("messages", len(n_logged) >= 1, "the contact history is readable by the admin", n_logged)
    r.x.update(
        instructions={m["id"]: (again[m["id"]]) for m in members},
        lines_of=lines_of,
        phones=phones,
        viewer=v,
    )


WA_LINK = re.compile(r"^https://wa\.me/(?P<digits>[0-9]{8,15})\?text=(?P<text>[A-Za-z0-9%._~-]+)$")


def parse_link(step: str, link: str | None) -> tuple[str, str]:
    """(digits, decoded text) of a wa.me link, or a failure naming what is wrong with it."""
    check(step, link is not None and WA_LINK.match(link) is not None, "a wa.me link of the agreed shape", link)
    m = WA_LINK.match(link or "")
    assert m is not None
    return m["digits"], urllib.parse.unquote(m["text"])


def step_handover(r: Run) -> None:
    """Stage 4 by the manager's own WhatsApp (handover/v1): preview, the yes, one exact link per farmer, reports."""
    a, b, v = r.a, r.b, r.x["viewer"]
    pid, members, phones, ins = r.x["plan"]["id"], r.x["members"], r.x["phones"], r.x["instructions"]
    pv = a.call("GET", f"/plans/{pid}/handover-preview?kind=SALE_INSTRUCTION")
    eq(
        "handover",
        (pv["recipient_count"], pv["unreviewed_count"], pv["blocked"]),
        (3, 3, []),
        "the question: 3 farmers",
    )
    check("handover", not any(n in json.dumps(pv) for n in phones.values()), "a preview never shows a number", pv)
    v.call("GET", f"/plans/{pid}/handover-preview?kind=SALE_INSTRUCTION", expect=403)
    body = {"kind": "SALE_INSTRUCTION", "recipient_count": 3, "acknowledge_unreviewed": True}
    a.call("POST", f"/plans/{pid}/handovers", {**body, "recipient_count": 2}, expect=409, code="CONFIRMATION_STALE")
    a.call(
        "POST",
        f"/plans/{pid}/handovers",
        {**body, "acknowledge_unreviewed": False},
        expect=422,
        code="UNREVIEWED_NOT_ACKNOWLEDGED",
    )
    v.call("POST", f"/plans/{pid}/handovers", body, expect=403)
    eq("handover", a.call("GET", f"/plans/{pid}/handovers"), [], "nothing was created by the refused presses")
    h = a.call("POST", f"/plans/{pid}/handovers", body, key=f"chain-hnd-{uuid.uuid4().hex}", expect=201)
    eq("handover", (h["recipient_count"], len(h["items"])), (3, 3), "one item per farmer")
    items = {i["member_id"]: i for i in h["items"]}
    for m in members:
        i = items[m["id"]]
        digits, decoded = parse_link("handover", i["link"])
        eq("handover", digits, "91" + phones[m["id"]], "the digits of the link are the farmer's number")
        eq("handover", decoded, ins[m["id"]]["text"], "the link carries the stored instruction text, byte for byte")
        eq("handover", (i["state"], i["status"]), ("NOT_REPORTED", "ISSUED"), "a new item")
    # a viewer and the other FPO never get a link or a number
    seen = v.call("GET", f"/handovers/{h['id']}")
    check("handover", all(i["link"] is None for i in seen["items"]), "a viewer gets no link", seen)
    b.call("GET", f"/handovers/{h['id']}", expect=404)
    # the manager's reports: farmers 1 and 3 sent, farmer 2 not
    a0, a1, a2 = (items[m["id"]]["instruction_id"] for m in members)
    for ins_id, outcome in ((a0, "SENT"), (a1, "NOT_SENT"), (a2, "SENT")):
        got = a.call("POST", f"/handovers/{h['id']}/items/{ins_id}/outcome", {"outcome": outcome}, expect=201)
        eq(
            "handover",
            got["state"],
            {"SENT": "MANAGER_REPORTED_SENT", "NOT_SENT": "MANAGER_REPORTED_NOT_SENT"}[outcome],
            "state",
        )
    a.call("POST", f"/handovers/{h['id']}/items/{a0}/outcome", {"outcome": "SENT"}, expect=409, code="NO_CHANGE")
    after = {i["member_id"]: i for i in a.call("GET", f"/handovers/{h['id']}")["items"]}
    eq(
        "handover",
        [after[m["id"]]["link"] is None for m in members],
        [True, False, True],
        "links remain only for farmer 2",
    )
    # a report is the manager's word, not delivery: messaging/v1 is unchanged
    got_att = [[x["status"] for x in a.call("GET", f"/instructions/{ins[m['id']]['id']}/attempts")] for m in members]
    eq(
        "handover",
        got_att,
        [["PREVIEWED"], [], ["PREVIEWED"]],
        "delivery records: only the earlier previews, no SENT or DELIVERED",
    )
    pv2 = a.call("GET", f"/plans/{pid}/handover-preview?kind=SALE_INSTRUCTION")
    eq(
        "handover",
        ([x["member_id"] for x in pv2["ready"]], sorted(pv2["already_reported_sent"])),
        ([members[1]["id"]], sorted([members[0]["id"], members[2]["id"]])),
        "second press: only farmer 2",
    )
    h2 = a.call("POST", f"/plans/{pid}/handovers", {**body, "recipient_count": 1}, expect=201)
    eq(
        "handover",
        [parse_link("handover", i["link"])[0] for i in h2["items"]],
        ["91" + phones[members[1]["id"]]],
        "the second press",
    )
    r.x["handover_2"] = h2["id"]
    r.x["handover_1"] = h["id"]


def step_sales(r: Run) -> None:
    a, plan, lots = r.a, r.x["plan"], r.x["lots"]
    pid = plan["id"]
    asg = {x["lot_id"]: x for x in r.x["rev_assignments"] if x["lot_id"] in (lots[0]["id"], lots[2]["id"])}
    mandi = r.x["mandis"][0][0]

    def sell(
        lot: dict[str, Any],
        qty: str,
        price: str,
        linked: bool,
        expect: int = 201,
        **extra: Any,
    ) -> Any:
        ref = (
            {
                "plan_id": pid,
                "mandi_id": asg[lot["id"]]["mandi_id"],
                "date": asg[lot["id"]]["date"],
            }
            if linked
            else None
        )
        body = {
            "lot_id": lot["id"],
            "plan_ref": ref,
            "mandi_id": mandi,
            "sale_date": r.today,
            "quantity_kg": qty,
            "price_inr_per_quintal": price,
            "j_form_no": r.j(),
            **extra,
        }
        return a.call(
            "POST",
            "/sales",
            body,
            expect=expect,
            key=f"chain-sale-{uuid.uuid4().hex}" if expect == 201 else None,
        )

    l1, l2, l3 = lots[0], lots[1], lots[2]
    s1 = sell(l1, "500.000", "4400.00", True)
    eq(
        "sales",
        (s1["amount_inr"], s1["consumed_kg"]),
        (cl.rupees(cl.sale_amount_paise("500.000", "4400.00")), "500.000"),
        "amount and consumption of the first sale",
    )
    r.expect_event(l1["id"], "RESERVATION_CONSUMED", 500_000)
    r.expect_event(l1["id"], "SOLD", 500_000)
    bal = a.call("GET", f"/lots/{l1['id']}")["balance"]
    eq(
        "sales",
        (bal["on_hand_kg"], bal["reserved_kg"]),
        ("750.000", "700.000"),
        "lot 1 after the first sale",
    )
    s2 = sell(l1, "700.000", "4500.00", True)
    r.expect_event(l1["id"], "RESERVATION_CONSUMED", 700_000)
    r.expect_event(l1["id"], "SOLD", 700_000)
    eq(
        "sales",
        a.call("GET", f"/lots/{l1['id']}")["balance"]["reserved_kg"],
        "0.000",
        "lot 1 reserved once fully sold",
    )
    sell(l1, "1.000", "4500.00", True, expect=409)
    # off-plan sale on the lot with no free stock: the system asks, it does not choose
    asked = a.call(
        "POST",
        "/sales",
        {
            "lot_id": l2["id"],
            "plan_ref": None,
            "mandi_id": mandi,
            "sale_date": r.today,
            "quantity_kg": "100.000",
            "price_inr_per_quintal": "4300.00",
            "j_form_no": r.j(),
        },
        expect=409,
        code="RECONCILIATION_REQUIRED",
    )
    eq(
        "sales",
        [(i["path"], i["message"]) for i in asked["issues"][1:]],
        [(pid, "800.000")],
        "the plan that holds the lot's stock is named",
    )
    eq(
        "sales",
        a.call("GET", f"/lots/{l2['id']}")["balance"]["sold_kg"],
        "0.000",
        "nothing was written by the refused sale",
    )
    off = a.call(
        "POST",
        "/sales",
        {
            "lot_id": l2["id"],
            "plan_ref": None,
            "mandi_id": mandi,
            "sale_date": r.today,
            "quantity_kg": "100.000",
            "price_inr_per_quintal": "4300.00",
            "j_form_no": r.j(),
            "release_reservations": [{"plan_id": pid, "quantity_kg": "100.000"}],
        },
        expect=201,
    )
    eq(
        "sales",
        off["released_for_sale"],
        [{"plan_id": pid, "quantity_kg": "100.000"}],
        "the explicit release",
    )
    r.expect_event(l2["id"], "RESERVATION_RELEASED", 100_000)
    r.expect_event(l2["id"], "SOLD", 100_000)
    bal = a.call("GET", f"/lots/{l2['id']}")["balance"]
    eq(
        "sales",
        (bal["on_hand_kg"], bal["reserved_kg"], bal["sold_kg"]),
        ("700.000", "700.000", "100.000"),
        "lot 2 after the off-plan sale",
    )
    # a mistaken entry is voided: stock returns, the reservation does not
    s3 = sell(l3, "400.000", "4300.00", True)
    r.expect_event(l3["id"], "RESERVATION_CONSUMED", 400_000)
    r.expect_event(l3["id"], "SOLD", 400_000)
    v = a.call("POST", f"/sales/{s3['id']}/void", {"reason_code": "ENTRY_ERROR"}, expect=201)
    check("sales", v["voided"] is not None, "the void is recorded on the sale")
    r.expect_event(l3["id"], "ADJUSTED", 400_000)
    bal = a.call("GET", f"/lots/{l3['id']}")["balance"]
    eq(
        "sales",
        (bal["on_hand_kg"], bal["reserved_kg"]),
        ("500.000", "100.000"),
        "lot 3 after the void (stock back, reservation stays used)",
    )
    # two rounding ties at 4401.00 per quintal: 0.500 kg is exactly 2200.5 paise (half-even 2200; a half-up rule gives
    # 2201) and 1.500 kg is exactly 6601.5 paise (half-even 6602; rounding down gives 6601). Both sales come from the
    # lot's free stock, off plan, so they consume no reservation.
    for qty, want in (("0.500", 2200), ("1.500", 6602)):
        eq("sales", cl.sale_amount_paise(qty, "4401.00"), want, "the independent calculation of the tie")
        tie = a.call(
            "POST",
            "/sales",
            {
                "lot_id": l1["id"],
                "plan_ref": None,
                "mandi_id": mandi,
                "sale_date": r.today,
                "quantity_kg": qty,
                "price_inr_per_quintal": "4401.00",
                "j_form_no": r.j(),
            },
            key=f"chain-sale-{uuid.uuid4().hex}",
            expect=201,
        )
        eq("sales", cl.paise(tie["amount_inr"]), want, f"{qty} kg at 4401.00 falls exactly between two paise")
        eq("sales", tie["consumed_kg"], "0.000", "an off-plan sale from free stock consumes no reservation")
        r.expect_event(l1["id"], "SOLD", cl.grams(qty))
    # execution and report from first principles
    ex = a.call("GET", f"/plans/{pid}/execution")
    lines = {(x["lot_id"], x["date"]): x for x in ex["lines"]}
    k1 = (l1["id"], asg[l1["id"]]["date"])
    eq(
        "sales",
        (lines[k1]["sold_kg"], lines[k1]["status"]),
        ("1200.000", "COMPLETED"),
        "line of lot 1",
    )
    check(
        "sales",
        all(x["sold_kg"] == "0.000" and x["status"] == "NOT_STARTED" for k, x in lines.items() if k != k1),
        "no other line has sales (the voided one does not count)",
        ex["lines"],
    )
    eq("sales", ex["status"], "PARTIAL", "plan execution")
    rep = a.call("GET", f"/plans/{pid}/report")
    price = {
        (c["lot_id"], c["mandi_id"], c["date"]): c["price_inr_per_kg"] for c in plan["allocation_input"]["choices"]
    }
    rl = {(x["lot_id"], x["date"]): x for x in rep["lines"]}[k1]
    plan_pq = cl.paise(rl["planned_price_inr_per_quintal"])
    key1 = (l1["id"], asg[l1["id"]]["mandi_id"], asg[l1["id"]]["date"])
    whole, frac = price[key1].split(".")
    eq(
        "sales",
        plan_pq,
        int(whole) * 10_000 + int(frac),
        "the plan price per quintal in paise = the price per kg digits",
    )
    avg = cl.weighted_avg_price_paise([("500.000", "4400.00"), ("700.000", "4500.00")])
    eq(
        "sales",
        cl.paise(rl["actual_avg_price_inr_per_quintal"]),
        avg,
        "weighted realised price",
    )
    eq("sales", cl.paise(rl["price_gap_inr_per_quintal"]), avg - plan_pq, "gap")
    eq(
        "sales",
        cl.paise(rl["actual_value_inr"]),
        cl.sale_amount_paise("500.000", "4400.00") + cl.sale_amount_paise("700.000", "4500.00"),
        "actual value",
    )
    eq(
        "sales",
        (rep["totals"]["sold_kg"], rep["totals"]["off_plan_kg"]),
        ("1200.000", "102.000"),
        "report totals",
    )
    f = {x["farmer_id"]: x for x in rep["farmers"]}
    m0, m1, m2 = (m["id"] for m in r.x["members"])
    eq(
        "sales",
        (f[m0]["planned_ratio"]["display"], f[m0]["actual_ratio"]["display"]),
        (cl.ratio6("1200.000", "1250.000"), cl.ratio6("1200.000", "1250.000")),
        "farmer 1 shares",
    )
    eq(
        "sales",
        (f[m1]["actual_ratio"]["display"], f[m2]["actual_ratio"]["display"]),
        ("0.000000", "0.000000"),
        "farmers 2 and 3 sold nothing on plan",
    )
    out = a.call("GET", "/forecast-outcomes")
    eq(
        "sales",
        (out["real"]["pairs"], out["synthetic"]["pairs"]),
        (0, 1),
        "fictional rows are kept apart from real ones",
    )
    eq(
        "sales",
        cl.paise(out["synthetic"]["mean_error_inr_per_quintal"]),
        avg - plan_pq,
        "outcome error",
    )
    r.x["sale_ids"] = {"s1": s1["id"], "s2": s2["id"], "off": off["id"], "s3": s3["id"]}
    r.x["actual_value_paise"] = cl.paise(rep["totals"]["actual_value_inr"])


def step_close(r: Run) -> None:
    a, pid, members, lots = r.a, r.x["plan"]["id"], r.x["members"], r.x["lots"]
    a.call(
        "POST",
        f"/plans/{pid}/instructions",
        {"kind": "CANCELLATION"},
        expect=409,
        code="NOT_RELEASED",
    )
    st = a.call("POST", f"/plans/{pid}/release", {"reason_code": "PLAN_CANCELLED"}, expect=201)
    eq(
        "close",
        (
            st["status"],
            st["reservation"]["state"],
            st["reservation"]["release"] is not None,
        ),
        ("APPROVED", "RELEASED", True),
        "released by a person",
    )
    ex = a.call("GET", f"/plans/{pid}/execution")
    eq(
        "close",
        [x["remaining_kg"] for x in ex["lots"]],
        ["0.000"] * 3,
        "nothing stays reserved",
    )
    rem = {"lot2": 700_000, "lot3": 100_000}
    r.expect_event(lots[1]["id"], "RESERVATION_RELEASED", rem["lot2"])
    r.expect_event(lots[2]["id"], "RESERVATION_RELEASED", rem["lot3"])
    a.call(
        "POST",
        f"/plans/{pid}/release",
        {"reason_code": "OTHER"},
        expect=409,
        code="NOTHING_TO_RELEASE",
    )
    ins = r.x["instructions"]
    late = a.call(
        "POST",
        f"/instructions/{ins[members[1]['id']]['id']}/send",
        {"channel": "PREVIEW"},
        expect=201,
    )
    eq(
        "close",
        (late["status"], late["issues"]),
        ("BLOCKED", ["COMMITMENT_NOT_ACTIVE"]),
        "a sale message after the release",
    )
    docs = {
        d["member_id"]: d for d in a.call("POST", f"/plans/{pid}/instructions", {"kind": "CANCELLATION"}, expect=201)
    }
    eq(
        "close",
        sorted(docs),
        sorted([members[0]["id"], members[2]["id"]]),
        "cancellations go only to farmers who were previewed",
    )
    for m, lang in ((members[0], "pa"), (members[2], "en")):
        d = docs[m["id"]]
        eq(
            "close",
            (d["status"], d["language"], d["supersedes"]),
            ("PREPARED", lang, ins[m["id"]]["id"]),
            "cancellation links the earlier instruction",
        )
        bad = cl.check_message(d["text"], "CANCELLATION", lang, m["display_name"], r.x["lines_of"][m["id"]])
        check("close", not bad, f"the {lang} cancellation text is right", bad)
        eq(
            "close",
            a.call(
                "POST",
                f"/instructions/{d['id']}/send",
                {"channel": "PREVIEW"},
                expect=201,
            )["status"],
            "PREVIEWED",
            "preview of the cancellation",
        )
    # the unsent link of the second hand-over is withheld now that the reservation is released
    left = {i["member_id"]: i for i in a.call("GET", f"/handovers/{r.x['handover_2']}")["items"]}
    eq(
        "close",
        (left[members[1]["id"]]["link"], left[members[1]["id"]]["link_withheld"]),
        (None, ["COMMITMENT_NOT_ACTIVE"]),
        "a released plan withholds the link",
    )
    # the cancellation by the manager's own WhatsApp reaches exactly the farmers who were told: 1 and 3
    cpv = a.call("GET", f"/plans/{pid}/handover-preview?kind=CANCELLATION")
    eq(
        "close",
        sorted(x["member_id"] for x in cpv["ready"]),
        sorted([members[0]["id"], members[2]["id"]]),
        "who gets the cancellation",
    )
    ch = a.call(
        "POST",
        f"/plans/{pid}/handovers",
        {"kind": "CANCELLATION", "recipient_count": 2, "acknowledge_unreviewed": True},
        expect=201,
    )
    for i in ch["items"]:
        digits, decoded = parse_link("close", i["link"])
        eq("close", digits, "91" + r.x["phones"][i["member_id"]], "the number of the cancellation link")
        eq("close", decoded, docs[i["member_id"]]["text"], "the cancellation text, byte for byte")


def step_ledger_audit(r: Run) -> None:
    a = r.a
    if r.sabotage == "ledger":  # a stock event written straight into the database, bypassing every rule of the API
        lot = r.x["lots"][1]["id"]
        operator(
            [
                "psql",
                "-U",
                "kisaniti",
                "-d",
                "kisaniti",
                "-v",
                "ON_ERROR_STOP=1",
                "-c",
                f"INSERT INTO stock_events (lot_id, fpo_id, kind, quantity_g) "
                f"VALUES ('{lot}', '{r.x['fpo_a']}', 'RESERVED', 1000)",
            ],
            service="db",
        )
    ex = a.call("GET", f"/plans/{r.x['plan']['id']}/execution")
    remaining = {x["lot_id"]: cl.grams(x["remaining_kg"]) for x in ex["lots"]}
    for lot in r.x["lots"]:
        evs = a.call("GET", f"/lots/{lot['id']}/events?limit=1000")
        got = [
            (
                e["kind"],
                cl.paise_free_signed(e["quantity_delta_kg"]) if e["kind"] == "ADJUSTED" else cl.grams(e["quantity_kg"]),
            )
            for e in sorted(evs, key=lambda e: e["id"])
        ]
        eq(
            "ledger-audit",
            got,
            r.events[lot["id"]],
            f"the ledger events of lot {lot['id']} (exactly what the chain did, in order)",
        )
        b = cl.ledger_balances(evs)
        api = a.call("GET", f"/lots/{lot['id']}")["balance"]
        eq(
            "ledger-audit",
            (
                cl.kg(b.on_hand_g),
                cl.kg(b.reserved_g),
                cl.kg(b.sold_g),
                cl.kg(b.available_g),
            ),
            (
                api["on_hand_kg"],
                api["reserved_kg"],
                api["sold_kg"],
                api["available_kg"],
            ),
            f"balances of lot {lot['id']}: replayed ledger vs API",
        )
        eq(
            "ledger-audit",
            b.reserved_g,
            remaining.get(lot["id"], 0),
            f"reserved stock of lot {lot['id']} = what the plan's execution still holds",
        )
    sales = [s for s in a.call("GET", "/sales") if s["voided"] is None and s["plan_ref"] is not None]
    eq(
        "ledger-audit",
        sum(cl.paise(s["amount_inr"]) for s in sales),
        r.x["actual_value_paise"],
        "the sales on the plan add up to the report's actual value",
    )
    check(
        "ledger-audit",
        all(
            cl.paise(s["amount_inr"]) == cl.sale_amount_paise(s["quantity_kg"], s["price_inr_per_quintal"])
            for s in a.call("GET", "/sales")
        ),
        "every sale amount is quantity x price / 100 to the paisa",
    )


def step_isolation(r: Run) -> None:
    a, b = r.a, r.b
    pid, lot = r.x["plan"]["id"], r.x["lots"][0]["id"]
    b.call("GET", f"/plans/{pid}/handover-preview?kind=SALE_INSTRUCTION", expect=404)
    b.call("GET", f"/plans/{pid}/handovers", expect=404)
    b.call("GET", f"/handovers/{r.x['handover_1']}", expect=404)
    for path in (
        f"/plans/{pid}",
        f"/plans/{pid}/state",
        f"/plans/{pid}/execution",
        f"/plans/{pid}/report",
        f"/lots/{lot}",
        f"/sales/{r.x['sale_ids']['s1']}",
        f"/devices/{r.x['device']}",
    ):
        b.call("GET", path, expect=404)
    for path in ("/plans", "/sales", "/instruction-queue", "/plan-states", "/replies"):
        eq("isolation", b.call("GET", path), [], f"B's {path}")
    eq(
        "isolation",
        b.call("GET", "/forecast-outcomes")["synthetic"]["pairs"],
        0,
        "B's outcomes",
    )
    eq(
        "isolation",
        [c["member_id"] for c in b.call("GET", "/contacts")],
        [r.x["lot_b"]["member_id"]],
        "B sees only its own member",
    )
    eq(
        "isolation",
        b.call("GET", f"/lots/{r.x['lot_b']['id']}")["balance"]["on_hand_kg"],
        "1000.000",
        "B's own lot is untouched",
    )
    a.call("GET", f"/lots/{r.x['lot_b']['id']}", expect=404)


STEPS: list[tuple[str, Callable[[Run], None]]] = [
    ("preflight", step_preflight), ("fpos", step_fpos), ("data", step_data), ("stock", step_stock),
    ("sensor", step_sensor), ("plan", step_plan), ("review", step_review), ("approval", step_approval),
    ("messages", step_messages), ("handover", step_handover), ("sales", step_sales),
    ("close", step_close), ("ledger-audit", step_ledger_audit), ("isolation", step_isolation),
]  # fmt: skip


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--api", default=os.environ.get("KN_API", "http://localhost:8000"))
    ap.add_argument("--report", default="chain_report.json")
    ap.add_argument("--stop-after", choices=[n for n, _ in STEPS], help="stop after this step (for a demo)")
    ap.add_argument("--print-login", action="store_true", help="print the fictional manager login of FPO A")
    ap.add_argument(
        "--sabotage",
        choices=["ledger"],
        help="prove the chain can fail: corrupt the ledger before the audit",
    )
    args = ap.parse_args(argv)
    run = Run(args.api, args.sabotage)
    commit = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        capture_output=True,
        text=True,
        cwd=ROOT,
    ).stdout.strip()
    report: dict[str, Any] = {
        "run": run.id,
        "api": args.api,
        "commit": commit,
        "started_at": datetime.now(UTC).isoformat(),
        "sabotage": args.sabotage,
        "steps": [],
    }
    print(
        f"chain run {run.id} against {args.api} (commit {commit})"
        + (f"  [sabotage: {args.sabotage}]" if args.sabotage else "")
    )
    failure: ChainFailure | None = None
    for name, fn in STEPS:
        run.a.step = run.b.step = name
        t0 = time.time()
        try:
            fn(run)
        except ChainFailure as e:
            failure = e
            report["steps"].append({"step": name, "ok": False, "seconds": round(time.time() - t0, 2)})
            print(f"[FAIL] {name}")
            print(
                f"       expected: {e.expected}\n       got:      {e.got}"
                + (f"\n       request:  {e.request_id}" if e.request_id else "")
            )
            break
        except (
            AssertionError,
            KeyError,
            ValueError,
            IndexError,
            urllib.error.URLError,
        ) as e:
            failure = ChainFailure(name, "no error", f"{type(e).__name__}: {e}")
            report["steps"].append({"step": name, "ok": False, "seconds": round(time.time() - t0, 2)})
            print(f"[FAIL] {name}\n       {type(e).__name__}: {str(e)[:400]}")
            break
        report["steps"].append({"step": name, "ok": True, "seconds": round(time.time() - t0, 2)})
        print(f"[ ok ] {name:13s} {time.time() - t0:5.1f} s")
        if args.stop_after == name:
            print(f"stopped after {name} on request: the later steps did not run")
            report["stopped_after"] = name
            break
    report["ok"] = failure is None
    if failure:
        report["failure"] = {
            "step": failure.step,
            "expected": failure.expected,
            "got": failure.got,
            "request_id": failure.request_id,
        }
    else:
        report["entities"] = {
            "fpo_a": run.x.get("fpo_a"),
            "fpo_b": run.x.get("fpo_b"),
            "plan": run.x["plan"]["id"],
            "device": run.x.get("device"),
            "lots": [x["id"] for x in run.x["lots"]],
            "contracts": run.x["contracts"],
            "mode": run.x["mode"],
            "price_dataset_status": run.x["dataset_status"],
            "fixture_prices_seeded": bool(run.x.get("seeded_fixture")),
        }
    if args.print_login and failure is None:
        user, pw = run.x["login_a"]
        site = os.environ.get("KN_SITE", "http://localhost:3000")
        print(f"demo login (fictional, local only): {user} / {pw}\n  plan page: {site}/en/plans/{run.x['plan']['id']}")
    Path(args.report).write_text(json.dumps(report, indent=2))
    print(
        "RESULT: "
        + (
            (
                f"STOPPED EARLY after {args.stop_after}: a demo, not a full pass"
                if args.stop_after
                else "PASS (all fictional data; report in " + args.report + ")"
            )
            if failure is None
            else f"FAIL at step {failure.step}"
        )
    )
    return 0 if failure is None else 1


if __name__ == "__main__":
    raise SystemExit(main())

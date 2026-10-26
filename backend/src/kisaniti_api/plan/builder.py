"""Builds a plan/v1 proposal document from already-loaded inputs (L3_PLAN_CONTRACT_v1.md). No database, no clock, no
network: the same inputs give the same document, which is what makes a proposal checkable afterwards. The routes load
the inputs inside the tenant transaction and store what this returns."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from fractions import Fraction
from typing import Any

from kisaniti_core.allocation import (
    AllocationInput,
    InputValidationError,
    Plan,
    build_result,
    parse_input,
    solve,
)
from kisaniti_core.allocation.model import POLICY_VERSIONS
from kisaniti_core.forecast import Basis, HorizonForecast, IndexPoint, milli_to_index_json
from kisaniti_core.quantity import Quantity, RatePerQuintal
from kisaniti_core.timeutil import format_instant

PLAN_POLICY_VERSIONS = {
    "index": "INDEX_DATED_ONLY_DEMO_V1",
    "forecast": "BASELINE_FORECAST_V1",
    "range": "RANGE_ABS_ERROR_CONFORMAL_V1",
    "plan_forecast": "PLAN_USES_NAIVE_DEMO_V1",
    "basis": "BASIS_RECENT_MEDIAN_DEMO_V1",
    "transport": "TRANSPORT_AS_ENTERED_V1",
    "waiting_cost": "WAITING_COST_NOT_MODELLED_V1",
    "storage": "STORAGE_FROM_LINKED_SENSOR_V1",
    "lots": "PLAN_LOTS_V1",
    "increment": "PLAN_INCREMENT_DEMO_V1",
}
ASSESSMENT_MAX_AGE = timedelta(hours=2)
UNKNOWN_REASONS = {"NO_ASSIGNMENT", "NOT_IN_GRAIN", "SETTLING", "STALE", "LOW_COVERAGE"}


class PlanRefusal(Exception):
    """The request cannot become a proposal; ``issue_code`` is one of the contract's request issue codes."""

    def __init__(self, issue_code: str, message: str, path: str | None = None) -> None:
        super().__init__(message)
        self.issue_code, self.message, self.path = issue_code, message, path


@dataclass(frozen=True, slots=True)
class SensorState:
    """What is known about the sensor of one lot at plan time (None fields when there is no link or assessment)."""

    device_id: str | None
    category: str | None  # of the latest stored assessment
    unknown_reasons: tuple[str, ...]
    assessed_at: datetime | None


@dataclass(frozen=True, slots=True)
class LotInput:
    lot_id: str
    member_id: str
    member_origin: str
    variety: str | None
    available_g: int
    sensor: SensorState


def resolve_storage(s: SensorState, now: datetime) -> tuple[str, str | None]:
    """STORAGE_FROM_LINKED_SENSOR_V1: (category, reason). UNKNOWN is never given an invented rank."""
    if s.device_id is None:
        return "UNKNOWN", "NO_SENSOR_LINK"
    if s.assessed_at is None or s.category is None:
        return "UNKNOWN", "NO_ASSESSMENT"
    if now - s.assessed_at > ASSESSMENT_MAX_AGE:
        return "UNKNOWN", "ASSESSMENT_OLD"
    if s.category == "UNKNOWN":
        known = [r for r in s.unknown_reasons if r in UNKNOWN_REASONS]
        return "UNKNOWN", known[0] if known else "NO_ASSESSMENT"
    return s.category, None


def _sha(doc: Any) -> str:
    return hashlib.sha256(json.dumps(doc, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _range(r: tuple[RatePerQuintal, RatePerQuintal] | None) -> dict[str, str] | None:
    return None if r is None else {"low_inr_per_quintal": r[0].to_json(), "high_inr_per_quintal": r[1].to_json()}


def forecast_set_doc(
    *,
    variety: str,
    dataset: dict[str, Any],
    points: list[IndexPoint],
    forecasts: dict[str, list[HorizonForecast]],
    basis: list[Basis],
    mandi_names: dict[str, str],
) -> dict[str, Any]:
    """ForecastSetV1 for one variety, from the newest index day."""
    pts = sorted(points, key=lambda p: p.day)
    origin = pts[-1]
    in_season = [p for p in pts if p.season == origin.season]
    return {
        "contract": "plan.forecast_set/v1",
        "variety": variety,
        "index_variant": "dated_only",
        "dataset": dataset,
        "origin_date": origin.day.isoformat(),
        "season": origin.season,
        "index_days_in_season": len(in_season),
        "label": "Baseline forecast, not validated on held-out data",
        "signal_verdict": "NOT_ASSESSED",
        "calibration": "NOT_VERIFIED",
        "methods": [
            {
                "method": name,
                "horizons": [
                    {
                        "h": r.h,
                        "target_date": r.target_date.isoformat(),
                        "point_inr_per_quintal": r.point.to_json(),
                        "range80": _range(r.range80),
                        "range95": _range(r.range95),
                        "residual_pairs": r.residual_pairs,
                        "status": r.status,
                    }
                    for r in rows
                ],
            }
            for name, rows in forecasts.items()
        ],
        "candidate_mandis": [
            {
                "mandi_id": b.mandi_id,
                "name": mandi_names.get(b.mandi_id, b.mandi_id),
                "basis_inr_per_quintal": None if b.basis is None else b.basis.to_json(),
                "days_used": b.days_used,
                "status": "ELIGIBLE" if b.eligible else "BASIS_TOO_FEW_DAYS",
            }
            for b in sorted(basis, key=lambda b: mandi_names.get(b.mandi_id, b.mandi_id))
        ],
        "series_tail": [
            {"date": p.day.isoformat(), "index_inr_per_quintal": milli_to_index_json(p.milli), "n_mandis": p.n_mandis}
            for p in in_season[-30:]
        ],
        "policy_versions": {k: PLAN_POLICY_VERSIONS[k] for k in ("index", "forecast", "range")},
    }


def build_plan_document(
    *,
    plan_id: str,
    run_id: str,
    fpo_id: str,
    commodity_id: str,
    created_by: str,
    created_at: datetime,
    today_ist: date,
    mode: str,
    request: dict[str, Any],
    dataset: dict[str, Any],
    forecast_set: dict[str, Any],
    naive: list[HorizonForecast],
    basis: dict[str, Basis],
    mandi_names: dict[str, str],
    lots: list[LotInput],
    time_limit_s: float,
    backend: str = "highs",
) -> dict[str, Any]:
    """The whole PlanProposalV1. Raises PlanRefusal when no proposal or audit record can be made of the request."""
    variety: str = request["variety"]
    horizons: list[int] = sorted(request["horizons_days"])
    increment = Quantity.parse(request["increment_kg"], "/increment_kg")
    if increment.units <= 0:
        raise PlanRefusal("INCREMENT_INVALID", "the increment must be positive", "/increment_kg")
    origin_date = date.fromisoformat(forecast_set["origin_date"])
    point = {r.h: r.point for r in naive}

    # ---- mandis ------------------------------------------------------------------------------------------------
    mandi_rows, eligible = [], []
    for m in request["mandis"]:
        b = basis.get(m["mandi_id"])
        ok = b is not None and b.eligible
        row = {
            "mandi_id": m["mandi_id"],
            "name": mandi_names.get(m["mandi_id"], m["mandi_id"]),
            "basis_inr_per_quintal": b.basis.to_json() if ok and b is not None and b.basis is not None else None,
            "days_used": 0 if b is None else b.days_used,
            "transport_inr_per_quintal": m["transport_inr_per_quintal"],
            "eligible": ok,
            "excluded_reason": None if ok else "BASIS_TOO_FEW_DAYS",
        }
        mandi_rows.append(row)
        if ok:
            eligible.append((m, b))
    if not eligible:
        raise PlanRefusal("MANDI_NOT_ELIGIBLE", "none of the selected mandis has enough recent prices", "/mandis")

    # ---- lots --------------------------------------------------------------------------------------------------
    lot_rows, planned = [], []
    now = created_at
    for lot in sorted(lots, key=lambda x: x.lot_id):
        category, reason = resolve_storage(lot.sensor, now)
        if lot.variety != variety:
            excluded = "VARIETY_NOT_MATCHING"
        elif lot.available_g <= 0:
            excluded = "NO_STOCK"
        elif lot.available_g < increment.units:
            excluded = "LESS_THAN_ONE_INCREMENT"
        else:
            excluded = None
        lot_rows.append(
            {
                "lot_id": lot.lot_id,
                "member_id": lot.member_id,
                "available_kg": Quantity(lot.available_g).to_json(),
                "planned_kg": None if excluded else Quantity(lot.available_g).to_json(),
                "excluded_reason": excluded,
                "storage": category,
                "storage_reason": reason,
                "device_id": lot.sensor.device_id,
                "assessed_at": None if lot.sensor.assessed_at is None else format_instant(lot.sensor.assessed_at),
            }
        )
        if excluded is None:
            planned.append(lot)
    if not planned:
        raise PlanRefusal("NO_ELIGIBLE_LOTS", "no lot of this variety has stock of at least one increment", "/variety")

    # ---- the frozen optimiser input ------------------------------------------------------------------------------
    origin = "SYNTHETIC" if any(lot.member_origin == "SYNTHETIC" for lot in planned) else "REAL"
    dates = [origin_date + timedelta(days=h) for h in horizons]
    choices = []
    for lot in sorted(planned, key=lambda x: x.lot_id):
        for m, b in eligible:
            assert b is not None and b.basis is not None
            transport = RatePerQuintal.parse(m["transport_inr_per_quintal"])
            for h, d in zip(horizons, dates, strict=True):
                price = point[h] + b.basis
                if price.units <= 0:
                    raise PlanRefusal(
                        "PRICE_NOT_POSITIVE", f"forecast plus basis is not positive for {m['mandi_id']}", "/mandis"
                    )
                choices.append(
                    {
                        "lot_id": lot.lot_id,
                        "mandi_id": m["mandi_id"],
                        "date": d.isoformat(),
                        "price_inr_per_kg": price.per_kg().to_json(),
                        "cost_inr_per_kg": transport.per_kg().to_json(),
                    }
                )
    storage_of = {r["lot_id"]: r["storage"] for r in lot_rows}
    provenance: dict[str, Any] = {
        "origin": origin,
        "source_kind": "DERIVED",
        "source_ref": f"plan/v1 {plan_id} over dataset {dataset['name']}",
        "acquired_at": format_instant(created_at),
    }
    if origin == "SYNTHETIC":
        provenance["generator"] = {"script": "kisaniti_api.plan", "version": "1", "seed": 0}
    input_doc: dict[str, Any] = {
        "contract": "allocation_input/v1",
        "fpo_id": fpo_id,
        "commodity_id": commodity_id,
        "mode": mode,
        "provenance": provenance,
        "run_id": run_id,
        "allocation_mode": "PARTIAL",
        "market_load_policy": "DISABLED",
        "dates": [d.isoformat() for d in dates],
        "mandis": [{"id": m["mandi_id"]} for m, _ in eligible],
        "farmers": [{"id": f} for f in sorted({lot.member_id for lot in planned})],
        "lots": [
            {
                "id": lot.lot_id,
                "farmer_id": lot.member_id,
                "quantity_kg": Quantity(lot.available_g).to_json(),
                "increment_kg": increment.to_json(),
                "storage": storage_of[lot.lot_id],
            }
            for lot in sorted(planned, key=lambda x: x.lot_id)
        ],
        "choices": choices,
        "capacities": [],
        "policy_versions": dict(POLICY_VERSIONS),
    }
    inp = _parse(input_doc)
    input_sha = _sha(input_doc)

    # ---- solve, check, explain -----------------------------------------------------------------------------------
    status_reasons: list[str] = []
    report = None
    try:
        report = solve(inp, backend=backend, time_limit=time_limit_s)
    except Exception:  # a solver crash is a stored, visible outcome, never a silent one
        status_reasons.append("TECHNICAL_FAILURE")
    plan = report.plan if report is not None and report.plan is not None else Plan(())
    result = build_result(inp, input_doc, plan, backend, report)
    if report is None or report.plan is None:
        result["status"]["solution"] = "NONE"
        if report is None:
            result["status"].update(optimality="NONE", termination="TECHNICAL_FAILURE", certified_stages=[])
        elif "TECHNICAL_FAILURE" not in status_reasons:
            status_reasons.append("TECHNICAL_FAILURE" if report.termination == "TECHNICAL_FAILURE" else "NO_SOLUTION")
    elif report.optimality != "PROVEN_OPTIMAL":
        status_reasons.append("OPTIMALITY_NOT_PROVEN")
    elif result["status"]["allocation_outcome"] == "NONE":
        status_reasons.append("NOTHING_ALLOCATED")
    status = "NOT_PROPOSED" if status_reasons else "PROPOSED"

    warnings = _warnings(dataset, origin_date, today_ist, forecast_set, horizons, lot_rows, planned, result, origin)
    explanation = _explanation(
        dataset, origin_date, today_ist, mandi_rows, lot_rows, planned, increment, result, horizons
    )
    return {
        "contract": "plan.proposal/v1",
        "id": plan_id,
        "fpo_id": fpo_id,
        "commodity_id": commodity_id,
        "variety": variety,
        "created_at": format_instant(created_at),
        "created_by": created_by,
        "status": status,
        "not_proposed_reasons": status_reasons,
        "request": {**request, "horizons_days": horizons},
        "dataset": dataset,
        "origin_date": origin_date.isoformat(),
        "forecast": forecast_set,
        "mandis": mandi_rows,
        "lots": lot_rows,
        "allocation_input": input_doc,
        "allocation_input_sha256": input_sha,
        "allocation_result": result,
        "validation": result["validation"],
        "warnings": warnings,
        "explanation": explanation,
        "policy_versions": dict(PLAN_POLICY_VERSIONS),
        "origin": origin,
        "provenance": provenance,
    }


def _parse(input_doc: dict[str, Any]) -> AllocationInput:
    try:
        return parse_input(input_doc)
    except InputValidationError as e:
        first = e.issues[0]
        raise PlanRefusal(first.get("code", "VALIDATION_FAILED"), first["message"], first.get("path")) from e


def _warnings(
    dataset: dict[str, Any],
    origin_date: date,
    today: date,
    forecast_set: dict[str, Any],
    horizons: list[int],
    lot_rows: list[dict[str, Any]],
    planned: list[LotInput],
    result: dict[str, Any],
    origin: str,
) -> list[dict[str, Any]]:
    w: list[dict[str, Any]] = []

    def add(code: str, **params: str) -> None:
        w.append({"code": code, "params": params})

    if dataset["validation_status"] != "VALIDATED":
        add("PRICE_DATASET_UNCHECKED", status=dataset["validation_status"])
    if origin_date < today:
        add("PRICE_DATA_OLDER_THAN_TODAY", days=str((today - origin_date).days), origin_date=origin_date.isoformat())
    naive = next(m for m in forecast_set["methods"] if m["method"] == "NAIVE")
    no_range = [str(h["h"]) for h in naive["horizons"] if h["h"] in horizons and h["status"] != "RANGE_AVAILABLE"]
    if no_range:
        add("RANGE_NOT_AVAILABLE", horizons=",".join(no_range))
    included = {lot.lot_id for lot in planned}
    if all(r["storage"] == "UNKNOWN" for r in lot_rows if r["lot_id"] in included):
        add("STORAGE_ALL_UNKNOWN")
    totals = result["totals"]
    if totals["scheduled_kg"] != totals["participating_kg"]:
        add("NOT_ALL_STOCK_ALLOCATED", scheduled_kg=totals["scheduled_kg"], participating_kg=totals["participating_kg"])
    if origin == "SYNTHETIC":
        add("SYNTHETIC_DEMO_DATA")
    if len(horizons) > 1:
        add("PRICE_SAME_ON_ALL_DATES")
    return w


def _explanation(
    dataset: dict[str, Any],
    origin_date: date,
    today: date,
    mandi_rows: list[dict[str, Any]],
    lot_rows: list[dict[str, Any]],
    planned: list[LotInput],
    increment: Quantity,
    result: dict[str, Any],
    horizons: list[int],
) -> list[dict[str, Any]]:
    e: list[dict[str, Any]] = []

    def add(code: str, **params: str) -> None:
        e.append({"code": code, "params": params})

    add("FORECAST_METHOD", method="NAIVE")
    add("FORECAST_NOT_VALIDATED")
    add("PRICE_DATASET_STATUS", status=dataset["validation_status"])
    add("PRICE_DATA_AGE", origin_date=origin_date.isoformat(), days=str((today - origin_date).days))
    for m in mandi_rows:
        if m["eligible"]:
            add(
                "MANDI_BASIS",
                mandi_id=m["mandi_id"],
                basis_inr_per_quintal=m["basis_inr_per_quintal"],
                days_used=str(m["days_used"]),
            )
            add("TRANSPORT_AS_ENTERED", mandi_id=m["mandi_id"], inr_per_quintal=m["transport_inr_per_quintal"])
        else:
            add("MANDI_EXCLUDED", mandi_id=m["mandi_id"], reason=m["excluded_reason"], days_used=str(m["days_used"]))
    add("WAITING_COST_NOT_MODELLED")
    for r in lot_rows:
        if r["excluded_reason"]:
            add("LOT_EXCLUDED", lot_id=r["lot_id"], reason=r["excluded_reason"])
        elif r["storage"] in ("HIGH", "MEDIUM"):
            add("STORAGE_PRIORITY", lot_id=r["lot_id"], category=r["storage"])
        elif r["storage"] == "UNKNOWN":
            add("STORAGE_UNKNOWN", lot_id=r["lot_id"], reason=r["storage_reason"] or "NO_ASSESSMENT")
    for lot in planned:
        scheduled = next(x for x in result["lots"] if x["lot_id"] == lot.lot_id)["unallocated_kg"]
        if Quantity.parse(scheduled).units > 0:
            add("INCREMENT_REMAINDER", lot_id=lot.lot_id, remainder_kg=scheduled, increment_kg=increment.to_json())
    farmers = result["farmers"]
    if farmers and result["status"]["solution"] == "FOUND":
        lowest = min(
            farmers,
            key=lambda f: Fraction(
                Quantity.parse(f["fulfilment"]["numerator_kg"]).units,
                Quantity.parse(f["fulfilment"]["denominator_kg"]).units,
            ),
        )
        add("FAIRNESS_RULE", rule="LEXIMIN_FULFILMENT_V1", lowest_fulfilment=lowest["fulfilment"]["display"])
        for f in farmers:
            add("FARMER_FULFILMENT", farmer_id=f["farmer_id"], display=f["fulfilment"]["display"])
    if len(horizons) > 1:
        add("DATE_BY_TIE_BREAK", rule="TIE_BREAK_V1")
    totals = result["totals"]
    add("OUTCOME", allocated_kg=totals["scheduled_kg"], net_return_inr=totals["expected_net_return_inr"])
    return e


def summary_of(doc: dict[str, Any]) -> dict[str, Any]:
    """PlanSummaryV1 from a stored proposal."""
    totals = doc["allocation_result"]["totals"]
    return {
        "contract": "plan.summary/v1",
        "id": doc["id"],
        "created_at": doc["created_at"],
        "created_by": doc["created_by"],
        "variety": doc["variety"],
        "status": doc["status"],
        "origin": doc["origin"],
        "lots": sum(1 for r in doc["lots"] if r["excluded_reason"] is None),
        "farmers": len(doc["allocation_input"]["farmers"]),
        "allocated_kg": totals["scheduled_kg"],
        "net_return_inr": totals["expected_net_return_inr"],
        "warnings": [w["code"] for w in doc["warnings"]],
    }


__all__ = ["PLAN_POLICY_VERSIONS", "LotInput", "PlanRefusal", "SensorState", "build_plan_document", "summary_of"]

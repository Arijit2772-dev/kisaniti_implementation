"""Build an allocation_result/v1 document from a plan (optimizer, heuristic or manual)."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from typing import Any

from ..quantity import Quantity, ratio_display
from .milp import SolveReport
from .model import AllocationInput, AllocationMode, Plan, plan_to_json
from .objective import PRIORITY_CATEGORIES, evaluate
from .validator import validate_plan


def input_digest(doc: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(doc, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def build_result(
    inp: AllocationInput, input_doc: dict[str, Any], plan: Plan, solver: str, report: SolveReport | None = None
) -> dict[str, Any]:
    val = validate_plan(inp, plan)
    obj = evaluate(inp, plan)
    per_lot: dict[str, int] = defaultdict(int)
    per_farmer: dict[str, int] = defaultdict(int)
    for a in plan.assignments:
        per_lot[a.lot_id] += a.quantity.units
        per_farmer[inp.lot(a.lot_id).farmer_id] += a.quantity.units
    participating = sum(lot.quantity.units for lot in inp.lots)
    if obj.allocated_g == 0:
        outcome = "NONE"
    elif obj.allocated_g == participating:
        outcome = "FULL"
    else:
        outcome = "PARTIAL"
    if inp.allocation_mode is AllocationMode.FULL and outcome != "FULL":
        outcome = "NONE"
    storage: dict[str, Any] = {}
    i = 0
    for cat in PRIORITY_CATEGORIES:
        total, per_date = obj.storage_g[i], obj.storage_g[i + 1 : i + 1 + len(inp.dates)]
        i += 1 + len(inp.dates)
        storage[cat.value] = {
            "scheduled_kg": Quantity(total).to_json(),
            "by_date_kg": [Quantity(q).to_json() for q in per_date],
        }
    farmers = []
    for f in inp.fairness_farmers:
        num, den = Quantity(per_farmer[f]), Quantity(inp.participating_g(f))
        farmers.append(
            {
                "farmer_id": f,
                "fulfilment": {
                    "numerator_kg": num.to_json(),
                    "denominator_kg": den.to_json(),
                    "display": ratio_display(num, den),
                },
            }
        )
    status = {
        "solution": "FOUND",
        "allocation_outcome": outcome,
        "optimality": report.optimality if report else "NOT_APPLICABLE_HEURISTIC",
        "termination": report.termination if report else "COMPLETED",
        "certified_stages": list(report.certified_stages) if report else [],
    }
    return {
        "contract": "allocation_result/v1",
        "fpo_id": inp.fpo_id,
        "commodity_id": inp.commodity_id,
        "input_sha256": input_digest(input_doc),
        "origin": inp.origin.value,
        "mode": inp.mode.value,
        "solver": {"name": solver, "hierarchy": report.hierarchy.value if report else "HEURISTIC"},
        "status": status,
        "assignments": plan_to_json(plan),
        "lots": [
            {
                "lot_id": lot.id,
                "participating_kg": lot.quantity.to_json(),
                "scheduled_kg": Quantity(per_lot[lot.id]).to_json(),
                "unallocated_kg": Quantity(lot.quantity.units - per_lot[lot.id]).to_json(),
            }
            for lot in inp.lots
        ],
        "farmers": farmers,
        "storage": storage,
        "totals": {
            "participating_kg": Quantity(participating).to_json(),
            "scheduled_kg": val.scheduled.to_json(),
            "gross_inr": val.gross.to_json(),
            "cost_inr": val.cost.to_json(),
            "expected_net_return_inr": val.net.to_json(),
        },
        "tie_break_score": obj.tie_break,
        "validation": val.to_json(),
    }

"""Independent PlanValidator (architecture §47; contract plan_validation/v1).

It re-checks hard validity and recomputes money for optimizer, edited and manual plans. It does not
import the optimizer, the oracle or the objective module, calls no provider, and does not judge
optimality or fairness: a hard-valid plan may be less fair than the optimum.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from ..quantity import Money, Quantity, line_amount
from .model import AllocationInput, AllocationMode, MarketLoad, Plan


@dataclass
class ValidationReport:
    valid: bool
    issues: list[dict[str, str]] = field(default_factory=list)
    scheduled: Quantity = field(default_factory=Quantity.zero)
    gross: Money = field(default_factory=Money.zero)
    cost: Money = field(default_factory=Money.zero)
    net: Money = field(default_factory=Money.zero)
    limit_violations: int = 0
    excess: Quantity = field(default_factory=Quantity.zero)

    def to_json(self) -> dict[str, Any]:
        return {
            "contract": "plan_validation/v1",
            "valid": self.valid,
            "issues": self.issues,
            "recomputed": {
                "scheduled_kg": self.scheduled.to_json(),
                "gross_inr": self.gross.to_json(),
                "cost_inr": self.cost.to_json(),
                "expected_net_return_inr": self.net.to_json(),
            },
            "market_load": {"limit_violations": self.limit_violations, "excess_kg": self.excess.to_json()},
        }


def validate_plan(inp: AllocationInput, plan: Plan, claimed: dict[str, Any] | None = None) -> ValidationReport:
    """Check a plan against its frozen input. ``claimed`` (an allocation_result/v1 document) is compared
    with the recomputed totals and fulfilment when given."""
    issues: list[dict[str, str]] = []

    def bad(code: str, path: str, msg: str) -> None:
        issues.append({"code": code, "path": path, "message": msg, "severity": "ERROR"})

    choices = {(c.lot_id, c.mandi_id, c.date): c for c in inp.choices}
    lots = {lot.id: lot for lot in inp.lots}
    per_lot: dict[str, int] = defaultdict(int)
    load: dict[tuple[str, date], int] = defaultdict(int)
    seen: set[tuple[str, str, date]] = set()
    gross, cost, total = Money.zero(), Money.zero(), 0
    for i, a in enumerate(plan.assignments):
        p = f"/assignments/{i}"
        key = (a.lot_id, a.mandi_id, a.date)
        if a.lot_id not in lots:
            bad("TENANT_SCOPE_VIOLATION", p, "lot is not part of this input")
            continue
        if key in seen:
            bad("VALIDATION_FAILED", p, "duplicate assignment for the same lot, mandi and date")
        seen.add(key)
        c = choices.get(key)
        if c is None:
            bad("VALIDATION_FAILED", p, "not an admissible (lot, mandi, date) choice")
            continue
        if a.quantity.is_zero():
            bad("OUT_OF_RANGE", f"{p}/quantity_kg", "assigned quantity must be positive")
        if a.quantity.units % lots[a.lot_id].increment.units:
            bad("VALIDATION_FAILED", f"{p}/quantity_kg", "quantity is not a whole number of planning increments")
        per_lot[a.lot_id] += a.quantity.units
        load[(a.mandi_id, a.date)] += a.quantity.units
        total += a.quantity.units
        gross = gross + line_amount(a.quantity, c.price)
        cost = cost + line_amount(a.quantity, c.cost)
    for lot_id, q in per_lot.items():
        lot = lots[lot_id]
        if q > lot.quantity.units:
            bad("VALIDATION_FAILED", f"/lots/{lot_id}", "lot over-allocated")
    if inp.allocation_mode is AllocationMode.FULL:
        for lot in inp.lots:
            if per_lot.get(lot.id, 0) != lot.quantity.units:
                bad("VALIDATION_FAILED", f"/lots/{lot.id}", "FULL mode: lot not fully allocated")
    violations, excess = 0, 0
    if inp.market_load is MarketLoad.STRICT:
        for k, q in load.items():
            capq = inp.capacities[k].units
            if q > capq:
                violations += 1
                excess += q - capq
                bad(
                    "VALIDATION_FAILED",
                    f"/market_load/{k[0]}/{k[1].isoformat()}",
                    f"mandi-day limit exceeded by {Quantity(q - capq).to_json()} kg",
                )
    rep = ValidationReport(not issues, issues, Quantity(total), gross, cost, gross - cost, violations, Quantity(excess))
    if claimed is not None:
        t = claimed.get("totals", {})
        for name, mine in (
            ("scheduled_kg", rep.scheduled.to_json()),
            ("gross_inr", gross.to_json()),
            ("cost_inr", cost.to_json()),
            ("expected_net_return_inr", rep.net.to_json()),
        ):
            if t.get(name) != mine:
                bad("VALIDATION_FAILED", f"/totals/{name}", f"claimed {t.get(name)} but recomputed {mine}")
        sched: dict[str, int] = defaultdict(int)
        for a in plan.assignments:
            if a.lot_id in lots:
                sched[lots[a.lot_id].farmer_id] += a.quantity.units
        for row in claimed.get("farmers", []):
            f = row["farmer_id"]
            num, den = row["fulfilment"]["numerator_kg"], row["fulfilment"]["denominator_kg"]
            want_den = sum(lot.quantity.units for lot in inp.lots if lot.farmer_id == f)
            if num != Quantity(sched[f]).to_json() or den != Quantity(want_den).to_json():
                bad("VALIDATION_FAILED", f"/farmers/{f}", "claimed fulfilment does not match the plan")
        rep.valid = not issues
    return rep

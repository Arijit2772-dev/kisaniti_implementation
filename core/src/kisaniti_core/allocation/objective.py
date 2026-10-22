"""Exact evaluation of the lexicographic business objective (no solver, no floats).

The objective tuple, compared lexicographically (larger is better):
  1. allocated quantity (grams)                                   — maximise
  2. sorted farmer fulfilment ratios S_f / P_f, ascending (Fractions) — leximin
  3. storage: HIGH total, HIGH per date (earliest first), then MEDIUM total, MEDIUM per date
  4. expected net selling return (exact Fraction of rupees)        — maximise
  5. tie-break score TIE_BREAK_V1 (negated, so a smaller score wins)
Comparing the sorted fulfilment vectors lexicographically is the same as comparing the prefix sums
L_1, L_2, ... of the k smallest ratios (architecture §35).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from fractions import Fraction

from .model import AllocationInput, Plan, StorageCategory

PRIORITY_CATEGORIES = (StorageCategory.HIGH, StorageCategory.MEDIUM)


@dataclass(frozen=True, slots=True)
class ObjectiveTuple:
    allocated_g: int
    fulfilment_sorted: tuple[Fraction, ...]
    storage_g: tuple[int, ...]
    net_return_inr: Fraction
    tie_break: int

    def key(self) -> tuple[int, tuple[Fraction, ...], tuple[int, ...], Fraction, int]:
        return (self.allocated_g, self.fulfilment_sorted, self.storage_g, self.net_return_inr, -self.tie_break)

    def prefix_sums(self) -> tuple[Fraction, ...]:
        out, s = [], Fraction(0)
        for r in self.fulfilment_sorted:
            s += r
            out.append(s)
        return tuple(out)


def choice_ranks(inp: AllocationInput) -> dict[tuple[str, str, date], int]:
    return {c.key: i for i, c in enumerate(inp.choices_canonical)}


def increments_of(inp: AllocationInput, plan: Plan) -> dict[tuple[str, str, date], int]:
    """Whole increments per choice; raises ValueError if a quantity is not a whole number of increments."""
    n: dict[tuple[str, str, date], int] = defaultdict(int)
    for a in plan.assignments:
        q, r = divmod(a.quantity.units, inp.lot(a.lot_id).increment.units)
        if r:
            raise ValueError(f"{a.key} quantity is not a whole number of increments")
        n[a.key] += q
    return dict(n)


def fulfilment(inp: AllocationInput, plan: Plan) -> dict[str, Fraction]:
    sched: dict[str, int] = defaultdict(int)
    for a in plan.assignments:
        sched[inp.lot(a.lot_id).farmer_id] += a.quantity.units
    return {f: Fraction(sched[f], inp.participating_g(f)) for f in inp.fairness_farmers}


def evaluate(
    inp: AllocationInput, plan: Plan, net_override: Mapping[tuple[str, str, date], int] | None = None
) -> ObjectiveTuple:
    """Exact objective tuple of a plan. ``net_override`` (net 1/10,000 ₹ per kg by choice key) scores the same
    plan under other prices, e.g. the actual prices instead of the planning prices."""
    choices = {c.key: c for c in inp.choices}
    ranks = choice_ranks(inp)
    allocated = sum(a.quantity.units for a in plan.assignments)
    fair = tuple(sorted(fulfilment(inp, plan).values()))
    by_cat_date: dict[tuple[StorageCategory, date], int] = defaultdict(int)
    net = 0
    tie = 0
    for a in plan.assignments:
        lot = inp.lot(a.lot_id)
        by_cat_date[(lot.storage, a.date)] += a.quantity.units
        rate = net_override[a.key] if net_override is not None else choices[a.key].net_units
        net += a.quantity.units * rate
        tie += ranks[a.key] * (a.quantity.units // lot.increment.units)
    storage: list[int] = []
    for cat in PRIORITY_CATEGORIES:
        per_date = [by_cat_date[(cat, d)] for d in inp.dates]
        storage += [sum(per_date), *per_date]
    # grams x (1/10,000 ₹ per kg) = 1e-7 ₹
    return ObjectiveTuple(allocated, fair, tuple(storage), Fraction(net, 10**7), tie)


def gini(values: list[Fraction] | tuple[Fraction, ...]) -> float:
    """Gini coefficient of fulfilment ratios (0 = all equal). Reporting only."""
    xs = sorted(float(v) for v in values)
    n, total = len(xs), sum(xs)
    if n == 0 or total == 0:
        return 0.0
    cum = sum((i + 1) * x for i, x in enumerate(xs))
    return (2 * cum) / (n * total) - (n + 1) / n

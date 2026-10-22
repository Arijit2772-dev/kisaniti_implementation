"""Greedy water-filling heuristic (a fast fallback and a research baseline, not an exact method).

Repeatedly gives one increment to the farmer with the lowest current fulfilment (ties: farmer ID).
For that farmer it picks the admissible choice with spare lot quantity and spare mandi-day capacity
that is best by: HIGH storage first, then MEDIUM, then the earliest date for those, then the highest
net return per kg, then canonical order. It follows the same priorities as the exact hierarchy but
gives no optimality guarantee.
"""

from __future__ import annotations

import heapq
from collections import defaultdict
from datetime import date
from fractions import Fraction

from .model import AllocationInput, AllocationMode, Choice, MarketLoad, Plan, StorageCategory

_PRI = {StorageCategory.HIGH: 0, StorageCategory.MEDIUM: 1, StorageCategory.LOW: 2, StorageCategory.UNKNOWN: 2}


def solve_greedy(inp: AllocationInput) -> Plan:
    strict = inp.market_load is MarketLoad.STRICT
    cap = {k: v.units for k, v in inp.capacities.items()}
    load: dict[tuple[str, date], int] = defaultdict(int)
    left = {lot.id: lot.max_increments for lot in inp.lots}
    by_farmer: dict[str, list[Choice]] = defaultdict(list)
    for c in inp.choices_canonical:
        by_farmer[inp.lot(c.lot_id).farmer_id].append(c)
    order = {c.key: i for i, c in enumerate(inp.choices_canonical)}
    for f, cs in by_farmer.items():
        cs.sort(
            key=lambda c: (
                _PRI[inp.lot(c.lot_id).storage],
                c.date if _PRI[inp.lot(c.lot_id).storage] < 2 else date.min,
                -c.net_units,
                order[c.key],
            )
        )
        by_farmer[f] = cs
    P = {f: inp.participating_g(f) for f in inp.fairness_farmers}
    sched: dict[str, int] = defaultdict(int)
    n: dict[tuple[str, str, date], int] = defaultdict(int)
    heap = [(Fraction(0), f) for f in P]
    heapq.heapify(heap)
    while heap:
        _, f = heapq.heappop(heap)
        for c in by_farmer.get(f, []):
            lot = inp.lot(c.lot_id)
            inc = lot.increment.units
            if left[lot.id] <= 0:
                continue
            if strict and load[(c.mandi_id, c.date)] + inc > cap[(c.mandi_id, c.date)]:
                continue
            left[lot.id] -= 1
            load[(c.mandi_id, c.date)] += inc
            n[c.key] += 1
            sched[f] += inc
            heapq.heappush(heap, (Fraction(sched[f], P[f]), f))
            break
    plan = Plan.from_increments(inp, n)
    if inp.allocation_mode is AllocationMode.FULL and any(v > 0 for v in left.values()):
        raise ValueError("greedy could not place every lot in FULL mode")
    return plan

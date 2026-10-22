"""Brute-force reference solver for tiny cases: enumerates every feasible integer plan.

It shares only the input types and the exact objective with the optimizer, never its model, so it
is an independent check of the complete lexicographic objective (acceptance test T30).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date

from .model import AllocationInput, AllocationMode, Choice, MarketLoad, Plan
from .objective import ObjectiveTuple, evaluate


class TooLarge(Exception):
    """The enumeration would exceed the requested limit."""


@dataclass(frozen=True, slots=True)
class OracleResult:
    best: ObjectiveTuple
    best_plans: tuple[Plan, ...]
    plans_enumerated: int


def _compositions(total: int, parts: int) -> Iterator[tuple[int, ...]]:
    if parts == 0:
        if total == 0:
            yield ()
        return
    if parts == 1:
        yield (total,)
        return
    for first in range(total + 1):
        for rest in _compositions(total - first, parts - 1):
            yield (first, *rest)


def _lot_options(inp: AllocationInput, choices: list[Choice], max_inc: int) -> list[tuple[int, ...]]:
    totals = [max_inc] if inp.allocation_mode is AllocationMode.FULL else list(range(max_inc + 1))
    return [v for t in totals for v in _compositions(t, len(choices))]


def solve_oracle(inp: AllocationInput, max_plans: int = 2_000_000) -> OracleResult:
    by_lot: dict[str, list[Choice]] = defaultdict(list)
    for c in inp.choices_canonical:
        by_lot[c.lot_id].append(c)
    lots = sorted(inp.lots, key=lambda lot: lot.id)
    options = [_lot_options(inp, by_lot[lot.id], lot.max_increments) for lot in lots]
    size = 1
    for o in options:
        size *= max(1, len(o))
    if size > max_plans:
        raise TooLarge(f"{size} combinations > {max_plans}")
    strict = inp.market_load is MarketLoad.STRICT
    cap = {k: v.units for k, v in inp.capacities.items()}

    best: ObjectiveTuple | None = None
    best_plans: list[Plan] = []
    count = 0
    load: dict[tuple[str, date], int] = defaultdict(int)
    chosen: dict[tuple[str, str, date], int] = {}

    def rec(i: int) -> None:
        nonlocal best, count
        if i == len(lots):
            count += 1
            plan = Plan.from_increments(inp, chosen)
            val = evaluate(inp, plan)
            if best is None or val.key() > best.key():
                best, best_plans[:] = val, [plan]
            elif val.key() == best.key():
                best_plans.append(plan)
            return
        lot, chs = lots[i], by_lot[lots[i].id]
        if not chs:  # a lot with no admissible choice can only stay unallocated
            if inp.allocation_mode is AllocationMode.FULL:
                return
            rec(i + 1)
            return
        for vec in options[i]:
            ok = True
            for c, k in zip(chs, vec, strict=True):
                if k and strict and load[(c.mandi_id, c.date)] + k * lot.increment.units > cap[(c.mandi_id, c.date)]:
                    ok = False
                    break
            if not ok:
                continue
            for c, k in zip(chs, vec, strict=True):
                if k:
                    load[(c.mandi_id, c.date)] += k * lot.increment.units
                    chosen[c.key] = k
            rec(i + 1)
            for c, k in zip(chs, vec, strict=True):
                if k:
                    load[(c.mandi_id, c.date)] -= k * lot.increment.units
                    del chosen[c.key]

    rec(0)
    if best is None:
        raise ValueError("no feasible plan (FULL mode with insufficient capacity or choices)")
    return OracleResult(best, tuple(best_plans), count)

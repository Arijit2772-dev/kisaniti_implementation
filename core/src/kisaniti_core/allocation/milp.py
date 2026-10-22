"""Exact staged lexicographic MILP (architecture §34–§40; contract CONTRACT_SPEC_V1_L3d_allocation.md).

Stages for the KisanNiti hierarchy (each optimum is preserved before the next stage):
  ALLOCATION  max total allocated quantity
  LEXIMIN     for k = 1..|F|: max L_k = sum of the k smallest fulfilment ratios (Ogryczak's
              cumulative formulation k*theta - sum_f w_f, w_f >= theta - r_f). After stage k the k-th
              value v_k is known exactly, and L_k* is preserved with theta fixed at v_k:
              sum_f (v_k - r_f)^+ <= k*v_k - L_k*. One preserve row set per distinct value (equal
              values give identical rows). When v_k = 1 every later value is 1, so the remaining
              stages collapse into one linear row sum_f r_f >= L_|F|*.
  STORAGE     HIGH total, then HIGH on each date (earliest first), then the same for MEDIUM
  NER         max expected net selling return
  TIE_BREAK   min sum_e rank(e) * y_e, rank = position in (lot ID, date, mandi ID) order

Every stage objective except LEXIMIN is integer-valued after exact scaling, so it is preserved
exactly (>= optimum - 1/2). Fairness values are recomputed exactly from the integer plan after each
stage; the solver's floating theta is never used as business truth (architecture §46). A final exact
check confirms that later stages did not lose any preserved fairness value.

Research alternatives use the same machinery: MAXMIN (only L_1), UTILITARIAN (no fairness stage)
and WEIGHTED (one blended objective, the weighted-sum design that DESIGN_ALTERNATIVES rejects).
"""

from __future__ import annotations

import math
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from fractions import Fraction

from .backends import Backend, LinearModel, SolveStatus, make_backend
from .model import AllocationInput, AllocationMode, MarketLoad, Plan, StorageCategory
from .objective import ObjectiveTuple, choice_ranks, evaluate

INTEGER_GAP = 0.49
FAIR_GAP = 1e-9
FAIR_TOL = Fraction(1, 10**9)


class Hierarchy(StrEnum):
    KISANITI = "KISANITI"  # allocation > leximin > storage > net return > tie-break
    MAXMIN = "MAXMIN"  # allocation > max-min only > net return > tie-break
    UTILITARIAN = "UTILITARIAN"  # allocation > net return > tie-break
    WEIGHTED = "WEIGHTED"  # one weighted sum of normalised net return, storage and min fulfilment


@dataclass(frozen=True, slots=True)
class Weights:
    net_return: Fraction
    storage: Fraction
    fairness: Fraction


@dataclass
class StageLog:
    name: str
    status: str
    seconds: float
    value: str = ""


@dataclass
class SolveReport:
    plan: Plan | None
    objective: ObjectiveTuple | None
    solver: str
    hierarchy: Hierarchy
    optimality: str  # PROVEN_OPTIMAL | FEASIBLE_NOT_PROVEN | NONE
    termination: str  # COMPLETED | PROVEN_INFEASIBLE | TIME_LIMIT | NOT_APPLICABLE | TECHNICAL_FAILURE
    certified_stages: list[str] = field(default_factory=list)
    stages: list[StageLog] = field(default_factory=list)
    build_seconds: float = 0.0
    solve_seconds: float = 0.0
    detail: str = ""
    n_vars: int = 0
    n_rows: int = 0

    @property
    def n_solves(self) -> int:
        return len(self.stages)


class _Stop(Exception):
    def __init__(self, termination: str, detail: str) -> None:
        super().__init__(detail)
        self.termination = termination
        self.detail = detail


class StagedSolver:
    def __init__(self, inp: AllocationInput, backend: Backend, time_limit: float) -> None:
        self.inp = inp
        self.backend = backend
        self.deadline = time.perf_counter() + time_limit
        self.m = LinearModel()
        self.report = SolveReport(None, None, backend.version(), Hierarchy.KISANITI, "NONE", "COMPLETED")
        self.values: list[float] | None = None
        # CP-SAT works in exact integers, so it needs no floating tolerance on preserved fairness rows.
        self.tol = Fraction(0) if backend.name == "cpsat" else FAIR_TOL
        t0 = time.perf_counter()
        self._build()
        self.report.build_seconds = time.perf_counter() - t0

    # ---- base model -----------------------------------------------------------------------------------
    def _build(self) -> None:
        inp, m = self.inp, self.m
        g = inp.quantum_g
        self.g = g
        self.y: dict[tuple[str, str, date], int] = {}
        self.inc_q: dict[tuple[str, str, date], int] = {}  # increment size in quanta
        by_lot: dict[str, list[int]] = defaultdict(list)
        for c in inp.choices_canonical:
            lot = inp.lot(c.lot_id)
            idx = m.add_var(0, lot.max_increments, integer=True)
            self.y[c.key] = idx
            self.inc_q[c.key] = lot.increment.units // g
            by_lot[c.lot_id].append(idx)
        for lot in inp.lots:
            row = {i: Fraction(1) for i in by_lot.get(lot.id, [])}
            if inp.allocation_mode is AllocationMode.FULL:
                m.add_row(row, lot.max_increments, lot.max_increments)
            elif row:
                m.add_row(row, None, lot.max_increments)
        if inp.market_load is MarketLoad.STRICT:
            load: dict[tuple[str, date], dict[int, Fraction]] = defaultdict(dict)
            for c in inp.choices:
                load[(c.mandi_id, c.date)][self.y[c.key]] = Fraction(self.inc_q[c.key])
            for k, row in load.items():
                m.add_row(row, None, inp.capacities[k].units // g)
        # fulfilment ratio expressions r_f = sum_e (increment_g / P_f) y_e
        self.farmers = list(inp.fairness_farmers)
        self.P = {f: inp.participating_g(f) for f in self.farmers}
        self.r: dict[str, dict[int, Fraction]] = {f: {} for f in self.farmers}
        for c in inp.choices:
            f = inp.lot(c.lot_id).farmer_id
            self.r[f][self.y[c.key]] = Fraction(inp.lot(c.lot_id).increment.units, self.P[f])
        den = 1
        for f in self.farmers:
            den = math.lcm(den, self.P[f] // math.gcd(self.P[f], g))
        self.fair_den = den

    # ---- helpers --------------------------------------------------------------------------------------
    def _remaining(self) -> float:
        return self.deadline - time.perf_counter()

    def _plan(self) -> Plan:
        assert self.values is not None
        n = {}
        for key, idx in self.y.items():
            v = self.values[idx]
            k = round(v)
            if abs(v - k) > 1e-6:
                raise _Stop("TECHNICAL_FAILURE", f"non-integral solver value {v} for {key}")
            if k:
                n[key] = k
        return Plan.from_increments(self.inp, n)

    def _solve(self, name: str, objective: dict[int, Fraction], abs_gap: float) -> Plan:
        rem = self._remaining()
        if rem <= 0:
            raise _Stop("TIME_LIMIT", f"time budget used up before stage {name}")
        out = self.backend.solve(self.m, objective, rem, abs_gap, self.values)
        self.report.solve_seconds += out.seconds
        self.report.stages.append(StageLog(name, out.status.value, out.seconds))
        if out.status is SolveStatus.NOT_APPLICABLE:
            raise _Stop("NOT_APPLICABLE", out.detail)
        if out.status is SolveStatus.INFEASIBLE:
            raise _Stop("PROVEN_INFEASIBLE", f"stage {name}: no plan satisfies the hard constraints")
        if out.status is SolveStatus.ERROR:
            raise _Stop("TECHNICAL_FAILURE", f"stage {name}: {out.status.value} {out.detail}")
        if out.status is SolveStatus.NO_SOLUTION:
            raise _Stop("TIME_LIMIT", f"stage {name}: no incumbent")
        self.values = out.values
        plan = self._plan()
        if out.status is SolveStatus.FEASIBLE:
            raise _Stop("TIME_LIMIT", f"stage {name}: time limit with an incumbent")
        self.report.certified_stages.append(name)
        return plan

    def _int_stage(self, name: str, coefs: dict[int, Fraction], minimise: bool = False) -> None:
        """Integer-valued stage objective: solve, then preserve its optimum exactly."""
        if not coefs:
            self.report.certified_stages.append(name)
            return
        obj = {k: -v for k, v in coefs.items()} if minimise else coefs
        self._solve(name, obj, INTEGER_GAP)
        assert self.values is not None
        val = sum(Fraction(round(self.values[k])) * c for k, c in coefs.items())
        self.report.stages[-1].value = str(val)
        if minimise:
            self.m.add_row(coefs, None, val + Fraction(1, 2))
        else:
            self.m.add_row(coefs, val - Fraction(1, 2), None)

    # ---- stages ---------------------------------------------------------------------------------------
    def allocation(self) -> None:
        if self.inp.allocation_mode is AllocationMode.FULL:
            self.report.certified_stages.append("ALLOCATION")
            return
        self._int_stage("ALLOCATION", {self.y[k]: Fraction(q) for k, q in self.inc_q.items()})

    def _ratios(self) -> list[Fraction]:
        plan = self._plan()
        sched: dict[str, int] = defaultdict(int)
        for a in plan.assignments:
            sched[self.inp.lot(a.lot_id).farmer_id] += a.quantity.units
        return sorted(Fraction(sched[f], self.P[f]) for f in self.farmers)

    def leximin(self, only_first: bool = False) -> None:
        if self.inp.allocation_mode is AllocationMode.FULL or not self.farmers:
            self.report.certified_stages.append("LEXIMIN")
            return
        F = len(self.farmers)
        self.v_star: list[Fraction] = []
        L_prev = Fraction(0)
        last_threshold: Fraction | None = None
        for k in range(1, F + 1):
            theta = self.m.add_var(0, 1, integer=False, den=self.fair_den)
            w = {}
            for f in self.farmers:
                wf = self.m.add_var(0, 1, integer=False, den=self.fair_den)
                w[f] = wf
                row = {wf: Fraction(1), theta: Fraction(-1)} | dict(self.r[f])  # w_f - theta + r_f >= 0
                self.m.add_row(row, 0, None)
            obj = {theta: Fraction(k)} | {wf: Fraction(-1) for wf in w.values()}
            if self.values is not None:  # extend the incumbent with a feasible theta/w
                vals = self.values + [0.0] * (len(self.m.vars) - len(self.values))
                self.values = vals
            self._solve(f"LEXIMIN_{k}", obj, FAIR_GAP)
            ratios = self._ratios()
            # verify that earlier preserved values still hold exactly
            for j, vj in enumerate(self.v_star):
                if ratios[j] != vj:
                    raise _Stop("TECHNICAL_FAILURE", f"leximin value {j + 1} drifted: {ratios[j]} != {vj}")
            v_k = ratios[k - 1]
            self.v_star.append(v_k)
            L_k = L_prev + v_k
            self.report.stages[-1].value = str(v_k)
            # This stage's theta and w stay in the model as free helpers; without the objective they
            # constrain nothing (theta = 0, w = 0 is always feasible).
            if only_first:
                self._preserve_threshold(v_k, k, L_k)
                return
            if v_k == 1:
                total = L_k + (F - k)
                sum_r: dict[int, Fraction] = defaultdict(Fraction)
                for f in self.farmers:
                    for idx, c in self.r[f].items():
                        sum_r[idx] += c
                self.m.add_row(dict(sum_r), total - self.tol, None)
                self.v_star += [Fraction(1)] * (F - k)
                self.report.stages[-1].value += f" (all remaining = 1; {F - k} stages skipped)"
                return
            if v_k != last_threshold:  # equal values give an identical preserve row (module docstring)
                self._preserve_threshold(v_k, k, L_k)
                last_threshold = v_k
            L_prev = L_k

    def _preserve_threshold(self, t: Fraction, k: int, L_k: Fraction) -> None:
        """sum_f (t - r_f)^+ <= k t - L_k*, with helper u_f >= t - r_f, u_f >= 0."""
        us = {}
        for f in self.farmers:
            u = self.m.add_var(0, 1, integer=False, den=self.fair_den)
            us[f] = u
            self.m.add_row({u: Fraction(1)} | dict(self.r[f]), t, None)  # u_f + r_f >= t
        self.m.add_row({u: Fraction(1) for u in us.values()}, None, k * t - L_k + self.tol)
        if self.values is not None:
            self.values = self.values + [0.0] * (len(self.m.vars) - len(self.values))
            ratios = {}
            plan = self._plan()
            sched: dict[str, int] = defaultdict(int)
            for a in plan.assignments:
                sched[self.inp.lot(a.lot_id).farmer_id] += a.quantity.units
            for f in self.farmers:
                ratios[f] = Fraction(sched[f], self.P[f])
                self.values[us[f]] = float(max(Fraction(0), t - ratios[f]))

    def storage(self) -> None:
        inp = self.inp
        for cat in (StorageCategory.HIGH, StorageCategory.MEDIUM):
            keys = [c.key for c in inp.choices if inp.lot(c.lot_id).storage is cat]
            if not keys:
                self.report.certified_stages += [f"STORAGE_{cat.value}"]
                continue
            self._int_stage(f"STORAGE_{cat.value}_TOTAL", {self.y[k]: Fraction(self.inc_q[k]) for k in keys})
            for d in inp.dates:
                dk = [k for k in keys if k[2] == d]
                if dk:
                    self._int_stage(
                        f"STORAGE_{cat.value}_{d.isoformat()}", {self.y[k]: Fraction(self.inc_q[k]) for k in dk}
                    )

    def net_return(self) -> None:
        coefs = {self.y[c.key]: Fraction(c.net_units * self.inc_q[c.key]) for c in self.inp.choices}
        self._int_stage("NET_RETURN", coefs)

    def tie_break(self) -> None:
        ranks = choice_ranks(self.inp)
        self._int_stage("TIE_BREAK", {self.y[k]: Fraction(ranks[k]) for k in self.y if ranks[k]}, minimise=True)

    def weighted(self, wts: Weights) -> None:
        """One blended objective: w1*NER/NER_ub + w2*(HIGH+MEDIUM on the first date)/(their quantity) + w3*min r_f."""
        inp = self.inp
        best_net: dict[str, int] = defaultdict(int)
        for c in inp.choices:
            best_net[c.lot_id] = max(best_net[c.lot_id], c.net_units)
        ner_ub = sum(Fraction(best_net[lot.id] * lot.max_increments * self.inc_q_of(lot.id)) for lot in inp.lots)
        obj: dict[int, Fraction] = defaultdict(Fraction)
        if ner_ub > 0:
            for c in inp.choices:
                obj[self.y[c.key]] += wts.net_return * c.net_units * self.inc_q[c.key] / ner_ub
        pri = [c for c in inp.choices if inp.lot(c.lot_id).storage in (StorageCategory.HIGH, StorageCategory.MEDIUM)]
        pri_q = sum(
            Fraction(lot.max_increments * self.inc_q_of(lot.id))
            for lot in inp.lots
            if lot.storage in (StorageCategory.HIGH, StorageCategory.MEDIUM)
        )
        if pri and pri_q > 0:
            for c in pri:
                if c.date == inp.dates[0]:
                    obj[self.y[c.key]] += wts.storage * self.inc_q[c.key] / pri_q
        if self.farmers:
            t = self.m.add_var(0, 1, integer=False, den=self.fair_den)
            for f in self.farmers:
                self.m.add_row({t: Fraction(-1)} | dict(self.r[f]), 0, None)  # r_f >= t
            obj[t] += wts.fairness
        self._solve("WEIGHTED", dict(obj), FAIR_GAP)

    def inc_q_of(self, lot_id: str) -> int:
        return self.inp.lot(lot_id).increment.units // self.g

    # ---- driver ---------------------------------------------------------------------------------------
    def run(self, hierarchy: Hierarchy, weights: Weights | None = None) -> SolveReport:
        rep = self.report
        rep.hierarchy = hierarchy
        try:
            if hierarchy is Hierarchy.WEIGHTED:
                assert weights is not None
                self.weighted(weights)
            else:
                self.allocation()
                if hierarchy is Hierarchy.KISANITI:
                    self.leximin()
                    self.storage()
                elif hierarchy is Hierarchy.MAXMIN:
                    self.leximin(only_first=True)
                self.net_return()
            if hierarchy is not Hierarchy.WEIGHTED:  # the blended optimum is fractional; not preserved
                self.tie_break()
            rep.optimality = "PROVEN_OPTIMAL"
        except _Stop as s:
            rep.termination = s.termination
            rep.detail = s.detail
            rep.optimality = (
                "FEASIBLE_NOT_PROVEN" if self.values is not None and s.termination == "TIME_LIMIT" else "NONE"
            )
        if self.values is not None and rep.termination in ("COMPLETED", "TIME_LIMIT"):
            rep.plan = self._plan()
            rep.objective = evaluate(self.inp, rep.plan)
            lost = hasattr(self, "v_star") and rep.objective.fulfilment_sorted != tuple(self.v_star)
            if rep.termination == "COMPLETED" and hierarchy is Hierarchy.KISANITI and lost:
                rep.termination, rep.optimality = "TECHNICAL_FAILURE", "NONE"
                rep.detail = "final plan lost a preserved leximin value"
        rep.n_vars, rep.n_rows = len(self.m.vars), len(self.m.rows)
        return rep


def solve(
    inp: AllocationInput,
    backend: str = "highs",
    hierarchy: Hierarchy = Hierarchy.KISANITI,
    time_limit: float = 300.0,
    weights: Weights | None = None,
    threads: int | None = None,
    seed: int = 0,
) -> SolveReport:
    """Solve one allocation input with the named back end and objective hierarchy."""
    return StagedSolver(inp, make_backend(backend, threads, seed), time_limit).run(hierarchy, weights)

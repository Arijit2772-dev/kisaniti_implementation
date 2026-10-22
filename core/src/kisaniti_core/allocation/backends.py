"""Solver back ends for an append-only exact linear model (HiGHS, SCIP, OR-Tools CP-SAT).

The model keeps coefficients as Fractions. HiGHS and SCIP receive floats; CP-SAT receives integers
after exact scaling (continuous variables carry a denominator ``den`` so that ``den * value`` is an
integer at every optimum we need). Each back end adds only what was appended since its last solve.
Solver libraries are imported lazily, so the core works without them.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from enum import StrEnum
from fractions import Fraction
from typing import Any, Protocol


@dataclass(slots=True)
class Var:
    lb: Fraction
    ub: Fraction
    integer: bool
    den: int = 1


@dataclass(slots=True)
class Row:
    coefs: dict[int, Fraction]
    lb: Fraction | None
    ub: Fraction | None


@dataclass
class LinearModel:
    vars: list[Var] = field(default_factory=list)
    rows: list[Row] = field(default_factory=list)

    def add_var(self, lb: Fraction | int, ub: Fraction | int, integer: bool, den: int = 1) -> int:
        self.vars.append(Var(Fraction(lb), Fraction(ub), integer, den))
        return len(self.vars) - 1

    def add_row(
        self, coefs: dict[int, Fraction], lb: Fraction | int | None = None, ub: Fraction | int | None = None
    ) -> int:
        self.rows.append(
            Row(
                {k: Fraction(v) for k, v in coefs.items() if v},
                None if lb is None else Fraction(lb),
                None if ub is None else Fraction(ub),
            )
        )
        return len(self.rows) - 1


class SolveStatus(StrEnum):
    OPTIMAL = "OPTIMAL"
    FEASIBLE = "FEASIBLE"  # time limit with an incumbent
    INFEASIBLE = "INFEASIBLE"
    NO_SOLUTION = "NO_SOLUTION"  # time limit without an incumbent
    NOT_APPLICABLE = "NOT_APPLICABLE"  # e.g. CP-SAT integer scaling too large
    ERROR = "ERROR"


@dataclass(slots=True)
class SolveOutcome:
    status: SolveStatus
    values: list[float] | None
    seconds: float
    detail: str = ""


class NotApplicable(Exception):
    pass


class Backend(Protocol):
    name: str

    def version(self) -> str: ...

    def solve(
        self,
        model: LinearModel,
        objective: dict[int, Fraction],
        time_limit: float,
        abs_gap: float,
        hint: list[float] | None,
    ) -> SolveOutcome: ...


INF = float("inf")


def _f(x: Fraction | None, default: float) -> float:
    return default if x is None else float(x)


class HighsBackend:
    name = "highs"

    def __init__(self, threads: int = 1, seed: int = 0) -> None:
        import highspy

        self._hs = highspy
        self.h = highspy.Highs()
        self.h.setOptionValue("output_flag", False)
        self.h.setOptionValue("threads", threads)
        self.h.setOptionValue("random_seed", seed)
        self.h.setOptionValue("mip_rel_gap", 0.0)
        self.h.setOptionValue("mip_feasibility_tolerance", 1e-9)
        self.h.setOptionValue("primal_feasibility_tolerance", 1e-9)
        self.nv = 0
        self.nr = 0

    def version(self) -> str:
        return f"HiGHS {self.h.version()}"

    def _sync(self, model: LinearModel) -> None:
        import numpy as np

        new_vars = model.vars[self.nv :]
        if new_vars:
            n = len(new_vars)
            self.h.addVars(n, np.array([float(v.lb) for v in new_vars]), np.array([float(v.ub) for v in new_vars]))
            idx = np.arange(self.nv, self.nv + n, dtype=np.int32)
            integ = np.array([1 if v.integer else 0 for v in new_vars], dtype=np.uint8)
            self.h.changeColsIntegrality(n, idx, integ)
            self.nv += n
        new_rows = model.rows[self.nr :]
        if new_rows:
            starts: list[int] = []
            index: list[int] = []
            value: list[float] = []
            for r in new_rows:
                starts.append(len(index))
                for k, c in r.coefs.items():
                    index.append(k)
                    value.append(float(c))
            self.h.addRows(
                len(new_rows),
                np.array([_f(r.lb, -INF) for r in new_rows]),
                np.array([_f(r.ub, INF) for r in new_rows]),
                len(index),
                np.array(starts, dtype=np.int32),
                np.array(index, dtype=np.int32),
                np.array(value),
            )
            self.nr += len(new_rows)

    def solve(
        self,
        model: LinearModel,
        objective: dict[int, Fraction],
        time_limit: float,
        abs_gap: float,
        hint: list[float] | None,
    ) -> SolveOutcome:
        import numpy as np

        hs = self._hs
        self._sync(model)
        cost = np.zeros(self.nv)
        for k, c in objective.items():
            cost[k] = float(c)
        self.h.changeColsCost(self.nv, np.arange(self.nv, dtype=np.int32), cost)
        self.h.changeObjectiveSense(hs.ObjSense.kMaximize)
        self.h.setOptionValue("time_limit", max(0.01, time_limit))
        self.h.setOptionValue("mip_abs_gap", abs_gap)
        if hint is not None:
            sol = hs.HighsSolution()
            sol.col_value = list(hint) + [0.0] * (self.nv - len(hint))
            sol.value_valid = True
            self.h.setSolution(sol)
        t0 = time.perf_counter()
        self.h.run()
        dt = time.perf_counter() - t0
        st = self.h.getModelStatus()
        info = self.h.getInfo()
        has_sol = info.primal_solution_status == 2
        values = list(self.h.getSolution().col_value) if has_sol else None
        if st == hs.HighsModelStatus.kOptimal:
            return SolveOutcome(SolveStatus.OPTIMAL, values, dt)
        if st == hs.HighsModelStatus.kInfeasible:
            return SolveOutcome(SolveStatus.INFEASIBLE, None, dt)
        if st in (hs.HighsModelStatus.kTimeLimit, hs.HighsModelStatus.kInterrupt):
            return SolveOutcome(SolveStatus.FEASIBLE if has_sol else SolveStatus.NO_SOLUTION, values, dt, str(st))
        return SolveOutcome(SolveStatus.ERROR, values, dt, str(st))


class ScipBackend:
    name = "scip"

    def __init__(self, threads: int = 1, seed: int = 0) -> None:
        import pyscipopt

        self._ps = pyscipopt
        self.m = pyscipopt.Model()
        self.m.hideOutput()
        self.m.setParam("limits/gap", 0.0)
        self.m.setParam("numerics/feastol", 1e-9)
        self.m.setParam("randomization/randomseedshift", seed)
        self.m.setParam("limits/maxorigsol", 1000)
        self.x: list[Any] = []
        self.nr = 0

    def version(self) -> str:
        return f"SCIP {self.m.version()}"

    def solve(
        self,
        model: LinearModel,
        objective: dict[int, Fraction],
        time_limit: float,
        abs_gap: float,
        hint: list[float] | None,
    ) -> SolveOutcome:
        ps = self._ps
        self.m.freeTransform()
        for i in range(len(self.x), len(model.vars)):
            v = model.vars[i]
            self.x.append(self.m.addVar(vtype="I" if v.integer else "C", lb=float(v.lb), ub=float(v.ub)))
        for r in model.rows[self.nr :]:
            expr = ps.quicksum(float(c) * self.x[k] for k, c in r.coefs.items())
            if r.lb is not None and r.ub is not None and r.lb == r.ub:
                self.m.addCons(expr == float(r.lb))
            else:
                if r.lb is not None:
                    self.m.addCons(expr >= float(r.lb))
                if r.ub is not None:
                    self.m.addCons(expr <= float(r.ub))
        self.nr = len(model.rows)
        self.m.setObjective(ps.quicksum(float(c) * self.x[k] for k, c in objective.items()), "maximize")
        self.m.setParam("limits/time", max(0.01, time_limit))
        self.m.setParam("limits/absgap", abs_gap)
        if hint is not None:
            try:  # the previous stage's plan is feasible here; a hint is optional, so a refusal is ignored
                s = self.m.createPartialSol()
                for k, val in enumerate(hint):
                    self.m.setSolVal(s, self.x[k], val)
                self.m.addSol(s)
            except Exception:
                pass
        t0 = time.perf_counter()
        self.m.optimize()
        dt = time.perf_counter() - t0
        st = self.m.getStatus()
        has = self.m.getNSols() > 0
        values = [self.m.getVal(v) for v in self.x] if has else None
        if st == "optimal":
            return SolveOutcome(SolveStatus.OPTIMAL, values, dt)
        if st == "infeasible":
            return SolveOutcome(SolveStatus.INFEASIBLE, None, dt)
        if st in ("timelimit", "userinterrupt"):
            return SolveOutcome(SolveStatus.FEASIBLE if has else SolveStatus.NO_SOLUTION, values, dt, st)
        return SolveOutcome(SolveStatus.ERROR, values, dt, st)


MAX_CPSAT_COEF = 2**40


class CpSatBackend:
    """Exact integer back end. Continuous variables are substituted by x = x'/den with x' integer."""

    name = "cpsat"

    def __init__(self, threads: int = 8, seed: int = 0) -> None:
        from ortools.sat.python import cp_model

        self._cp = cp_model
        self.model = cp_model.CpModel()
        self.x: list[Any] = []
        self.dens: list[int] = []
        self.nr = 0
        self.threads = threads
        self.seed = seed

    def version(self) -> str:
        import ortools

        return f"OR-Tools CP-SAT {getattr(ortools, '__version__', '?')}"

    def _int_row(
        self, terms: dict[int, Fraction], bounds: list[Fraction | None]
    ) -> tuple[dict[int, int], list[int | None], int]:
        scaled = {k: c / self.dens[k] for k, c in terms.items()}
        mult = 1
        for v in [*scaled.values(), *[b for b in bounds if b is not None]]:
            mult = math.lcm(mult, v.denominator)
        coefs = {k: int(v * mult) for k, v in scaled.items()}
        bs: list[int | None] = []
        for b in bounds:
            bs.append(None if b is None else int(b * mult))
        biggest = max([abs(c) for c in coefs.values()] + [abs(b) for b in bs if b is not None] + [0])
        if biggest > MAX_CPSAT_COEF:
            raise NotApplicable(f"integer scaling needs coefficients up to {biggest:.3g} (> 2^40)")
        return coefs, bs, mult

    def solve(
        self,
        model: LinearModel,
        objective: dict[int, Fraction],
        time_limit: float,
        abs_gap: float,
        hint: list[float] | None,
    ) -> SolveOutcome:
        cp = self._cp
        t0 = time.perf_counter()
        try:
            for i in range(len(self.x), len(model.vars)):
                v = model.vars[i]
                lo, hi = v.lb * v.den, v.ub * v.den
                if lo.denominator != 1 or hi.denominator != 1:
                    raise NotApplicable("variable bounds are not integral after scaling")
                if abs(hi) > MAX_CPSAT_COEF:
                    raise NotApplicable("variable bound too large")
                self.x.append(self.model.new_int_var(int(lo), int(hi), f"x{i}"))
                self.dens.append(v.den)
            for r in model.rows[self.nr :]:
                coefs, (lb, ub), _ = self._int_row(r.coefs, [r.lb, r.ub])
                expr = sum(c * self.x[k] for k, c in coefs.items())
                if lb is not None:
                    self.model.add(expr >= lb)
                if ub is not None:
                    self.model.add(expr <= ub)
            self.nr = len(model.rows)
            ocoefs, _, omult = self._int_row(objective, [])
        except NotApplicable as e:
            return SolveOutcome(SolveStatus.NOT_APPLICABLE, None, time.perf_counter() - t0, str(e))
        self.model.clear_objective()  # type: ignore[no-untyped-call]
        self.model.maximize(sum(c * self.x[k] for k, c in ocoefs.items()))
        self.model.clear_hints()  # type: ignore[no-untyped-call]
        if hint is not None:
            for k, val in enumerate(hint):
                self.model.add_hint(self.x[k], round(val * self.dens[k]))
        solver = cp.CpSolver()
        solver.parameters.max_time_in_seconds = max(0.01, time_limit)
        solver.parameters.num_workers = self.threads
        solver.parameters.random_seed = self.seed
        solver.parameters.absolute_gap_limit = max(0.0, abs_gap * omult - 1e-9) if abs_gap >= 1e-6 else 0.0
        st = solver.solve(self.model)
        dt = time.perf_counter() - t0
        has = st in (cp.OPTIMAL, cp.FEASIBLE)
        values = [solver.value(x) / d for x, d in zip(self.x, self.dens, strict=True)] if has else None
        if st == cp.OPTIMAL:
            return SolveOutcome(SolveStatus.OPTIMAL, values, dt)
        if st == cp.INFEASIBLE:
            return SolveOutcome(SolveStatus.INFEASIBLE, None, dt)
        if st == cp.FEASIBLE:
            return SolveOutcome(SolveStatus.FEASIBLE, values, dt, "time limit")
        if st == cp.UNKNOWN:
            return SolveOutcome(SolveStatus.NO_SOLUTION, None, dt, "time limit")
        return SolveOutcome(SolveStatus.ERROR, None, dt, solver.status_name(st))


def make_backend(name: str, threads: int | None = None, seed: int = 0) -> Backend:
    if name == "highs":
        return HighsBackend(threads or 1, seed)
    if name == "scip":
        return ScipBackend(threads or 1, seed)
    if name == "cpsat":
        return CpSatBackend(threads or 8, seed)
    raise ValueError(f"unknown backend {name!r}")

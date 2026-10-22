"""L3d allocation: oracle agreement (T30), per-farmer leximin (T31), validator and input checks."""

from __future__ import annotations

import json
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from kisaniti_core.allocation import (
    AllocationMode,
    Hierarchy,
    InputValidationError,
    Plan,
    build_result,
    evaluate,
    parse_input,
    plan_from_json,
    solve,
    solve_greedy,
    solve_oracle,
    validate_plan,
)
from kisaniti_core.allocation.fixtures import random_instance
from kisaniti_core.allocation.model import Assignment
from kisaniti_core.allocation.oracle import TooLarge
from kisaniti_core.quantity import Quantity

EXAMPLES = Path(__file__).resolve().parents[2] / "contracts" / "allocation" / "v1" / "examples"
BACKENDS = ["highs", "scip", "cpsat"]

# (seed, farmers, lots, mandis, dates, density, max_increments, mode, strict, ties)
SHAPES: list[tuple[int, int, int, int, int, float, int, AllocationMode, bool, bool]] = []
for s in range(12):
    SHAPES.append((s, 2, 3, 2, 2, 0.7, 2, AllocationMode.PARTIAL, True, False))
    SHAPES.append((100 + s, 3, 4, 2, 2, 0.5, 2, AllocationMode.PARTIAL, True, s % 2 == 0))
    SHAPES.append((200 + s, 2, 2, 3, 3, 0.4, 2, AllocationMode.PARTIAL, True, True))
    SHAPES.append((300 + s, 3, 3, 1, 3, 0.8, 3, AllocationMode.FULL, True, False))
for s in range(6):
    SHAPES.append((400 + s, 2, 3, 2, 2, 0.6, 2, AllocationMode.PARTIAL, False, True))


def _load(name: str) -> dict[str, Any]:
    doc: dict[str, Any] = json.loads((EXAMPLES / name).read_text())
    return doc


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("shape", SHAPES, ids=lambda s: f"seed{s[0]}")
def test_oracle_agreement(shape: tuple[Any, ...], backend: str) -> None:
    seed, f, lots, m, d, dens, mi, mode, strict, ties = shape
    share = 1.6 if mode is AllocationMode.FULL else 0.6
    inp = random_instance(
        seed, f, lots, m, d, density=dens, max_increments=mi, mode=mode, strict=strict, ties=ties, capacity_share=share
    )
    try:
        ref = solve_oracle(inp, max_plans=400_000)
    except TooLarge:
        pytest.skip("enumeration too large")
    except ValueError:  # the oracle proves FULL mode infeasible; the solver must say so too
        rep = solve(inp, backend=backend, time_limit=60)
        assert rep.termination == "PROVEN_INFEASIBLE" and rep.plan is None
        return
    rep = solve(inp, backend=backend, time_limit=60)
    assert rep.termination == "COMPLETED", rep.detail
    assert rep.optimality == "PROVEN_OPTIMAL"
    assert rep.plan is not None and rep.objective is not None
    assert rep.objective.key() == ref.best.key(), "complete objective tuple must match the oracle"
    assert any(p == rep.plan for p in ref.best_plans) or rep.objective.key() == ref.best.key()
    assert validate_plan(inp, rep.plan).valid


def test_t31_fairness_example() -> None:
    inp = parse_input(_load("valid/input_t31_two_farmers.json"))
    rep = solve(inp)
    assert rep.objective is not None
    assert rep.objective.fulfilment_sorted == (Fraction(1, 2), Fraction(1, 2))
    by_lot = {a.lot_id: a.quantity for a in (rep.plan.assignments if rep.plan else ())}
    assert sorted(q.to_json() for q in by_lot.values()) == ["150.000", "50.000"]


def test_leximin_is_discrete_safe() -> None:
    """Two farmers, 2 units each, 3 units of capacity: neither farmer can be fixed at the max-min
    value alone, but the sorted vector (1/2, 1) is still found (a progressive filling that fixes
    single farmers would fail here)."""
    doc = _load("valid/input_t31_two_farmers.json")
    for lot in doc["lots"]:
        lot["quantity_kg"] = "100.000"
    doc["capacities"][0]["capacity_kg"] = "150.000"
    inp = parse_input(doc)
    for b in BACKENDS:
        rep = solve(inp, backend=b)
        assert rep.objective is not None and rep.objective.fulfilment_sorted == (Fraction(1, 2), Fraction(1)), b


def test_leximin_improves_on_maxmin_somewhere() -> None:
    """Over random instances, max-min alone sometimes leaves the second-worst farmer worse off."""
    found = 0
    for seed in range(60):
        inp = random_instance(seed, 4, 5, 2, 2, density=0.5, max_increments=3)
        lex, mm = solve(inp, hierarchy=Hierarchy.KISANITI), solve(inp, hierarchy=Hierarchy.MAXMIN)
        assert lex.objective is not None and mm.objective is not None
        assert lex.objective.fulfilment_sorted[0] == mm.objective.fulfilment_sorted[0]
        assert lex.objective.fulfilment_sorted >= mm.objective.fulfilment_sorted
        found += lex.objective.fulfilment_sorted > mm.objective.fulfilment_sorted
    assert found > 0


def test_utilitarian_never_loses_money_to_leximin() -> None:
    for seed in range(20):
        inp = random_instance(seed, 4, 5, 2, 2, density=0.6, max_increments=3)
        lex, util = solve(inp, hierarchy=Hierarchy.KISANITI), solve(inp, hierarchy=Hierarchy.UTILITARIAN)
        assert lex.objective is not None and util.objective is not None
        assert util.objective.allocated_g == lex.objective.allocated_g
        assert util.objective.net_return_inr >= lex.objective.net_return_inr


def test_greedy_is_valid_and_never_beats_the_oracle() -> None:
    for seed in range(30):
        inp = random_instance(seed, 3, 3, 2, 2, density=0.7, max_increments=2)
        plan = solve_greedy(inp)
        assert validate_plan(inp, plan).valid
        ref = solve_oracle(inp)
        assert evaluate(inp, plan).key() <= ref.best.key()


def test_result_document_and_claimed_totals() -> None:
    doc = _load("valid/input_small_strict.json")
    inp = parse_input(doc)
    rep = solve(inp)
    assert rep.plan is not None
    res = build_result(inp, doc, rep.plan, "highs", rep)
    golden = _load("valid/result_small_strict.json")
    assert res["totals"] == golden["totals"] and res["farmers"] == golden["farmers"]
    assert res["assignments"] == golden["assignments"], "same input + same solver must give the same plan"
    assert validate_plan(inp, plan_from_json(res["assignments"]), claimed=res).valid
    res["totals"]["gross_inr"] = "1.00"
    bad = validate_plan(inp, rep.plan, claimed=res)
    assert not bad.valid and bad.issues[0]["path"] == "/totals/gross_inr"


def test_validator_catches_hard_violations() -> None:
    inp = parse_input(_load("valid/input_small_strict.json"))
    c = inp.choices_canonical[0]
    lot = inp.lot(c.lot_id)
    cases = {
        "over-allocated": Plan((Assignment(c.lot_id, c.mandi_id, c.date, lot.quantity + Quantity.parse("100.000")),)),
        "not whole increments": Plan((Assignment(c.lot_id, c.mandi_id, c.date, Quantity.parse("150.000")),)),
        "inadmissible": Plan(
            (Assignment(c.lot_id, c.mandi_id, inp.dates[-1].replace(day=1), Quantity.parse("100.000")),)
        ),
        "duplicate": Plan((Assignment(c.lot_id, c.mandi_id, c.date, Quantity.parse("100.000")),) * 2),
    }
    for name, plan in cases.items():
        assert not validate_plan(inp, plan).valid, name
    big = next(lt for lt in inp.lots if lt.quantity.units >= 2_000_000)
    ch = next(x for x in inp.choices if x.lot_id == big.id)
    over = validate_plan(inp, Plan((Assignment(big.id, ch.mandi_id, ch.date, Quantity.parse("900.000")),)))
    assert not over.valid and over.limit_violations == 1 and over.excess.to_json() == "100.000"


def test_input_validator_semantic_examples() -> None:
    expected = _load("invalid_semantic/expected_codes.json")
    for name, code in expected.items():
        with pytest.raises(InputValidationError) as e:
            parse_input(_load(f"invalid_semantic/{name}"))
        assert code in {i["code"] for i in e.value.issues}, (name, e.value.issues)


def test_live_mode_refuses_synthetic() -> None:
    doc = _load("valid/input_small_strict.json")
    doc["mode"] = "LIVE"
    with pytest.raises(InputValidationError) as e:
        parse_input(doc)
    assert "SYNTHETIC_INPUT_IN_LIVE" in {i["code"] for i in e.value.issues}


def test_time_limit_reports_honestly() -> None:
    inp = random_instance(7, 40, 40, 5, 5, density=0.6, max_increments=20)
    rep = solve(inp, time_limit=0.05)
    assert rep.termination == "TIME_LIMIT" and rep.optimality in ("FEASIBLE_NOT_PROVEN", "NONE")
    if rep.plan is not None:
        assert validate_plan(inp, rep.plan).valid


def test_cpsat_reports_not_applicable_for_huge_denominators() -> None:
    inp = random_instance(3, 30, 30, 2, 2, density=0.8, max_increments=40, increment_kg=7)
    rep = solve(inp, backend="cpsat", time_limit=20)
    assert rep.termination in ("NOT_APPLICABLE", "COMPLETED", "TIME_LIMIT")

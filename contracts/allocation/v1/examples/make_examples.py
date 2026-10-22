"""Regenerate the allocation/v1 examples from kisaniti_core (run from the repo root):

    PYTHONPATH=core/src core/.venv/bin/python contracts/allocation/v1/examples/make_examples.py

Valid examples are produced by the core so that schema, core and examples cannot drift apart. Invalid
examples are mutations of a valid one, each breaking exactly one rule.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

from kisaniti_core.allocation import build_result, parse_input, solve
from kisaniti_core.ids import fixture_id

HERE = Path(__file__).resolve().parent
NS = "kisaniti-contract-examples"
PROV = {"origin": "SYNTHETIC", "source_kind": "FIXTURE_GENERATOR", "source_ref": "contracts/allocation/v1/examples/make_examples.py",
        "generator": {"script": "make_examples.py", "version": "1", "seed": 0}, "acquired_at": "2026-10-22T06:00:00.000Z"}
POL = {"fairness": "LEXIMIN_FULFILMENT_V1", "storage": "STORAGE_PRIORITY_V1", "tie_break": "TIE_BREAK_V1",
       "economic": "NONE_DEMO_V1"}


def ids(kind: str, key: str) -> str:
    return fixture_id(NS, kind, key)


def t31() -> dict:
    """Layer 1-3 acceptance T31 illustration: A offers 100 kg, B 300 kg, only 200 kg can be sold."""
    a, b, m = ids("mbr", "A"), ids("mbr", "B"), ids("mnd", "M1")
    return {
        "contract": "allocation_input/v1", "fpo_id": ids("fpo", "demo"), "commodity_id": ids("cmd", "paddy"),
        "mode": "DEMO", "provenance": PROV, "allocation_mode": "PARTIAL", "market_load_policy": "STRICT",
        "dates": ["2025-10-20"], "mandis": [{"id": m}], "farmers": [{"id": a}, {"id": b}],
        "lots": [{"id": ids("lot", "A1"), "farmer_id": a, "quantity_kg": "100.000", "increment_kg": "50.000", "storage": "LOW"},
                 {"id": ids("lot", "B1"), "farmer_id": b, "quantity_kg": "300.000", "increment_kg": "50.000", "storage": "LOW"}],
        "choices": [{"lot_id": ids("lot", "A1"), "mandi_id": m, "date": "2025-10-20", "price_inr_per_kg": "23.8900", "cost_inr_per_kg": "0.3000"},
                    {"lot_id": ids("lot", "B1"), "mandi_id": m, "date": "2025-10-20", "price_inr_per_kg": "23.8900", "cost_inr_per_kg": "0.3000"}],
        "capacities": [{"mandi_id": m, "date": "2025-10-20", "capacity_kg": "200.000"}],
        "policy_versions": POL,
    }


def small() -> dict:
    """Three farmers (one with two lots), two mandis, two dates, storage categories, STRICT limits."""
    f = [ids("mbr", f"F{i}") for i in range(3)]
    m = [ids("mnd", "Patiala"), ids("mnd", "Patran")]
    d = ["2025-10-20", "2025-10-21"]
    lots = [("L1", f[0], "1000.000", "HIGH"), ("L2", f[0], "500.000", "LOW"), ("L3", f[1], "2000.000", "MEDIUM"),
            ("L4", f[2], "800.000", "UNKNOWN")]
    cost = {("L1", 0): "0.4000", ("L1", 1): "0.6000", ("L2", 0): "0.4000", ("L2", 1): "0.6000",
            ("L3", 0): "0.7000", ("L3", 1): "0.3000", ("L4", 0): "0.2000", ("L4", 1): "0.9000"}
    choices = []
    for (lid, _, _, _) in lots:
        for mi in range(2):
            for di, day in enumerate(d):
                c = float(cost[(lid, mi)]) + 0.02 * di
                choices.append({"lot_id": ids("lot", lid), "mandi_id": m[mi], "date": day,
                                "price_inr_per_kg": "23.8900", "cost_inr_per_kg": f"{c:.4f}"})
    return {
        "contract": "allocation_input/v1", "fpo_id": ids("fpo", "demo"), "commodity_id": ids("cmd", "paddy"),
        "mode": "DEMO", "provenance": PROV, "allocation_mode": "PARTIAL", "market_load_policy": "STRICT",
        "dates": d, "mandis": [{"id": x} for x in m], "farmers": [{"id": x} for x in f],
        "lots": [{"id": ids("lot", lid), "farmer_id": fid, "quantity_kg": q, "increment_kg": "100.000", "storage": s}
                 for lid, fid, q, s in lots],
        "choices": choices,
        "capacities": [{"mandi_id": mm, "date": dd, "capacity_kg": "800.000"} for mm in m for dd in d],
        "policy_versions": POL,
    }


def dump(path: Path, doc: object) -> None:
    path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    for name, doc in (("t31_two_farmers", t31()), ("small_strict", small())):
        dump(HERE / "valid" / f"input_{name}.json", doc)
        inp = parse_input(doc)
        rep = solve(inp, backend="highs", time_limit=60)
        assert rep.plan is not None
        res = build_result(inp, doc, rep.plan, "highs", rep)
        res["solver"]["name"] = "highs"  # keep examples independent of the solver version string
        dump(HERE / "valid" / f"result_{name}.json", res)
        dump(HERE / "valid" / f"validation_{name}.json", res["validation"])
    base = small()
    schema_bad = {
        "input_float_quantity": lambda d: d["lots"][0].__setitem__("quantity_kg", 1000.0),
        "input_unknown_storage": lambda d: d["lots"][0].__setitem__("storage", "SAFE"),
        "input_weighted_fairness_policy": lambda d: d["policy_versions"].__setitem__("fairness", "WEIGHTED_SUM"),
        "input_live_with_synthetic": lambda d: d.__setitem__("mode", "LIVE"),
        "input_rate_scale_2": lambda d: d["choices"][0].__setitem__("price_inr_per_kg", "23.89"),
    }
    for name, fn in schema_bad.items():
        d = copy.deepcopy(base)
        fn(d)
        dump(HERE / "invalid" / f"{name}.json", d)
    res = json.loads((HERE / "valid" / "result_small_strict.json").read_text())
    r1 = copy.deepcopy(res)
    r1["assignments"][0]["quantity_kg"] = "0.000"
    dump(HERE / "invalid" / "result_zero_assignment.json", r1)
    v1 = copy.deepcopy(res["validation"])
    v1["valid"] = False
    dump(HERE / "invalid" / "validation_invalid_without_issue.json", v1)
    semantic = {
        "missing_capacity_strict": (lambda d: d["capacities"].pop(), "VALIDATION_FAILED"),
        "lot_owner_not_in_input": (lambda d: d["lots"][0].__setitem__("farmer_id", ids("mbr", "stranger")), "TENANT_SCOPE_VIOLATION"),
        "choice_for_unknown_lot": (lambda d: d["choices"][0].__setitem__("lot_id", ids("lot", "ghost")), "TENANT_SCOPE_VIOLATION"),
        "choice_date_outside_horizon": (lambda d: d["choices"][0].__setitem__("date", "2025-10-25"), "INVALID_DATE"),
        "duplicate_choice": (lambda d: d["choices"].append(copy.deepcopy(d["choices"][0])), "VALIDATION_FAILED"),
        "full_mode_partial_increment": (lambda d: (d.__setitem__("allocation_mode", "FULL"),
                                                   d["lots"][0].__setitem__("quantity_kg", "1050.000")), "VALIDATION_FAILED"),
        "impossible_date": (lambda d: d["dates"].__setitem__(1, "2025-02-30"), "INVALID_DATE"),
    }
    expect = {}
    for name, (fn, code) in semantic.items():
        d = copy.deepcopy(base)
        fn(d)
        dump(HERE / "invalid_semantic" / f"input_{name}.json", d)
        expect[f"input_{name}.json"] = code
    dump(HERE / "invalid_semantic" / "expected_codes.json", expect)


if __name__ == "__main__":
    main()

"""L3d allocation: exact lexicographic optimizer, brute-force oracle, greedy heuristic, PlanValidator."""

from .greedy import solve_greedy
from .milp import Hierarchy, SolveReport, Weights, solve
from .model import (
    AllocationInput,
    AllocationMode,
    Assignment,
    Choice,
    InputValidationError,
    Lot,
    MarketLoad,
    Plan,
    StorageCategory,
    input_to_json,
    parse_input,
    plan_from_json,
    plan_to_json,
)
from .objective import ObjectiveTuple, evaluate, gini
from .oracle import TooLarge, solve_oracle
from .result import build_result
from .validator import ValidationReport, validate_plan

__all__ = [
    "AllocationInput",
    "AllocationMode",
    "Assignment",
    "Choice",
    "Hierarchy",
    "InputValidationError",
    "Lot",
    "MarketLoad",
    "ObjectiveTuple",
    "Plan",
    "SolveReport",
    "StorageCategory",
    "TooLarge",
    "ValidationReport",
    "Weights",
    "build_result",
    "evaluate",
    "gini",
    "input_to_json",
    "parse_input",
    "plan_from_json",
    "plan_to_json",
    "solve",
    "solve_greedy",
    "solve_oracle",
    "validate_plan",
]

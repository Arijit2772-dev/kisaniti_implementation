"""Allocation input and plan types, and the input validator (contract allocation_input/v1).

Meaning: kisaniti/docs/contracts/CONTRACT_SPEC_V1_L3d_allocation.md. Every quantity is exact: grams
(int) for kilograms, 1/10,000 rupee (int) for rates per kg.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from math import gcd
from typing import Any

from ..errors import ContractError, ErrorCode
from ..ids import validate_id
from ..provenance import Mode, Origin
from ..quantity import Quantity, RatePerKg
from ..timeutil import parse_business_date

Day = date  # alias: Choice/Assignment have a field named "date"

CONTRACT_INPUT = "allocation_input/v1"
POLICY_VERSIONS = {
    "fairness": "LEXIMIN_FULFILMENT_V1",
    "storage": "STORAGE_PRIORITY_V1",
    "tie_break": "TIE_BREAK_V1",
    "economic": "NONE_DEMO_V1",
}


class AllocationMode(StrEnum):
    PARTIAL = "PARTIAL"
    FULL = "FULL"


class MarketLoad(StrEnum):
    DISABLED = "DISABLED"
    STRICT = "STRICT"


class StorageCategory(StrEnum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class Lot:
    id: str
    farmer_id: str
    quantity: Quantity
    increment: Quantity
    storage: StorageCategory

    @property
    def max_increments(self) -> int:
        return self.quantity.units // self.increment.units


@dataclass(frozen=True, slots=True)
class Choice:
    lot_id: str
    mandi_id: str
    date: Day
    price: RatePerKg
    cost: RatePerKg

    @property
    def key(self) -> tuple[str, str, Day]:
        return (self.lot_id, self.mandi_id, self.date)

    @property
    def net_units(self) -> int:
        """Expected net selling return per kg, in 1/10,000 rupee."""
        return self.price.units - self.cost.units


@dataclass(frozen=True, slots=True)
class Assignment:
    lot_id: str
    mandi_id: str
    date: Day
    quantity: Quantity

    @property
    def key(self) -> tuple[str, str, Day]:
        return (self.lot_id, self.mandi_id, self.date)


@dataclass(frozen=True, slots=True)
class Plan:
    assignments: tuple[Assignment, ...]

    @staticmethod
    def from_increments(inp: AllocationInput, n: Mapping[tuple[str, str, date], int]) -> Plan:
        """Plan from a count of increments per choice key (zeros dropped), in canonical order."""
        out = []
        for c in inp.choices_canonical:
            k = n.get(c.key, 0)
            if k:
                out.append(Assignment(c.lot_id, c.mandi_id, c.date, inp.lot(c.lot_id).increment * k))
        return Plan(tuple(out))


@dataclass(frozen=True)
class AllocationInput:
    fpo_id: str
    commodity_id: str
    mode: Mode
    origin: Origin
    allocation_mode: AllocationMode
    market_load: MarketLoad
    dates: tuple[date, ...]
    mandis: tuple[str, ...]
    farmers: tuple[str, ...]
    lots: tuple[Lot, ...]
    choices: tuple[Choice, ...]
    capacities: Mapping[tuple[str, date], Quantity]
    policy_versions: Mapping[str, str] = field(default_factory=lambda: dict(POLICY_VERSIONS))
    _lots: dict[str, Lot] = field(init=False, repr=False, compare=False)
    choices_canonical: tuple[Choice, ...] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_lots", {lot.id: lot for lot in self.lots})
        # Canonical order (tie-break coordinate order): lot ID, then date, then mandi ID.
        canon = tuple(sorted(self.choices, key=lambda c: (c.lot_id, c.date, c.mandi_id)))
        object.__setattr__(self, "choices_canonical", canon)

    def lot(self, lot_id: str) -> Lot:
        return self._lots[lot_id]

    def participating_g(self, farmer_id: str) -> int:
        return sum(lot.quantity.units for lot in self.lots if lot.farmer_id == farmer_id)

    @property
    def fairness_farmers(self) -> tuple[str, ...]:
        """Participating farmers (P_f > 0), in ID order."""
        return tuple(sorted(f for f in self.farmers if self.participating_g(f) > 0))

    @property
    def quantum_g(self) -> int:
        """Greatest common divisor of all increments (grams)."""
        g = 0
        for lot in self.lots:
            g = gcd(g, lot.increment.units)
        return g


class InputValidationError(ContractError):
    """The OptimizationInputValidator found problems; ``issues`` lists every one of them."""

    def __init__(self, issues: list[dict[str, str]]) -> None:
        super().__init__(ErrorCode.VALIDATION_FAILED, f"{len(issues)} input issue(s): {issues[0]['message']}")
        self.issues = issues


def parse_input(doc: Mapping[str, Any]) -> AllocationInput:
    """Parse and validate an allocation_input/v1 document. Collects all issues before failing."""
    issues: list[dict[str, str]] = []

    def bad(code: ErrorCode, path: str, msg: str) -> None:
        issues.append({"code": code.value, "path": path, "message": msg})

    def get(obj: Mapping[str, Any], key: str, path: str) -> Any:
        if key not in obj:
            bad(ErrorCode.VALIDATION_FAILED, f"{path}/{key}", f"missing field {key!r}")
            return None
        return obj[key]

    def safe(fn: Any, *args: Any) -> Any:
        try:
            return fn(*args)
        except ContractError as e:
            issues.append({"code": e.code.value, "path": e.path or "", "message": e.message})
            return None

    if doc.get("contract") != CONTRACT_INPUT:
        bad(ErrorCode.VALIDATION_FAILED, "/contract", f"expected {CONTRACT_INPUT!r}")
    fpo_id = safe(validate_id, get(doc, "fpo_id", ""), "fpo", "/fpo_id")
    commodity_id = safe(validate_id, get(doc, "commodity_id", ""), "cmd", "/commodity_id")
    try:
        mode = Mode(str(doc.get("mode")))
        origin = Origin(str(doc.get("provenance", {}).get("origin")))
        amode = AllocationMode(str(doc.get("allocation_mode")))
        mload = MarketLoad(str(doc.get("market_load_policy")))
    except ValueError as e:
        raise InputValidationError([*issues, {"code": "VALIDATION_FAILED", "path": "", "message": str(e)}]) from None
    if mode is Mode.LIVE and origin is Origin.SYNTHETIC:
        bad(ErrorCode.SYNTHETIC_INPUT_IN_LIVE, "/mode", "LIVE run with SYNTHETIC input")

    dates = [safe(parse_business_date, d, f"/dates/{i}") for i, d in enumerate(get(doc, "dates", "") or [])]
    if len(set(dates)) != len(dates) or dates != sorted(d for d in dates if d is not None):
        bad(ErrorCode.INVALID_DATE, "/dates", "dates must be unique and in increasing order")
    if not dates:
        bad(ErrorCode.VALIDATION_FAILED, "/dates", "at least one planning date is needed")
    mandis = [safe(validate_id, m.get("id"), "mnd", f"/mandis/{i}/id") for i, m in enumerate(doc.get("mandis", []))]
    farmers = [safe(validate_id, f.get("id"), "mbr", f"/farmers/{i}/id") for i, f in enumerate(doc.get("farmers", []))]
    for name, seq in (("mandis", mandis), ("farmers", farmers)):
        if len(set(seq)) != len(seq):
            bad(ErrorCode.VALIDATION_FAILED, f"/{name}", f"duplicate {name} IDs")

    lots: list[Lot] = []
    for i, raw in enumerate(doc.get("lots", [])):
        p = f"/lots/{i}"
        lid = safe(validate_id, raw.get("id"), "lot", f"{p}/id")
        fid = safe(validate_id, raw.get("farmer_id"), "mbr", f"{p}/farmer_id")
        q = safe(Quantity.parse, raw.get("quantity_kg"), f"{p}/quantity_kg")
        inc = safe(Quantity.parse, raw.get("increment_kg"), f"{p}/increment_kg")
        try:
            cat = StorageCategory(raw.get("storage"))
        except ValueError:
            bad(ErrorCode.VALIDATION_FAILED, f"{p}/storage", "storage must be HIGH, MEDIUM, LOW or UNKNOWN")
            cat = StorageCategory.UNKNOWN
        if fid is not None and fid not in farmers:
            bad(ErrorCode.TENANT_SCOPE_VIOLATION, f"{p}/farmer_id", "lot owner is not a farmer of this input/FPO")
        if q is not None and q.is_zero():
            bad(ErrorCode.OUT_OF_RANGE, f"{p}/quantity_kg", "lot quantity must be positive")
        if inc is not None and inc.is_zero():
            bad(ErrorCode.OUT_OF_RANGE, f"{p}/increment_kg", "increment must be positive")
        if None not in (lid, fid, q, inc):
            assert q is not None and inc is not None and lid is not None and fid is not None
            if amode is AllocationMode.FULL and not inc.is_zero() and q.units % inc.units:
                bad(ErrorCode.VALIDATION_FAILED, f"{p}/quantity_kg", "FULL mode needs quantity = whole increments")
            lots.append(Lot(lid, fid, q, inc, cat))
    if len({lot.id for lot in lots}) != len(lots):
        bad(ErrorCode.VALIDATION_FAILED, "/lots", "duplicate lot IDs")
    lot_ids = {lot.id for lot in lots}

    choices: list[Choice] = []
    seen: set[tuple[str, str, date]] = set()
    for i, raw in enumerate(doc.get("choices", [])):
        p = f"/choices/{i}"
        lid, mid = raw.get("lot_id"), raw.get("mandi_id")
        d = safe(parse_business_date, raw.get("date"), f"{p}/date")
        price = safe(RatePerKg.parse, raw.get("price_inr_per_kg"), f"{p}/price_inr_per_kg")
        cost = safe(RatePerKg.parse, raw.get("cost_inr_per_kg"), f"{p}/cost_inr_per_kg")
        if lid not in lot_ids:
            bad(ErrorCode.TENANT_SCOPE_VIOLATION, f"{p}/lot_id", "choice refers to a lot not in this input")
            continue
        if mid not in mandis:
            bad(ErrorCode.VALIDATION_FAILED, f"{p}/mandi_id", "choice refers to a mandi not in this input")
            continue
        if d is not None and d not in dates:
            bad(ErrorCode.INVALID_DATE, f"{p}/date", "choice date is not a planning date")
            continue
        if None in (d, price, cost):
            continue
        assert d is not None and price is not None and cost is not None
        c = Choice(lid, mid, d, price, cost)
        if c.key in seen:
            bad(ErrorCode.VALIDATION_FAILED, p, "duplicate (lot, mandi, date) choice")
            continue
        seen.add(c.key)
        choices.append(c)

    capacities: dict[tuple[str, date], Quantity] = {}
    for i, raw in enumerate(doc.get("capacities", [])):
        p = f"/capacities/{i}"
        d = safe(parse_business_date, raw.get("date"), f"{p}/date")
        cap = safe(Quantity.parse, raw.get("capacity_kg"), f"{p}/capacity_kg")
        if raw.get("mandi_id") not in mandis or d is None or cap is None:
            bad(ErrorCode.VALIDATION_FAILED, p, "capacity needs a known mandi, a valid date and capacity_kg")
            continue
        if (raw["mandi_id"], d) in capacities:
            bad(ErrorCode.VALIDATION_FAILED, p, "duplicate capacity for this mandi-date")
        capacities[(raw["mandi_id"], d)] = cap
    if mload is MarketLoad.STRICT:
        for c in choices:
            if (c.mandi_id, c.date) not in capacities:
                bad(
                    ErrorCode.VALIDATION_FAILED,
                    "/capacities",
                    f"STRICT market load needs a capacity for {c.mandi_id} on {c.date} (missing is not unlimited)",
                )
                break
    pv = doc.get("policy_versions", {})
    for k, v in POLICY_VERSIONS.items():
        if pv.get(k) != v:
            bad(ErrorCode.VALIDATION_FAILED, f"/policy_versions/{k}", f"core implements {v!r}")

    if issues:
        raise InputValidationError(issues)
    assert fpo_id is not None and commodity_id is not None
    return AllocationInput(
        fpo_id,
        commodity_id,
        mode,
        origin,
        amode,
        mload,
        tuple(d for d in dates if d is not None),
        tuple(m for m in mandis if m),
        tuple(f for f in farmers if f),
        tuple(lots),
        tuple(choices),
        capacities,
        dict(pv),
    )


def input_to_json(inp: AllocationInput, provenance: Mapping[str, Any], run_id: str | None = None) -> dict[str, Any]:
    """Serialise an AllocationInput back to an allocation_input/v1 document."""
    doc: dict[str, Any] = {
        "contract": CONTRACT_INPUT,
        "fpo_id": inp.fpo_id,
        "commodity_id": inp.commodity_id,
        "mode": inp.mode.value,
        "provenance": dict(provenance),
        "allocation_mode": inp.allocation_mode.value,
        "market_load_policy": inp.market_load.value,
        "dates": [d.isoformat() for d in inp.dates],
        "mandis": [{"id": m} for m in inp.mandis],
        "farmers": [{"id": f} for f in inp.farmers],
        "lots": [
            {
                "id": lot.id,
                "farmer_id": lot.farmer_id,
                "quantity_kg": lot.quantity.to_json(),
                "increment_kg": lot.increment.to_json(),
                "storage": lot.storage.value,
            }
            for lot in inp.lots
        ],
        "choices": [
            {
                "lot_id": c.lot_id,
                "mandi_id": c.mandi_id,
                "date": c.date.isoformat(),
                "price_inr_per_kg": c.price.to_json(),
                "cost_inr_per_kg": c.cost.to_json(),
            }
            for c in inp.choices
        ],
        "capacities": [
            {"mandi_id": m, "date": d.isoformat(), "capacity_kg": q.to_json()}
            for (m, d), q in sorted(inp.capacities.items())
        ],
        "policy_versions": dict(inp.policy_versions),
    }
    if run_id is not None:
        doc["run_id"] = run_id
    return doc


def plan_to_json(plan: Plan) -> list[dict[str, str]]:
    return [
        {"lot_id": a.lot_id, "mandi_id": a.mandi_id, "date": a.date.isoformat(), "quantity_kg": a.quantity.to_json()}
        for a in plan.assignments
    ]


def plan_from_json(items: Sequence[Mapping[str, Any]]) -> Plan:
    return Plan(
        tuple(
            Assignment(
                str(a["lot_id"]), str(a["mandi_id"]), parse_business_date(a["date"]), Quantity.parse(a["quantity_kg"])
            )
            for a in items
        )
    )

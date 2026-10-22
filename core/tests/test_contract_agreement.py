"""The core parsers and the JSON Schemas must agree on the shared primitive examples."""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from kisaniti_core import ContractError, Money, Quantity, QuantityDelta, RatePerKg, RatePerQuintal
from kisaniti_core.ids import validate_id
from kisaniti_core.quantity import Distance
from kisaniti_core.timeutil import parse_business_date, parse_instant

PRIMITIVES = Path(__file__).resolve().parents[2] / "contracts" / "shared" / "v1" / "examples" / "primitives.json"
PARSERS: dict[str, Callable[[Any], object]] = {
    "quantity_kg": Quantity.parse,
    "quantity_delta_kg": QuantityDelta.parse,
    "money_inr": Money.parse,
    "rate_inr_per_kg": RatePerKg.parse,
    "rate_inr_per_quintal": RatePerQuintal.parse,
    "distance_km": Distance.parse,
    "instant_utc": parse_instant,
    "business_date": parse_business_date,
    "id_any": validate_id,
}
CASES = [
    (name, value, kind)
    for name, groups in json.loads(PRIMITIVES.read_text()).items()
    if not name.startswith("_")
    for kind in ("valid", "invalid", "invalid_semantic")
    for value in groups[kind]
]


def test_every_primitive_has_a_parser() -> None:
    assert {c[0] for c in CASES} == set(PARSERS)


@pytest.mark.parametrize(("name", "value", "kind"), CASES)
def test_core_agrees(name: str, value: Any, kind: str) -> None:
    if kind == "valid":
        PARSERS[name](value)
    else:
        with pytest.raises(ContractError):
            PARSERS[name](value)

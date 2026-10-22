from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from kisaniti_core import (
    ContractError,
    ErrorCode,
    Money,
    Quantity,
    QuantityDelta,
    RatePerKg,
    RatePerQuintal,
    line_amount,
)
from kisaniti_core.quantity import Distance, ratio_display


def test_parse_and_format_roundtrip() -> None:
    q = Quantity.parse("1250.000")
    assert q.units == 1_250_000 and q.to_json() == "1250.000"
    assert Money.parse("-15.00").to_json() == "-15.00"
    assert RatePerKg.parse("23.8900").units == 238_900


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("1250.0005", ErrorCode.PRECISION_EXCEEDED),
        ("1250.0", ErrorCode.INVALID_DECIMAL),
        ("1250", ErrorCode.INVALID_DECIMAL),
        ("01.000", ErrorCode.INVALID_DECIMAL),
        ("1e3", ErrorCode.INVALID_DECIMAL),
        ("-5.000", ErrorCode.OUT_OF_RANGE),
        ("1000000000.001", ErrorCode.OUT_OF_RANGE),
    ],
)
def test_quantity_rejects(text: str, code: ErrorCode) -> None:
    with pytest.raises(ContractError) as e:
        Quantity.parse(text, path="/q")
    assert e.value.code == code and e.value.path == "/q"


def test_negative_zero_rejected() -> None:
    with pytest.raises(ContractError) as e:
        QuantityDelta.parse("-0.000")
    assert e.value.code == ErrorCode.INVALID_DECIMAL


def test_float_input_rejected() -> None:
    with pytest.raises(ContractError):
        Quantity.parse(1250.0)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        Quantity(1.5)  # type: ignore[arg-type]


def test_units_cannot_mix() -> None:
    with pytest.raises(ContractError) as e:
        _ = Quantity.parse("1.000") + QuantityDelta.parse("1.000")
    assert e.value.code == ErrorCode.UNIT_MISMATCH
    with pytest.raises(ContractError):
        _ = Money.parse("1.00") < RatePerKg.parse("1.0000")


def test_quantity_never_negative() -> None:
    with pytest.raises(ContractError) as e:
        _ = Quantity.parse("1.000") - Quantity.parse("2.000")
    assert e.value.code == ErrorCode.OUT_OF_RANGE


def test_quintal_conversion_exact() -> None:
    assert RatePerQuintal.parse("2389.00").per_kg() == RatePerKg.parse("23.8900")
    assert Quantity.from_quintals("12.345").to_json() == "1234.500"
    assert Quantity.parse("1234.500").to_quintals() == Decimal("12.345")


def test_from_decimal_never_rounds() -> None:
    assert Quantity.from_decimal(Decimal("1.5")).to_json() == "1.500"
    with pytest.raises(ContractError) as e:
        Quantity.from_decimal(Decimal("0.0005"))
    assert e.value.code == ErrorCode.PRECISION_EXCEEDED
    with pytest.raises(ContractError):
        Quantity.from_decimal(Decimal("NaN"))


def test_line_amount_half_even() -> None:
    # 0.005 rupee ties round to the even paisa
    assert line_amount(Quantity.parse("1.000"), RatePerKg.parse("0.0050")).to_json() == "0.00"
    assert line_amount(Quantity.parse("1.000"), RatePerKg.parse("0.0150")).to_json() == "0.02"
    assert line_amount(Quantity.parse("1250.000"), RatePerKg.parse("23.8900")).to_json() == "29862.50"
    assert line_amount(Quantity.parse("0.001"), RatePerKg.parse("-23.8900")).to_json() == "-0.02"


def test_ratio_display() -> None:
    assert ratio_display(Quantity.parse("1.000"), Quantity.parse("3.000")) == "0.333333"
    with pytest.raises(ContractError):
        ratio_display(Quantity.parse("1.000"), Quantity.zero())


def test_distance() -> None:
    assert Distance.parse("15.165").units == 15165


@given(st.integers(min_value=0, max_value=Quantity.MAX_UNITS))
def test_quantity_roundtrip_property(units: int) -> None:
    q = Quantity(units)
    assert Quantity.parse(q.to_json()) == q
    assert Quantity.from_decimal(q.to_decimal()) == q


@given(st.integers(min_value=Money.MIN_UNITS, max_value=Money.MAX_UNITS))
def test_money_roundtrip_property(units: int) -> None:
    m = Money(units)
    assert Money.parse(m.to_json()) == m


@given(st.integers(min_value=0, max_value=10**9), st.integers(min_value=-(10**8), max_value=10**8))
def test_line_amount_within_half_paisa(q_units: int, r_units: int) -> None:
    exact = Decimal(q_units) * Decimal(r_units) / Decimal(10**7)
    got = line_amount(Quantity(q_units), RatePerKg(r_units)).to_decimal()
    assert abs(got - exact) <= Decimal("0.005")

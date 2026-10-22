"""Exact business numbers (CONTRACT_SPEC_V1_shared.md §3).

Every type stores an exact ``int`` count of its smallest unit (grams, paise, 1/10,000 rupee, metres), so
arithmetic never touches binary floats. Serialised form is a decimal string with exactly ``SCALE``
fractional digits. Parsing never rounds: extra digits raise ``PRECISION_EXCEEDED``.
"""

from __future__ import annotations

import re
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from fractions import Fraction
from typing import ClassVar, Self

from .errors import ContractError, ErrorCode

_DECIMAL_RE = re.compile(r"^(-?)(0|[1-9][0-9]*)(?:\.([0-9]+))?$")


class _Fixed:
    """Fixed-scale exact value stored as an integer number of minor units."""

    SCALE: ClassVar[int]
    UNIT: ClassVar[str]
    MIN_UNITS: ClassVar[int]
    MAX_UNITS: ClassVar[int]

    __slots__ = ("units",)
    units: int

    def __init__(self, units: int) -> None:
        if isinstance(units, bool) or not isinstance(units, int):
            raise TypeError(f"{type(self).__name__} needs an int count of minor units, got {type(units).__name__}")
        if not self.MIN_UNITS <= units <= self.MAX_UNITS:
            raise ContractError(ErrorCode.OUT_OF_RANGE, f"{self._fmt(units)} {self.UNIT} is outside the allowed range")
        object.__setattr__(self, "units", units)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError(f"{type(self).__name__} is immutable")

    # ---- construction -------------------------------------------------------------------------------
    @classmethod
    def parse(cls, text: str, path: str | None = None) -> Self:
        """Parse the canonical wire form: exactly SCALE fractional digits, no exponent, no negative zero."""
        if not isinstance(text, str):
            raise ContractError(
                ErrorCode.INVALID_DECIMAL, f"expected a decimal string, got {type(text).__name__}", path
            )
        m = _DECIMAL_RE.match(text)
        if not m:
            raise ContractError(ErrorCode.INVALID_DECIMAL, f"{text!r} is not a canonical decimal string", path)
        frac = m.group(3) or ""
        if len(frac) > cls.SCALE:
            raise ContractError(
                ErrorCode.PRECISION_EXCEEDED, f"{text!r} has {len(frac)} fractional digits; at most {cls.SCALE}", path
            )
        if len(frac) != cls.SCALE:
            raise ContractError(
                ErrorCode.INVALID_DECIMAL, f"{text!r} must have exactly {cls.SCALE} fractional digits", path
            )
        units = int(m.group(2)) * 10**cls.SCALE + int(frac or "0")
        if m.group(1):
            if units == 0:
                raise ContractError(ErrorCode.INVALID_DECIMAL, "negative zero is not allowed", path)
            units = -units
        try:
            return cls(units)
        except ContractError as e:
            raise ContractError(e.code, e.message, path) from None

    @classmethod
    def from_decimal(cls, value: Decimal | int | str) -> Self:
        """Exact conversion from a Decimal/int/str of any form; raises if it needs rounding."""
        d = Decimal(value)
        if not d.is_finite():
            raise ContractError(ErrorCode.INVALID_DECIMAL, f"{value!r} is not finite")
        scaled = d.scaleb(cls.SCALE)
        if scaled != scaled.to_integral_value():
            raise ContractError(
                ErrorCode.PRECISION_EXCEEDED, f"{value!r} needs more than {cls.SCALE} fractional digits"
            )
        return cls(int(scaled))

    @classmethod
    def zero(cls) -> Self:
        return cls(0)

    # ---- output -----------------------------------------------------------------------------------
    @classmethod
    def _fmt(cls, units: int) -> str:
        sign = "-" if units < 0 else ""
        whole, frac = divmod(abs(units), 10**cls.SCALE)
        return f"{sign}{whole}.{frac:0{cls.SCALE}d}"

    def to_json(self) -> str:
        return self._fmt(self.units)

    def to_decimal(self) -> Decimal:
        return Decimal(self.units).scaleb(-self.SCALE)

    def to_fraction(self) -> Fraction:
        return Fraction(self.units, 10**self.SCALE)

    def __str__(self) -> str:
        return f"{self.to_json()} {self.UNIT}"

    def __repr__(self) -> str:
        return f"{type(self).__name__}('{self.to_json()}')"

    # ---- comparison and arithmetic (same type only) -----------------------------------------------------
    def _same(self, other: object) -> _Fixed:
        if type(other) is not type(self):
            raise ContractError(
                ErrorCode.UNIT_MISMATCH, f"cannot combine {type(self).__name__} with {type(other).__name__}"
            )
        assert isinstance(other, _Fixed)
        return other

    def __eq__(self, other: object) -> bool:
        return type(other) is type(self) and self.units == other.units  # type: ignore[attr-defined]

    def __hash__(self) -> int:
        return hash((type(self).__name__, self.units))

    def __lt__(self, other: object) -> bool:
        return self.units < self._same(other).units

    def __le__(self, other: object) -> bool:
        return self.units <= self._same(other).units

    def __gt__(self, other: object) -> bool:
        return self.units > self._same(other).units

    def __ge__(self, other: object) -> bool:
        return self.units >= self._same(other).units

    def __add__(self, other: object) -> Self:
        return type(self)(self.units + self._same(other).units)

    def __sub__(self, other: object) -> Self:
        return type(self)(self.units - self._same(other).units)

    def __mul__(self, k: int) -> Self:
        if isinstance(k, bool) or not isinstance(k, int):
            raise ContractError(ErrorCode.UNIT_MISMATCH, f"{type(self).__name__} can only be multiplied by an int")
        return type(self)(self.units * k)

    __rmul__ = __mul__

    def is_zero(self) -> bool:
        return self.units == 0


class Quantity(_Fixed):
    """Stock quantity in kilograms; scale 3 (grams); never negative; at most 1e9 kg."""

    SCALE = 3
    UNIT = "kg"
    MIN_UNITS = 0
    MAX_UNITS = 1_000_000_000 * 10**3

    @classmethod
    def from_quintals(cls, q: Decimal | int | str) -> Quantity:
        return cls.from_decimal(Decimal(q) * 100)

    def to_quintals(self) -> Decimal:
        return self.to_decimal() / 100


class QuantityDelta(_Fixed):
    """Signed quantity change in kilograms (adjustments, corrections)."""

    SCALE = 3
    UNIT = "kg"
    MIN_UNITS = -1_000_000_000 * 10**3
    MAX_UNITS = 1_000_000_000 * 10**3


class Money(_Fixed):
    """Indian rupees; scale 2 (paise)."""

    SCALE = 2
    UNIT = "INR"
    MIN_UNITS = -(10**12) * 10**2
    MAX_UNITS = 10**12 * 10**2


class RatePerKg(_Fixed):
    """Rupees per kilogram; scale 4."""

    SCALE = 4
    UNIT = "INR/kg"
    MIN_UNITS = -(10**7) * 10**4
    MAX_UNITS = 10**7 * 10**4


class RatePerQuintal(_Fixed):
    """Rupees per quintal (AGMARKNET and MSP native unit); scale 2."""

    SCALE = 2
    UNIT = "INR/quintal"
    MIN_UNITS = -(10**9) * 10**2
    MAX_UNITS = 10**9 * 10**2

    def per_kg(self) -> RatePerKg:
        """Exact: 1 quintal = 100 kg, and scale 2 -> scale 4 needs no rounding."""
        return RatePerKg(self.units)


class Distance(_Fixed):
    """Kilometres; scale 3 (metres)."""

    SCALE = 3
    UNIT = "km"
    MIN_UNITS = 0
    MAX_UNITS = 100_000 * 10**3


def line_amount(quantity: Quantity, rate: RatePerKg) -> Money:
    """quantity x rate rounded once to paise, round half to even (the frozen money rule)."""
    exact = Fraction(quantity.units * rate.units, 10 ** (Quantity.SCALE + RatePerKg.SCALE))
    with localcontext() as ctx:
        ctx.prec = 60
        d = Decimal(exact.numerator) / Decimal(exact.denominator)
        paise = d.scaleb(Money.SCALE).quantize(Decimal(1), rounding=ROUND_HALF_EVEN)
    return Money(int(paise))


def ratio_display(numerator: Quantity, denominator: Quantity) -> str:
    """Display-only 6-decimal string of an exact ratio (round half to even). Never compare these strings."""
    if denominator.is_zero():
        raise ContractError(ErrorCode.OUT_OF_RANGE, "ratio denominator is zero")
    with localcontext() as ctx:
        ctx.prec = 60
        d = Decimal(numerator.units) / Decimal(denominator.units)
        return str(d.quantize(Decimal("0.000001"), rounding=ROUND_HALF_EVEN))

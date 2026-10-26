"""Baseline price forecast with empirical ranges (contract plan/v1, policies in L3_PLAN_CONTRACT_v1.md §1).

Pure and exact: prices are integers of 1/1000 rupee per quintal ("milli"); medians, means and errors are exact
fractions; rounding happens only where the contract says so (point forecasts and range bounds, half-even to the paisa).
Nothing here reads a database or a clock. These are the BASELINES of the draft pre-registered protocol; no claim of
accuracy or calibration follows from this module.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from fractions import Fraction

from .quantity import RatePerQuintal

VARIETIES = ("1121", "Basmati 1509")
HORIZONS = (1, 7, 14)
MIN_MANDIS = 3
MIN_SEASON_DAYS = 10  # an origin is eligible after 10 index days of its season (protocol §3)
MIN_PAIRS = 40  # no range from fewer pairs (protocol §6)
COVERAGES = {"80": Fraction(4, 5), "95": Fraction(19, 20)}
BASIS_WINDOW_DAYS = 30
BASIS_MIN_DAYS = 5
MA_DAYS = 5


@dataclass(frozen=True, slots=True)
class Obs:
    """One price row as the index needs it. ``modal_milli`` is None when the source had no modal price."""

    mandi_id: str
    observed_on: date
    modal_milli: int | None
    date_inferred: bool


@dataclass(frozen=True, slots=True)
class IndexPoint:
    day: date
    milli: int  # exact: the median of 2-decimal prices has at most 3 decimals
    n_mandis: int

    @property
    def season(self) -> str:
        return season_of(self.day)


def milli_from_decimal(value: Decimal | str) -> int:
    """A 2-decimal rupee price as integer milli-rupees; more precision is refused."""
    d = Decimal(value)
    scaled = d * 1000
    if scaled != scaled.to_integral_value() or (d * 100) != (d * 100).to_integral_value():
        raise ValueError(f"price {value} has more than two decimals")
    return int(scaled)


def milli_to_index_json(milli: int) -> str:
    """The wire form of an index value: three decimals, exact."""
    return f"{milli // 1000}.{milli % 1000:03d}"


def season_of(day: date) -> str:
    start = day.year if day.month >= 9 else day.year - 1
    return f"{start}-{str(start + 1)[2:]}"


def _median_milli(values: Sequence[int]) -> Fraction:
    v = sorted(values)
    n = len(v)
    return Fraction(v[n // 2]) if n % 2 else Fraction(v[n // 2 - 1] + v[n // 2], 2)


def _group_one_row_per_mandi(rows: Iterable[Obs]) -> dict[date, dict[str, int]]:
    """Dated rows with a modal price, one per (day, mandi); a mandi-day with two or more rows is dropped."""
    seen: dict[tuple[date, str], list[int]] = defaultdict(list)
    for r in rows:
        if r.modal_milli is None or r.date_inferred:
            continue
        seen[(r.observed_on, r.mandi_id)].append(r.modal_milli)
    out: dict[date, dict[str, int]] = defaultdict(dict)
    for (day, mandi), vals in seen.items():
        if len(vals) == 1:
            out[day][mandi] = vals[0]
    return out


def build_index(rows: Iterable[Obs], min_mandis: int = MIN_MANDIS) -> list[IndexPoint]:
    """INDEX_DATED_ONLY_DEMO_V1 for the rows of ONE variety: days with fewer than ``min_mandis`` mandis are absent."""
    by_day = _group_one_row_per_mandi(rows)
    out = []
    for day in sorted(by_day):
        prices = list(by_day[day].values())
        if len(prices) < min_mandis:
            continue
        med = _median_milli(prices)
        assert med.denominator == 1, "a median of 2-decimal prices must be exact in milli-rupees"
        out.append(IndexPoint(day, int(med), len(prices)))
    return out


# ---- point forecasts ----
def _season_days(points: Sequence[IndexPoint], origin: date) -> list[IndexPoint]:
    s = season_of(origin)
    return [p for p in points if p.season == s and p.day <= origin]


def forecast_naive(history: Sequence[IndexPoint]) -> Fraction:
    return Fraction(history[-1].milli)


def forecast_ma5(history: Sequence[IndexPoint]) -> Fraction:
    last = history[-MA_DAYS:]
    return Fraction(sum(p.milli for p in last), len(last))


METHODS: dict[str, Callable[[Sequence[IndexPoint]], Fraction]] = {
    "NAIVE": forecast_naive,
    "MOVING_AVERAGE_5": forecast_ma5,
}


def round_paise(milli: Fraction) -> RatePerQuintal:
    """Exact milli-rupees (any fraction) to paise, half-even."""
    return RatePerQuintal(round(milli / 10))


# ---- pairs and ranges ----
def error_pairs(points: Sequence[IndexPoint], method: str, h: int, until: date) -> list[Fraction]:
    """Absolute errors |actual - forecast| of every earlier forecast of horizon h whose target is an index day of the
    same season and not after ``until``. The origin must be an index day with at least 10 index days of its season."""
    f = METHODS[method]
    by_season: dict[str, list[IndexPoint]] = defaultdict(list)
    for p in points:
        by_season[p.season].append(p)
    errs: list[Fraction] = []
    for pts in by_season.values():
        pts = sorted(pts, key=lambda p: p.day)
        index_of = {p.day: p for p in pts}
        for i, o in enumerate(pts):
            if i + 1 < MIN_SEASON_DAYS:
                continue
            target = index_of.get(o.day + timedelta(days=h))
            if target is None or target.day > until:
                continue
            errs.append(abs(Fraction(target.milli) - f(pts[: i + 1])))
    return sorted(errs)


def half_width(sorted_errors: Sequence[Fraction], coverage: Fraction) -> Fraction | None:
    """RANGE_ABS_ERROR_CONFORMAL_V1: the ceil((n+1)c)-th smallest error; None below MIN_PAIRS or rank above n."""
    n = len(sorted_errors)
    if n < MIN_PAIRS:
        return None
    k = math.ceil((n + 1) * coverage)
    return sorted_errors[k - 1] if k <= n else None


@dataclass(frozen=True, slots=True)
class HorizonForecast:
    h: int
    target_date: date
    point: RatePerQuintal
    range80: tuple[RatePerQuintal, RatePerQuintal] | None
    range95: tuple[RatePerQuintal, RatePerQuintal] | None
    residual_pairs: int

    @property
    def status(self) -> str:
        return "RANGE_AVAILABLE" if self.range80 and self.range95 else "TOO_FEW_PAIRS"


class InsufficientHistory(Exception):
    """Fewer than MIN_SEASON_DAYS index days in the newest season (or no index at all)."""


def baseline_forecast(
    points: Sequence[IndexPoint], horizons: Sequence[int] = HORIZONS
) -> dict[str, list[HorizonForecast]]:
    """Forecast every method at the given horizons from the newest index day. Raises InsufficientHistory."""
    if not points:
        raise InsufficientHistory("no index days")
    pts = sorted(points, key=lambda p: p.day)
    origin = pts[-1].day
    history = _season_days(pts, origin)
    if len(history) < MIN_SEASON_DAYS:
        raise InsufficientHistory(f"{len(history)} index days in season {season_of(origin)}")
    out: dict[str, list[HorizonForecast]] = {}
    for name, f in METHODS.items():
        exact = f(history)
        rows = []
        for h in horizons:
            errs = error_pairs(pts, name, h, until=origin)
            ranges: dict[str, tuple[RatePerQuintal, RatePerQuintal] | None] = {}
            for label, cov in COVERAGES.items():
                w = half_width(errs, cov)
                ranges[label] = None if w is None else (round_paise(exact - w), round_paise(exact + w))
            both = ranges["80"] is not None and ranges["95"] is not None
            rows.append(
                HorizonForecast(
                    h,
                    origin + timedelta(days=h),
                    round_paise(exact),
                    ranges["80"] if both else None,
                    ranges["95"] if both else None,
                    len(errs),
                )
            )
        out[name] = rows
    return out


# ---- mandi basis ----
@dataclass(frozen=True, slots=True)
class Basis:
    mandi_id: str
    days_used: int
    basis: RatePerQuintal | None  # None when days_used < BASIS_MIN_DAYS

    @property
    def eligible(self) -> bool:
        return self.basis is not None


def mandi_basis(rows: Iterable[Obs], points: Sequence[IndexPoint], origin: date) -> list[Basis]:
    """BASIS_RECENT_MEDIAN_DEMO_V1: per mandi, the median over the 30 days ending at ``origin`` of (its modal
    price - the index that day), on days with exactly one row of that mandi and an index day. At least 5 days."""
    index = {p.day: p.milli for p in points}
    start = origin - timedelta(days=BASIS_WINDOW_DAYS - 1)
    diffs: dict[str, list[int]] = defaultdict(list)
    for day, per_mandi in _group_one_row_per_mandi(rows).items():
        if not (start <= day <= origin) or day not in index:
            continue
        for mandi, modal in per_mandi.items():
            diffs[mandi].append(modal - index[day])
    out = []
    for mandi in sorted(diffs):
        d = diffs[mandi]
        out.append(Basis(mandi, len(d), round_paise(_median_milli(d)) if len(d) >= BASIS_MIN_DAYS else None))
    return out

"""Index, baseline forecasts, ranges and mandi basis (plan/v1). Expected values are worked out by hand or by an
independent brute-force enumeration written inside the test, never by calling the function under test."""

from __future__ import annotations

import random
from datetime import date, timedelta
from fractions import Fraction

import pytest

from kisaniti_core.forecast import (
    BASIS_MIN_DAYS,
    MIN_PAIRS,
    IndexPoint,
    InsufficientHistory,
    Obs,
    baseline_forecast,
    build_index,
    error_pairs,
    half_width,
    mandi_basis,
    milli_from_decimal,
    milli_to_index_json,
    round_paise,
    season_of,
)

D = date


def obs(mandi: str, day: date, price: str | None, inferred: bool = False) -> Obs:
    return Obs(mandi, day, None if price is None else milli_from_decimal(price), inferred)


def series(start: date, values: list[int], step: int = 1) -> list[IndexPoint]:
    """Index points on consecutive days (step 1) with the given prices in rupees."""
    return [IndexPoint(start + timedelta(days=i * step), v * 1000, 3) for i, v in enumerate(values)]


# ---- conversions and seasons ----
def test_milli_conversion_is_exact_and_refuses_more_precision() -> None:
    assert milli_from_decimal("4355.00") == 4_355_000
    assert milli_from_decimal("12.5") == 12_500
    with pytest.raises(ValueError):
        milli_from_decimal("12.005")
    assert milli_to_index_json(4_012_375) == "4012.375"
    assert milli_to_index_json(4_012_000) == "4012.000"
    assert milli_to_index_json(5) == "0.005"


def test_season_starts_in_september() -> None:
    assert season_of(D(2026, 9, 1)) == "2026-27"
    assert season_of(D(2026, 8, 31)) == "2025-26"
    assert season_of(D(2027, 1, 10)) == "2026-27"


# ---- the index ----
def test_index_is_the_median_of_the_mandis_of_that_day_with_at_least_three() -> None:
    day = D(2026, 10, 1)
    rows = [obs("a", day, "3000.00"), obs("b", day, "3200.00"), obs("c", day, "3100.00")]
    (p,) = build_index(rows)
    assert (p.day, p.milli, p.n_mandis) == (day, 3_100_000, 3)


def test_index_of_an_even_count_is_the_exact_mean_of_the_two_middle_values() -> None:
    day = D(2026, 10, 1)
    rows = [obs("a", day, "4000.00"), obs("b", day, "4012.25"), obs("c", day, "4012.50"), obs("d", day, "9000.00")]
    (p,) = build_index(rows)
    assert p.milli == 4_012_375  # (4012.25 + 4012.50) / 2, three decimals, no rounding


def test_a_day_with_fewer_than_three_mandis_is_absent_not_filled() -> None:
    rows = [obs("a", D(2026, 10, 1), "3000.00"), obs("b", D(2026, 10, 1), "3100.00")]
    assert build_index(rows) == []


def test_a_mandi_day_with_two_rows_is_dropped_not_averaged() -> None:
    day = D(2026, 10, 1)
    rows = [obs("a", day, "3000.00"), obs("a", day, "9000.00"), obs("b", day, "3100.00"), obs("c", day, "3200.00")]
    assert build_index(rows) == []  # a is dropped, leaving two mandis
    rows += [obs("d", day, "3300.00")]
    (p,) = build_index(rows)
    assert (p.n_mandis, p.milli) == (3, 3_200_000)  # b, c, d only; a's 9000 never enters


def test_inferred_dates_and_missing_prices_are_not_used() -> None:
    day = D(2026, 10, 1)
    rows = [
        obs("a", day, "3000.00"),
        obs("b", day, "3100.00"),
        obs("c", day, "3200.00", inferred=True),
        obs("d", day, None),
    ]
    assert build_index(rows) == []
    assert len(build_index([*rows, obs("e", day, "3300.00")])) == 1


def test_index_days_are_sorted_and_independent() -> None:
    rows = []
    for day, base in ((D(2026, 10, 3), 3300), (D(2026, 10, 1), 3100)):
        rows += [obs(m, day, f"{base + i * 10}.00") for i, m in enumerate("abc")]
    assert [(p.day, p.milli) for p in build_index(rows)] == [(D(2026, 10, 1), 3_110_000), (D(2026, 10, 3), 3_310_000)]


# ---- point forecasts, by hand ----
HAND = series(D(2026, 9, 1), [100, 110, 120, 130, 140, 150, 160, 170, 180, 190, 200, 210])  # 12 days


def test_naive_and_moving_average_by_hand() -> None:
    f = baseline_forecast(HAND, horizons=(1,))
    assert f["NAIVE"][0].point.units == 21_000  # 210 rupees = 21 000 paise


def test_moving_average_is_the_mean_of_the_last_five_days() -> None:
    f = baseline_forecast(HAND, horizons=(1,))
    assert f["MOVING_AVERAGE_5"][0].point.units == 19_000  # (170+180+190+200+210)/5 = 190.00 rupees
    assert f["NAIVE"][0].target_date == D(2026, 9, 12) + timedelta(days=1)


def test_the_point_is_rounded_half_even_to_the_paisa_only_at_the_end() -> None:
    assert round_paise(Fraction(4_012_375)).units == 401_238  # .375 rupee-milli -> 4012.38 (8 is even)
    assert round_paise(Fraction(4_012_365)).units == 401_236  # .365 -> 4012.36 (6 is even)
    assert round_paise(Fraction(4_012_385)).units == 401_238  # .385 -> 4012.38 (8 is even)


def test_too_little_history_is_refused() -> None:
    with pytest.raises(InsufficientHistory):
        baseline_forecast(HAND[:9])
    with pytest.raises(InsufficientHistory):
        baseline_forecast([])


def test_history_of_an_older_season_does_not_count_for_the_current_one() -> None:
    old = series(D(2025, 9, 1), [100] * 30)
    new = series(D(2026, 9, 1), [100] * 9)
    with pytest.raises(InsufficientHistory):
        baseline_forecast(old + new)


# ---- error pairs against an independent enumeration ----
def random_index(seed: int, seasons: int = 3, days: int = 70, gaps: float = 0.2) -> list[IndexPoint]:
    rnd = random.Random(seed)
    pts = []
    for s in range(seasons):
        price = rnd.randint(2800, 4200) * 1000
        for i in range(days):
            price += rnd.randint(-40, 45) * 1000 + rnd.choice([0, 5, 500])
            if rnd.random() < gaps:
                continue
            pts.append(IndexPoint(date(2021 + s, 9, 10) + timedelta(days=i), price, 3))
    return pts


def brute_errors(points: list[IndexPoint], method: str, h: int, until: date) -> list[Fraction]:
    by_day = {p.day: p for p in points}
    out = []
    for o in points:
        same = sorted((p for p in points if p.season == o.season and p.day <= o.day), key=lambda p: p.day)
        if len(same) < 10:
            continue
        t = by_day.get(o.day + timedelta(days=h))
        if t is None or t.season != o.season or t.day > until:
            continue
        fc = Fraction(same[-1].milli) if method == "NAIVE" else Fraction(sum(p.milli for p in same[-5:]), 5)
        out.append(abs(Fraction(t.milli) - fc))
    return sorted(out)


@pytest.mark.parametrize("seed", range(6))
@pytest.mark.parametrize("method", ["NAIVE", "MOVING_AVERAGE_5"])
@pytest.mark.parametrize("h", [1, 7, 14])
def test_error_pairs_equal_an_independent_enumeration(seed: int, method: str, h: int) -> None:
    pts = random_index(seed)
    until = pts[-1].day
    assert error_pairs(pts, method, h, until) == brute_errors(pts, method, h, until)


def test_a_pair_never_crosses_a_season_boundary() -> None:
    # last index day of one season and first of the next are 1 day apart in the test data: must not pair
    a = series(D(2026, 8, 20), [100] * 12)  # season 2025-26, ends 2026-08-31
    b = series(D(2026, 9, 1), [500] * 12)  # season 2026-27
    e = error_pairs(a + b, "NAIVE", 1, D(2026, 12, 31))
    assert Fraction(400_000) not in e  # the 100 -> 500 jump is never an error pair


def test_no_forecast_error_uses_data_after_the_origin() -> None:
    pts = random_index(3)
    cut = pts[len(pts) // 2].day
    truncated = [p for p in pts if p.day <= cut]
    for method in ("NAIVE", "MOVING_AVERAGE_5"):
        for h in (1, 7, 14):
            assert error_pairs(pts, method, h, until=cut) == error_pairs(truncated, method, h, until=cut)


# ---- ranges ----
def test_half_width_is_the_ceil_n_plus_1_c_th_smallest_error() -> None:
    errs = [Fraction(i) for i in range(1, 51)]  # n = 50: ceil(51*0.8)=41 -> 41; ceil(51*0.95)=49 -> 49
    assert half_width(errs, Fraction(4, 5)) == 41
    assert half_width(errs, Fraction(19, 20)) == 49


def test_no_range_below_the_minimum_number_of_pairs() -> None:
    assert half_width([Fraction(i) for i in range(1, MIN_PAIRS)], Fraction(4, 5)) is None
    assert half_width([Fraction(i) for i in range(1, MIN_PAIRS + 1)], Fraction(4, 5)) is not None


def test_range_contains_the_point_and_the_95_range_contains_the_80_range() -> None:
    pts = random_index(1, seasons=4, days=90)
    f = baseline_forecast(pts)
    seen = 0
    for rows in f.values():
        for r in rows:
            if r.range80 and r.range95:
                seen += 1
                assert r.range80[0] <= r.point <= r.range80[1]
                assert r.range95[0] <= r.range80[0] and r.range80[1] <= r.range95[1]
    assert seen >= 3


def test_range_status_follows_the_pair_count() -> None:
    pts = random_index(2, seasons=1, days=60)
    for rows in baseline_forecast(pts).values():
        for r in rows:
            assert (r.status == "RANGE_AVAILABLE") == (r.residual_pairs >= MIN_PAIRS and r.range80 is not None)


def test_in_sample_coverage_is_at_least_nominal_on_exchangeable_errors() -> None:
    # Split-conformal guarantee on exchangeable data: with errors drawn i.i.d., a new error falls within the
    # ceil((n+1)c)-th smallest with probability >= c. Check the rate over many fresh draws.
    rnd = random.Random(7)
    hits = {Fraction(4, 5): 0, Fraction(19, 20): 0}
    trials = 3000
    for _ in range(trials):
        errs = sorted(Fraction(rnd.randint(0, 10_000)) for _ in range(MIN_PAIRS))
        new = Fraction(rnd.randint(0, 10_000))
        for c in hits:
            w = half_width(errs, c)
            assert w is not None
            hits[c] += new <= w
    assert (
        hits[Fraction(4, 5)] / trials >= 0.77 and hits[Fraction(19, 20)] / trials >= 0.93
    )  # nominal minus sampling slack


# ---- mandi basis ----
def basis_fixture() -> tuple[list[Obs], list[IndexPoint], date]:
    origin = D(2026, 10, 30)
    days = [origin - timedelta(days=i) for i in range(0, 8)]
    pts = [IndexPoint(d, 4_000_000, 3) for d in days]
    rows = []
    for d in days:
        rows.append(obs("hi", d, "4100.00"))  # always +100
        rows.append(
            obs("lo", d, "3900.00") if d >= origin - timedelta(days=3) else obs("lo", d - timedelta(days=100), "1.00")
        )
    return rows, pts, origin


def test_basis_is_the_median_difference_to_the_index_with_enough_days() -> None:
    rows, pts, origin = basis_fixture()
    got = {b.mandi_id: b for b in mandi_basis(rows, pts, origin)}
    assert got["hi"].days_used == 8 and got["hi"].basis is not None and got["hi"].basis.to_json() == "100.00"
    assert got["lo"].days_used == 4 and got["lo"].basis is None and not got["lo"].eligible  # fewer than 5 days
    assert BASIS_MIN_DAYS == 5


def test_basis_ignores_days_outside_the_window_without_an_index_day_or_with_duplicate_rows() -> None:
    origin = D(2026, 10, 30)
    pts = [IndexPoint(origin - timedelta(days=i), 4_000_000, 3) for i in range(0, 40)]
    pts = [p for p in pts if p.day != origin - timedelta(days=1)]  # no index on this day
    rows = [obs("m", p.day, "4050.00") for p in pts]  # +50 every day, 39 days of which only 30 lie in the window
    rows += [obs("m", origin - timedelta(days=2), "9999.00")]  # second row on a day: that day is dropped
    (b,) = mandi_basis(rows, pts, origin)
    assert b.basis is not None and b.basis.to_json() == "50.00"
    assert b.days_used == 30 - 1 - 1  # 30 window days, minus the day without index, minus the duplicate day


def test_basis_of_an_even_number_of_days_is_rounded_half_even_at_the_end() -> None:
    origin = D(2026, 10, 30)
    pts = [IndexPoint(origin - timedelta(days=i), 4_000_000, 3) for i in range(6)]
    prices = ["4000.01", "4000.02", "4000.03", "4000.04", "4000.05", "4000.06"]  # diffs 10..60 milli; median 35 milli
    rows = [obs("m", p.day, pr) for p, pr in zip(pts, prices, strict=True)]
    (b,) = mandi_basis(rows, pts, origin)
    assert b.basis is not None and b.days_used == 6
    assert b.basis.units == 4  # 35 milli = 3.5 paise -> half-even -> 4

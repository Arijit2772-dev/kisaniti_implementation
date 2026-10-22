"""STORAGE-DEMO-0.1 (contract L3B v1 §5.1): unit, boundary, hysteresis, property and contract tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from referencing import Registry, Resource

from kisaniti_core.provenance import Origin
from kisaniti_core.storage import (
    Adjustment,
    Assignment,
    Category,
    Crop,
    HysteresisState,
    InsectFlag,
    Level,
    Placement,
    Reading,
    UnknownReason,
    assess,
    assess_series,
    assessment_to_contract,
    assignment_from_contract,
    reading_from_contract,
)

T0 = datetime(2026, 10, 1, 6, 0, tzinfo=UTC)
FIVE = timedelta(minutes=5)
GRAIN = Assignment("kn-node-001", Placement.IN_GRAIN, Crop.PADDY, T0)
AT = T0 + timedelta(hours=30)  # settled (6 h) and a full 24 h window behind


def steady(rh: float, t: float, start: datetime = T0, end: datetime = AT, **kw: object) -> list[Reading]:
    out, now = [], start
    while now <= end:
        out.append(Reading(now, t, rh, **kw))  # type: ignore[arg-type]
        now += FIVE
    return out


def test_no_assignment_is_unknown() -> None:
    a = assess(steady(60, 20), None, AT)
    assert a.category is Category.UNKNOWN
    assert a.unknown_reasons == (UnknownReason.NO_ASSIGNMENT,)


def test_ambient_probe_is_unknown_but_reports_conditions() -> None:
    amb = Assignment("kn-node-001", Placement.AMBIENT, Crop.PADDY, T0)
    a = assess(steady(80, 30), amb, AT)
    assert a.category is Category.UNKNOWN
    assert a.unknown_reasons == (UnknownReason.NOT_IN_GRAIN,)
    assert a.rh_median_pct == 80 and a.temperature_median_c == 30
    assert a.mould_level is None


def test_settling_period() -> None:
    at = T0 + timedelta(hours=3)
    a = assess(steady(60, 20, end=at), GRAIN, at)
    assert UnknownReason.SETTLING in a.unknown_reasons
    assert a.category is Category.UNKNOWN
    # readings from the settling period are never used later either
    a2 = assess(steady(60, 20), GRAIN, T0 + timedelta(hours=20))
    assert a2.samples_used == (14 * 12) + 1
    assert UnknownReason.LOW_COVERAGE in a2.unknown_reasons


def test_low_band_and_insect_flag() -> None:
    a = assess(steady(60, 20), GRAIN, AT)
    assert a.category is Category.LOW
    assert a.insect_flag is InsectFlag.INSECTS_SLOW
    assert a.samples_used == 288 and a.samples_expected == 288
    assert a.aw_median == 0.6


@pytest.mark.parametrize(
    ("rh", "level"),
    [(64.99, Level.LOW), (65.0, Level.MEDIUM), (69.99, Level.MEDIUM), (70.0, Level.HIGH), (88.0, Level.HIGH)],
)
def test_aw_bands_at_boundaries(rh: float, level: Level) -> None:
    a = assess(steady(rh, 20), GRAIN, AT)
    assert a.mould_level is level
    assert a.category.value == level.value


def test_warm_grain_raises_low_to_medium() -> None:
    a = assess(steady(62, 28), GRAIN, AT)
    assert a.mould_level is Level.LOW and a.category is Category.MEDIUM
    assert a.adjustments == (Adjustment.WARM_25C_RAISED,)
    assert a.insect_flag is InsectFlag.INSECT_FAVOURABLE
    assert assess(steady(59.9, 28), GRAIN, AT).category is Category.LOW
    assert assess(steady(62, 24.9), GRAIN, AT).category is Category.LOW


def test_cold_grain_lowers_one_level() -> None:
    a = assess(steady(72, 8), GRAIN, AT)
    assert a.mould_level is Level.HIGH and a.category is Category.MEDIUM
    assert a.adjustments == (Adjustment.COLD_BELOW_10C_LOWERED,)
    assert assess(steady(60, 8), GRAIN, AT).category is Category.LOW
    assert assess(steady(60, 8), GRAIN, AT).insect_flag is InsectFlag.INSECTS_INACTIVE


def test_stale_after_15_minutes() -> None:
    readings = steady(60, 20, end=AT - timedelta(minutes=20))
    a = assess(readings, GRAIN, AT)
    assert a.unknown_reasons == (UnknownReason.STALE,)
    ok = assess(steady(60, 20, end=AT - timedelta(minutes=15)), GRAIN, AT)
    assert ok.category is Category.LOW


def test_coverage_boundary() -> None:
    full = steady(60, 20)
    window = [r for r in full if r.measured_at and r.measured_at > AT - timedelta(hours=24)]
    assert len(window) == 288
    keep_216 = window[-216:]
    assert assess(keep_216, GRAIN, AT).category is Category.LOW
    assert UnknownReason.LOW_COVERAGE in assess(window[-215:], GRAIN, AT).unknown_reasons


def test_untrusted_quarantined_and_invalid_readings_are_ignored() -> None:
    good = steady(60, 20)
    bad = (
        steady(95, 20, measured_at_trusted=False)
        + steady(95, 20, accepted=False)
        + [Reading(AT, None, 95.0), Reading(AT, 70.0, 95.0), Reading(None, 20.0, 95.0)]
    )
    a = assess(good + bad, GRAIN, AT)
    assert a.category is Category.LOW and a.samples_used == 288


def test_median_ignores_short_spikes() -> None:
    readings = steady(60, 20)
    spiked = [Reading(r.measured_at, 20, 99.0) if i % 10 == 0 else r for i, r in enumerate(readings)]
    assert assess(spiked, GRAIN, AT).category is Category.LOW


def test_hysteresis_escalates_at_once_and_holds_48h_before_de_escalating() -> None:
    hourly = [AT + timedelta(hours=h) for h in range(0, 24 * 8)]
    # 4 days at RH 72 (HIGH), then RH 67: raw MEDIUM and 3 points below 70, so it may de-escalate after 48 h
    high_end = AT + timedelta(days=4)
    readings = steady(72, 20, end=high_end) + steady(67, 20, start=high_end + FIVE, end=hourly[-1])
    series = assess_series(readings, GRAIN, hourly)
    cats = [a.category for a in series]
    assert cats[0] is Category.HIGH
    first_margin_met = next(i for i, a in enumerate(series) if a.rh_median_pct is not None and a.rh_median_pct <= 68)
    first_medium = cats.index(Category.MEDIUM)
    assert first_medium - first_margin_met == 48
    assert all(c is Category.HIGH for c in cats[:first_medium])


def test_hysteresis_margin_not_met_keeps_level() -> None:
    hourly = [AT + timedelta(hours=h) for h in range(0, 24 * 6)]
    high_end = AT + timedelta(days=2)
    readings = steady(72, 20, end=high_end) + steady(68.5, 20, start=high_end + FIVE, end=hourly[-1])
    assert all(a.category is Category.HIGH for a in assess_series(readings, GRAIN, hourly))


def test_rising_band_is_immediate() -> None:
    prev = HysteresisState(Level.LOW, None, GRAIN.assigned_at)
    assert assess(steady(71, 20), GRAIN, AT, prev).category is Category.HIGH


def test_unknown_keeps_state_and_new_assignment_resets_it() -> None:
    prev = HysteresisState(Level.HIGH, AT - timedelta(hours=10), GRAIN.assigned_at)
    stale = assess([], GRAIN, AT, prev)
    assert stale.category is Category.UNKNOWN and stale.state == prev
    moved = Assignment("kn-node-001", Placement.IN_GRAIN, Crop.PADDY, T0 + timedelta(hours=1))
    later = AT + timedelta(hours=1)
    a = assess(steady(60, 20, start=moved.assigned_at, end=later), moved, later, prev)
    assert a.category is Category.LOW


def test_taint_rule() -> None:
    assert assess(steady(60, 20), GRAIN, AT).origin is Origin.REAL
    mixed = [*steady(60, 20), Reading(AT, 20.0, 60.0, origin=Origin.SYNTHETIC)]
    assert assess(mixed, GRAIN, AT).origin is Origin.SYNTHETIC
    assert assess(steady(60, 20), GRAIN, AT, source_origin=Origin.SYNTHETIC).origin is Origin.SYNTHETIC


def test_naive_time_rejected() -> None:
    with pytest.raises(ValueError):
        assess([], GRAIN, datetime(2026, 10, 1))


# ---- properties (no previous state) ----

rh_values = st.floats(min_value=0, max_value=100, allow_nan=False)
t_values = st.floats(min_value=-10, max_value=60, allow_nan=False)


@settings(max_examples=150, deadline=None)
@given(rh=rh_values, t=t_values)
def test_final_category_is_band_or_one_step_away(rh: float, t: float) -> None:
    a = assess(steady(rh, t), GRAIN, AT)
    rank = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}
    assert a.mould_level is not None
    assert abs(rank[a.category.value] - rank[a.mould_level.value]) <= 1
    assert (a.adjustments != ()) == (
        rank[a.category.value] != rank[a.mould_level.value] or (a.mould_level is Level.LOW and t < 10)
    )


@settings(max_examples=150, deadline=None)
@given(rh=rh_values, delta=st.floats(min_value=0, max_value=30, allow_nan=False), t=t_values)
def test_wetter_grain_never_gets_a_lower_category(rh: float, delta: float, t: float) -> None:
    rank = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}
    lo = assess(steady(rh, t), GRAIN, AT)
    hi = assess(steady(min(100.0, rh + delta), t), GRAIN, AT)
    assert rank[hi.category.value] >= rank[lo.category.value]


# ---- contract round trip ----

CONTRACTS = Path(__file__).resolve().parents[3] / "contracts"


def _validator(name: str) -> Draft202012Validator:
    reg: Registry = Registry()
    for d in (CONTRACTS / "sensor" / "v1", CONTRACTS / "shared" / "v1"):
        for f in d.glob("*.schema.json"):
            doc = json.loads(f.read_text())
            reg = reg.with_resource(doc["$id"], Resource.from_contents(doc))
    sid = json.loads((CONTRACTS / "sensor" / "v1" / name).read_text())["$id"]
    return Draft202012Validator({"$ref": sid}, registry=reg)


@pytest.mark.parametrize(
    "case",
    ["low", "warm", "unknown_stale", "ambient", "no_assignment", "synthetic", "hysteresis_pending"],
)
def test_output_matches_storage_assessment_schema(case: str) -> None:
    v = _validator("storage_assessment.schema.json")
    amb = Assignment("kn-node-001", Placement.AMBIENT, Crop.WHEAT, T0)
    pending = HysteresisState(Level.HIGH, None, T0)
    a = {
        "low": lambda: assess(steady(60, 20), GRAIN, AT),
        "warm": lambda: assess(steady(62, 28), GRAIN, AT),
        "unknown_stale": lambda: assess([], GRAIN, AT),
        "ambient": lambda: assess(steady(70, 30), amb, AT),
        "no_assignment": lambda: assess([], None, AT),
        "synthetic": lambda: assess(steady(66, 20), GRAIN, AT, source_origin=Origin.SYNTHETIC),
        "hysteresis_pending": lambda: assess(steady(66, 20), GRAIN, AT, pending),
    }[case]()
    doc = assessment_to_contract(a)
    errors = [e.message for e in v.iter_errors(doc)]
    assert not errors, errors


def test_contract_inputs_parse() -> None:
    ex = CONTRACTS / "sensor" / "v1" / "examples" / "valid"
    r = reading_from_contract(json.loads((ex / "reading_device.json").read_text()))
    assert r.humidity_pct == 61.27 and r.origin is Origin.REAL and r.measured_at is not None
    twin = reading_from_contract(json.loads((ex / "reading_twin.json").read_text()))
    assert twin.origin is Origin.SYNTHETIC
    asg = assignment_from_contract(json.loads((ex / "assignment_in_grain.json").read_text()))
    assert asg.placement is Placement.IN_GRAIN and asg.sample_interval_s == 300

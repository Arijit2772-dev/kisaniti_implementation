"""STORAGE-DEMO-0.1: storage-condition category from an in-grain humidity/temperature probe.

Exact rule: kisaniti/docs/contracts/L3b_SENSOR_STORAGE_CONTRACT_v1.md §5.1, from Patiala research Part B.
Inter-granular relative humidity at equilibrium approximates grain water activity (aw = RH/100); FAO storage-fungi
limits are aw 0.65 and 0.70. The result estimates conditions that favour mould and insects at one probe point.
It does not detect spoilage, measure moisture % or predict shelf life.

Thresholds are compared in RH percentage points (65, 70, ...) so the boundaries are exact; aw is only reported.
Sensor values are floats on purpose: they are measurements, not stock or money.
"""

from __future__ import annotations

import bisect
import statistics
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from enum import StrEnum

from ..errors import ContractError, ErrorCode
from ..provenance import Origin, derived_origin

POLICY_VERSION = "STORAGE-DEMO-0.1"
LABEL = "Experimental approximation — not measured spoilage"
EVIDENCE_REF = "kisaniti/docs/research/2026-10-15_Patiala_Storage_Risk_and_Unknowns.md#part-b"

SETTLING = timedelta(hours=6)
WINDOW = timedelta(hours=24)
FRESH = timedelta(minutes=15)
COVERAGE = 0.75
RH_MEDIUM = 65.0  # aw 0.65, FAO minimum for storage fungi
RH_HIGH = 70.0  # aw 0.70
HYST_MARGIN_RH = 2.0
HYST_HOLD = timedelta(hours=48)
T_COLD = 10.0
T_WARM = 25.0
RH_WARM = 60.0  # aw 0.60
T_INSECTS_SLOW = 15.0
T_VALID = (-10.0, 60.0)


class Level(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


_RANK = {Level.LOW: 0, Level.MEDIUM: 1, Level.HIGH: 2}
_BY_RANK = {v: k for k, v in _RANK.items()}
_LOWER_BOUND_RH = {Level.MEDIUM: RH_MEDIUM, Level.HIGH: RH_HIGH}


class Category(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    UNKNOWN = "UNKNOWN"


class Placement(StrEnum):
    IN_GRAIN = "IN_GRAIN"
    AMBIENT = "AMBIENT"


class Crop(StrEnum):
    PADDY = "PADDY"  # includes basmati (CHAT-32)
    WHEAT = "WHEAT"


class UnknownReason(StrEnum):
    NO_ASSIGNMENT = "NO_ASSIGNMENT"
    NOT_IN_GRAIN = "NOT_IN_GRAIN"
    SETTLING = "SETTLING"
    STALE = "STALE"
    LOW_COVERAGE = "LOW_COVERAGE"


class Adjustment(StrEnum):
    COLD_BELOW_10C_LOWERED = "COLD_BELOW_10C_LOWERED"
    WARM_25C_RAISED = "WARM_25C_RAISED"


class InsectFlag(StrEnum):
    INSECT_FAVOURABLE = "INSECT_FAVOURABLE"
    INSECTS_SLOW = "INSECTS_SLOW"
    INSECTS_INACTIVE = "INSECTS_INACTIVE"


@dataclass(frozen=True, slots=True)
class Reading:
    """The parts of a SensorReadingV1 the rule needs. Times are aware UTC datetimes."""

    measured_at: datetime | None
    temperature_c: float | None
    humidity_pct: float | None
    measured_at_trusted: bool = True
    accepted: bool = True
    origin: Origin = Origin.REAL

    def usable_values(self) -> bool:
        t, rh = self.temperature_c, self.humidity_pct
        return t is not None and rh is not None and T_VALID[0] <= t <= T_VALID[1] and 0.0 <= rh <= 100.0


@dataclass(frozen=True, slots=True)
class Assignment:
    device_id: str
    placement: Placement
    crop: Crop
    assigned_at: datetime
    sample_interval_s: int = 300


@dataclass(frozen=True, slots=True)
class HysteresisState:
    level: Level | None = None
    below_since: datetime | None = None
    assigned_at: datetime | None = None  # internal: a new assignment resets the state


@dataclass(frozen=True, slots=True)
class Assessment:
    assessed_at: datetime
    category: Category
    unknown_reasons: tuple[UnknownReason, ...]
    mould_level: Level | None
    adjustments: tuple[Adjustment, ...]
    rh_median_pct: float | None
    temperature_median_c: float | None
    insect_flag: InsectFlag | None
    samples_used: int
    samples_expected: int
    latest_measured_at: datetime | None
    state: HysteresisState
    origin: Origin
    assignment: Assignment | None = field(default=None)

    @property
    def aw_median(self) -> float | None:
        return None if self.rh_median_pct is None else round(self.rh_median_pct / 100.0, 4)


def raw_level(rh_pct: float) -> Level:
    if rh_pct >= RH_HIGH:
        return Level.HIGH
    if rh_pct >= RH_MEDIUM:
        return Level.MEDIUM
    return Level.LOW


def insect_flag(t: float) -> InsectFlag:
    if t >= T_WARM:
        return InsectFlag.INSECT_FAVOURABLE
    if t >= T_INSECTS_SLOW:
        return InsectFlag.INSECTS_SLOW
    return InsectFlag.INSECTS_INACTIVE


def apply_hysteresis(raw: Level, rh_pct: float, prev: HysteresisState, at: datetime) -> HysteresisState:
    """Escalate at once; de-escalate only after RH stays >= 2 points below the current band for 48 h."""
    if prev.level is None or _RANK[raw] > _RANK[prev.level]:
        return replace(prev, level=raw, below_since=None)
    if raw == prev.level:
        return replace(prev, below_since=None)
    if rh_pct <= _LOWER_BOUND_RH[prev.level] - HYST_MARGIN_RH:
        since = prev.below_since or at
        if at - since >= HYST_HOLD:
            return replace(prev, level=raw, below_since=None)
        return replace(prev, below_since=since)
    return replace(prev, below_since=None)


def adjust_for_temperature(level: Level, t: float, rh_pct: float) -> tuple[Level, tuple[Adjustment, ...]]:
    if t < T_COLD:
        return _BY_RANK[max(0, _RANK[level] - 1)], (Adjustment.COLD_BELOW_10C_LOWERED,)
    if t >= T_WARM and rh_pct >= RH_WARM and level is Level.LOW:
        return Level.MEDIUM, (Adjustment.WARM_25C_RAISED,)
    return level, ()


def _check_aware(dt: datetime, name: str) -> None:
    if dt.tzinfo is None:
        raise ContractError(ErrorCode.INVALID_INSTANT, f"{name} must be timezone-aware UTC")


def assess(
    readings: Sequence[Reading],
    assignment: Assignment | None,
    assessed_at: datetime,
    previous: HysteresisState | None = None,
    source_origin: Origin = Origin.REAL,
) -> Assessment:
    """One assessment at ``assessed_at`` from the readings of one device (any order, any time span).

    ``source_origin`` is the origin of the registered device; the result's origin follows the taint rule over it and
    the readings in the window.
    """
    _check_aware(assessed_at, "assessed_at")
    prev = previous or HysteresisState()
    interval_s = assignment.sample_interval_s if assignment else 300
    expected = max(1, int(WINDOW.total_seconds() // interval_s))
    window_start = assessed_at - WINDOW

    if assignment is None:
        return Assessment(
            assessed_at,
            Category.UNKNOWN,
            (UnknownReason.NO_ASSIGNMENT,),
            None,
            (),
            None,
            None,
            None,
            0,
            expected,
            None,
            prev,
            source_origin,
        )
    _check_aware(assignment.assigned_at, "assigned_at")
    if prev.assigned_at != assignment.assigned_at:
        prev = HysteresisState(assigned_at=assignment.assigned_at)

    in_grain = assignment.placement is Placement.IN_GRAIN
    earliest = assignment.assigned_at + (SETTLING if in_grain else timedelta(0))
    usable = [
        r
        for r in readings
        if r.accepted
        and r.measured_at_trusted
        and r.measured_at is not None
        and r.usable_values()
        and window_start < r.measured_at <= assessed_at
        and r.measured_at >= earliest
    ]
    origin = derived_origin([source_origin, *(r.origin for r in usable)])
    latest = max((r.measured_at for r in usable if r.measured_at is not None), default=None)
    rh_med = statistics.median(r.humidity_pct for r in usable if r.humidity_pct is not None) if usable else None
    t_med = statistics.median(r.temperature_c for r in usable if r.temperature_c is not None) if usable else None

    reasons: list[UnknownReason] = []
    if not in_grain:
        reasons.append(UnknownReason.NOT_IN_GRAIN)
    if in_grain and assessed_at < assignment.assigned_at + SETTLING:
        reasons.append(UnknownReason.SETTLING)
    if latest is None or assessed_at - latest > FRESH:
        reasons.append(UnknownReason.STALE)
    if len(usable) < COVERAGE * expected:
        reasons.append(UnknownReason.LOW_COVERAGE)

    flag = insect_flag(t_med) if t_med is not None else None
    if reasons or rh_med is None or t_med is None:
        return Assessment(
            assessed_at,
            Category.UNKNOWN,
            tuple(reasons),
            None,
            (),
            rh_med,
            t_med,
            flag,
            len(usable),
            expected,
            latest,
            prev,
            origin,
            assignment,
        )

    state = apply_hysteresis(raw_level(rh_med), rh_med, prev, assessed_at)
    assert state.level is not None
    final, adjustments = adjust_for_temperature(state.level, t_med, rh_med)
    return Assessment(
        assessed_at,
        Category(final.value),
        (),
        state.level,
        adjustments,
        rh_med,
        t_med,
        flag,
        len(usable),
        expected,
        latest,
        state,
        origin,
        assignment,
    )


class ReadingIndex:
    """Readings sorted by trusted measurement time, so a long series can be assessed step by step quickly."""

    def __init__(self, readings: Sequence[Reading]) -> None:
        timed = [(r.measured_at, r) for r in readings if r.measured_at is not None]
        timed.sort(key=lambda pair: pair[0])
        self._times: list[datetime] = [t for t, _ in timed]
        self._items: list[Reading] = [r for _, r in timed]

    def window(self, start: datetime, end: datetime) -> list[Reading]:
        lo = bisect.bisect_right(self._times, start)
        hi = bisect.bisect_right(self._times, end)
        return self._items[lo:hi]


def assess_series(
    readings: Sequence[Reading],
    assignment: Assignment,
    times: Sequence[datetime],
    source_origin: Origin = Origin.REAL,
) -> list[Assessment]:
    """Assess at each time in order, carrying the hysteresis state forward."""
    index = ReadingIndex(readings)
    out: list[Assessment] = []
    state: HysteresisState | None = None
    for at in times:
        a = assess(index.window(at - WINDOW, at), assignment, at, state, source_origin)
        state = a.state
        out.append(a)
    return out

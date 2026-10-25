"""Placement at the time of a reading (contract devices/v1 §2a.4): pure functions, no database."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from kisaniti_core.provenance import Origin
from kisaniti_core.storage import Assignment, Crop, Placement, Reading

from kisaniti_api.devices.placement import assignment_at, counts_for, readings_for

T0 = datetime(2026, 10, 20, 8, 0, tzinfo=UTC)


def asg(offset_h: float, placement: Placement = Placement.IN_GRAIN, crop: Crop = Crop.PADDY) -> Assignment:
    return Assignment("kn-sim-001", placement, crop, T0 + timedelta(hours=offset_h), 300)


def reading(offset_h: float | None, trusted: bool = True) -> Reading:
    at = None if offset_h is None else T0 + timedelta(hours=offset_h)
    return Reading(at, 25.0, 60.0, measured_at_trusted=trusted, accepted=True, origin=Origin.SYNTHETIC)


def test_assignment_applies_from_its_own_instant_and_the_latest_wins() -> None:
    a1, a2 = asg(0), asg(10, Placement.AMBIENT)
    h = [a2, a1]  # order of the list must not matter
    assert assignment_at(h, T0 - timedelta(seconds=1)) is None
    assert assignment_at(h, T0) == a1  # inclusive at the start
    assert assignment_at(h, T0 + timedelta(hours=9, minutes=59)) == a1
    assert assignment_at(h, T0 + timedelta(hours=10)) == a2
    assert assignment_at(h, T0 + timedelta(days=30)) == a2


def test_an_assignment_in_the_future_does_not_apply_yet() -> None:
    assert assignment_at([asg(5)], T0) is None


def test_a_reading_counts_only_for_the_assignment_valid_at_its_own_time() -> None:
    a1, a2 = asg(0), asg(10)
    h = [a1, a2]
    assert counts_for(h, a1, reading(3)) and not counts_for(h, a2, reading(3))
    assert counts_for(h, a2, reading(10)) and not counts_for(h, a1, reading(10))
    assert counts_for(h, a2, reading(40))


def test_a_reading_from_before_any_assignment_counts_for_nothing() -> None:
    a1 = asg(0)
    assert not counts_for([a1], a1, reading(-1))


def test_untrusted_or_missing_time_is_never_matched() -> None:
    a1 = asg(0)
    assert not counts_for([a1], a1, reading(3, trusted=False))
    assert not counts_for([a1], a1, reading(None))
    assert not counts_for([a1], a1, reading(None, trusted=False))
    kept = readings_for([a1], a1, [reading(1), reading(2, trusted=False), reading(None), reading(-5)])
    assert [r.measured_at for r in kept] == [T0 + timedelta(hours=1)]

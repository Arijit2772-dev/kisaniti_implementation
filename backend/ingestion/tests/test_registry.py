"""DbRegistry: refresh rules, with a fake clock and a fake table (no database)."""

from __future__ import annotations

from kisaniti_ingestion.registry import DbRegistry


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def make(rows: list[tuple[str, str]], clock: Clock) -> tuple[DbRegistry, list[int]]:
    calls: list[int] = []

    def fetch() -> list[tuple[str, str]]:
        calls.append(1)
        return list(rows)

    return DbRegistry(fetch, ttl_s=30, miss_s=5, clock=clock), calls


def test_known_device_is_found_and_carries_its_origin() -> None:
    reg, _ = make([("kn-sim-001", "SIMULATOR")], Clock())
    assert reg.get("kn-sim-001") is not None and reg["kn-sim-001"].source_origin == "SIMULATOR"
    assert "kn-sim-001" in reg and "kn-other-999" not in reg and reg.get("kn-other-999") is None
    assert len(reg) == 1 and list(reg) == ["kn-sim-001"]


def test_table_is_read_again_only_after_the_ttl() -> None:
    clock = Clock()
    reg, calls = make([("kn-sim-001", "SIMULATOR")], clock)
    for _ in range(50):
        assert "kn-sim-001" in reg
    assert len(calls) == 1
    clock.t += 31
    assert "kn-sim-001" in reg
    assert len(calls) == 2


def test_unknown_device_causes_at_most_one_lookup_per_miss_interval() -> None:
    clock = Clock()
    rows: list[tuple[str, str]] = []
    reg, calls = make(rows, clock)
    assert "kn-new-001" not in reg  # first load
    n0 = len(calls)
    clock.t += 6
    for _ in range(100):  # a flood of messages from an unknown device
        assert "kn-new-001" not in reg
    assert len(calls) == n0 + 1  # one lookup, not a hundred
    rows.append(("kn-new-001", "DEVICE"))  # registered in the website meanwhile
    clock.t += 6
    assert "kn-new-001" in reg and reg["kn-new-001"].source_origin == "DEVICE"


def test_a_deactivated_device_disappears_after_the_next_refresh() -> None:
    clock = Clock()
    rows = [("kn-sim-001", "SIMULATOR")]
    reg, _ = make(rows, clock)
    assert "kn-sim-001" in reg
    rows.clear()  # the SQL lists active devices only
    clock.t += 31
    assert "kn-sim-001" not in reg

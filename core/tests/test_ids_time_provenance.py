from datetime import UTC, date, datetime

import pytest

from kisaniti_core import ContractError, ErrorCode
from kisaniti_core.ids import fixture_id, id_kind, is_fixture_id, new_id, validate_id
from kisaniti_core.provenance import (
    Generator,
    Mode,
    Origin,
    Provenance,
    SourceKind,
    check_mode,
    counts_for_accuracy,
    derived_origin,
)
from kisaniti_core.timeutil import business_date_of, date_range, format_instant, parse_business_date, parse_instant


def test_fixture_ids_are_deterministic_and_marked() -> None:
    a = fixture_id("ns", "lot", 7)
    assert a == fixture_id("ns", "lot", 7) and a != fixture_id("ns", "lot", 8)
    assert validate_id(a, "lot") == a and is_fixture_id(a) and id_kind(a) == "lot"


def test_new_id_sorts_by_time() -> None:
    a = new_id("run", now_ms=1, randomness=bytes(10))
    b = new_id("run", now_ms=2, randomness=bytes(10))
    assert a < b and not is_fixture_id(b)


@pytest.mark.parametrize(
    "bad", ["lot_123", "LOT_0000000000951YC3CSVS81Y403", "xyz_0000000000951YC3CSVS81Y403", 5, None]
)
def test_invalid_ids(bad: object) -> None:
    with pytest.raises(ContractError) as e:
        validate_id(bad)
    assert e.value.code == ErrorCode.INVALID_ID


def test_wrong_kind() -> None:
    with pytest.raises(ContractError):
        validate_id(fixture_id("ns", "mbr", 1), "lot")


def test_instant_rules() -> None:
    dt = parse_instant("2026-10-21T19:00:00.000Z")
    assert format_instant(dt) == "2026-10-21T19:00:00.000Z"
    assert business_date_of(dt, "Asia/Kolkata") == date(2026, 10, 22)
    for bad in ["2026-10-21T19:00:00Z", "2026-10-21T19:00:00.000+05:30", "2026-02-30T00:00:00.000Z"]:
        with pytest.raises(ContractError) as e:
            parse_instant(bad)
        assert e.value.code == ErrorCode.INVALID_INSTANT
    with pytest.raises(ContractError):
        format_instant(datetime(2026, 1, 1))
    with pytest.raises(ContractError):
        business_date_of(datetime(2026, 1, 1, tzinfo=UTC), "Europe/London")


def test_dates() -> None:
    assert parse_business_date("2024-02-29") == date(2024, 2, 29)
    with pytest.raises(ContractError) as e:
        parse_business_date("2025-02-29")
    assert e.value.code == ErrorCode.INVALID_DATE
    assert len(date_range(date(2026, 10, 22), date(2026, 10, 28))) == 7


def test_provenance_rules() -> None:
    with pytest.raises(ContractError):
        Provenance(Origin.SYNTHETIC, SourceKind.FIXTURE_GENERATOR, "x", "2026-10-22T06:00:00.000Z")
    Provenance(
        Origin.SYNTHETIC,
        SourceKind.FIXTURE_GENERATOR,
        "x",
        "2026-10-22T06:00:00.000Z",
        generator=Generator("g", "1", 0),
    )
    assert derived_origin([Origin.REAL, Origin.POLICY]) is Origin.REAL
    assert derived_origin([Origin.REAL, Origin.SYNTHETIC]) is Origin.SYNTHETIC
    with pytest.raises(ContractError) as e:
        check_mode(Mode.LIVE, [Origin.REAL, Origin.SYNTHETIC])
    assert e.value.code == ErrorCode.SYNTHETIC_INPUT_IN_LIVE
    check_mode(Mode.DEMO, [Origin.SYNTHETIC])
    assert counts_for_accuracy(Origin.REAL) and not counts_for_accuracy(Origin.POLICY)

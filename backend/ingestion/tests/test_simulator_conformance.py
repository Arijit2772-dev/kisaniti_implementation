"""Tests for the software node (H-2) and the conformance harness (H-1). All sensor data here is SYNTHETIC.

The harness is only worth having if it fails on broken devices and a broken backend, so most tests inject a fault
and require the matching finding.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from kisaniti_ingestion.conformance import (
    Finding,
    Record,
    RecordingLink,
    check_publisher,
    in_process_backend,
    run_receiver,
    selftest,
)
from kisaniti_ingestion.simulator import BUFFER_SAMPLES, SCENARIOS, InProcessLink, SimDevice, generate, run_simulation
from kisaniti_ingestion.validate import Device, schema_errors

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
DEV = "kn-sim-001"
REG = {DEV: Device(DEV, "SIMULATOR")}


def first_time(steps: int) -> datetime:
    return NOW - timedelta(minutes=5 * steps)


def fails(findings: list[Finding]) -> set[str]:
    return {f.code for f in findings if f.level == "FAIL"}


# ---- simulator ------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_every_scenario_produces_schema_valid_messages(name: str) -> None:
    docs = list(generate(SCENARIOS[name], 1, DEV, "a1b2c3d4", first_time(40), 40))
    assert len(docs) == 40
    assert all(not schema_errors(d, "device_telemetry.schema.json") for d in docs)
    assert all(d["source_origin"] == "SIMULATOR" for d in docs)
    assert [d["seq"] for d in docs] == list(range(40))


def test_same_seed_same_data_and_different_seed_different_data() -> None:
    def run(seed: int) -> list[dict[str, Any]]:
        return list(generate(SCENARIOS["humid_rise"], seed, DEV, "a1b2c3d4", first_time(30), 30))

    assert run(1) == run(1)
    assert run(1) != run(2)


def test_scenarios_do_what_they_say() -> None:
    n = 200
    humid = list(generate(SCENARIOS["humid_rise"], 3, DEV, "a1b2c3d4", first_time(n), n))
    assert humid[-1]["humidity_pct"] - humid[0]["humidity_pct"] > 20
    faulty = list(generate(SCENARIOS["sensor_fault"], 3, DEV, "a1b2c3d4", first_time(n), n))
    bad = [d for d in faulty if d["read_status"] != "OK"]
    assert 0.05 < len(bad) / n < 0.2
    assert all(d["temperature_c"] is None and d["humidity_pct"] is None for d in bad)
    assert all(isinstance(d["humidity_pct"], float) for d in faulty if d["read_status"] == "OK")
    late = list(generate(SCENARIOS["late_ntp"], 3, DEV, "a1b2c3d4", first_time(30), 30))
    assert {d["time_quality"] for d in late[:12]} == {"UPTIME_BACKFILLED"} and late[12]["time_quality"] == "NTP_SYNCED"
    never = list(generate(SCENARIOS["never_synced"], 3, DEV, "a1b2c3d4", first_time(30), 30))
    assert all(d["measured_at"] is None and d["time_quality"] == "UNSYNCED" for d in never)


def run_device(
    steps: int,
    scenario: str = "stable_dry",
    *,
    outage: range | None = None,
    duplicate_every: int = 0,
    withhold: int = 0,
) -> tuple[SimDevice, Any, RecordingLink]:
    handle, store = in_process_backend(REG)
    inner = InProcessLink(handle, lambda: NOW, duplicate_every=duplicate_every)
    link = RecordingLink(inner, withhold=withhold)
    dev = SimDevice(DEV, link)

    def on_step(step: int, _t: datetime) -> None:
        inner.down = outage is not None and step in outage

    run_simulation(dev, SCENARIOS[scenario], 1, "a1b2c3d4", first_time(steps), steps, on_step=on_step)
    inner.down = False
    dev.flush()
    return dev, store, link


def test_readings_are_stored_once_in_order_and_labelled_synthetic() -> None:
    dev, store, _ = run_device(30)
    rows = store.readings_for(DEV)
    assert [r["seq"] for r in rows] == list(range(30))
    assert dev.buffer == [] and dev.dropped == 0
    assert all(r["provenance"]["origin"] == "SYNTHETIC" and r["provenance"]["source_kind"] == "SIMULATOR" for r in rows)
    assert all(r["ingest_status"] == "ACCEPTED" for r in rows)


def test_outage_is_buffered_then_delivered_exactly_once_not_marked_resent() -> None:
    _, store, link = run_device(40, outage=range(10, 25))
    assert [r["seq"] for r in store.readings_for(DEV)] == list(range(40))
    assert store.counts()["sensor_reading_conflicts"] == 0
    # samples buffered during the outage were never sent before, so none of them is a re-send
    assert not any(r.doc["resent"] for r in link.records)
    assert [r.doc["seq"] for r in link.records] == list(range(40))
    assert fails(check_publisher(link.records, DEV, "SIMULATOR")) == set()


def test_buffer_overflow_drops_oldest_and_reports_it() -> None:
    steps = BUFFER_SAMPLES + 40
    dev, store, link = run_device(steps, outage=range(0, steps - 1))
    assert dev.dropped > 0
    stored = [r["seq"] for r in store.readings_for(DEV)]
    assert stored == sorted(stored) and len(stored) == steps - dev.dropped
    assert max(r.doc["dropped_since_boot"] for r in link.records) == dev.dropped
    assert fails(check_publisher(link.records, DEV, "SIMULATOR")) == set()


def test_duplicate_delivery_creates_no_second_row() -> None:
    _, store, _ = run_device(30, duplicate_every=3)
    assert len(store.readings_for(DEV)) == 30
    assert store.counts()["sensor_readings"] == 30 and store.counts()["sensor_reading_conflicts"] == 0


def test_lost_acks_make_the_device_resend_and_the_backend_say_duplicate() -> None:
    _, store, link = run_device(12, withhold=2)
    assert len(store.readings_for(DEV)) == 12
    assert sum(1 for r in link.records if r.doc["resent"]) >= 2
    assert fails(check_publisher(link.records, DEV, "SIMULATOR")) == set()


# ---- conformance harness: it passes a correct system ---------------------------------------------------------------
def test_selftest_passes_for_receiver_and_all_scenarios() -> None:
    results, findings = selftest(NOW)
    assert all(r.passed for r in results), [(r.case.case_id, r.got) for r in results if not r.passed]
    assert all(fails(f) == set() for f in findings.values())


# ---- conformance harness: it fails a broken system ----------------------------------------------------------------
def good_records(n: int = 8) -> list[Record]:
    docs = list(generate(SCENARIOS["stable_dry"], 1, DEV, "a1b2c3d4", first_time(n), n))
    return [Record(d, len(json.dumps(d)), acked=True) for d in docs]


def mutate(records: list[Record], i: int, **over: Any) -> list[Record]:
    out = list(records)
    out[i] = Record(dict(records[i].doc, **over), records[i].size, records[i].acked)
    return out


def test_publisher_check_accepts_a_clean_trace() -> None:
    assert check_publisher(good_records(), DEV, "SIMULATOR") == []


@pytest.mark.parametrize(
    ("code", "build"),
    [
        ("SOURCE_ORIGIN", lambda r: mutate(r, 2, source_origin="DEVICE")),
        ("SCHEMA", lambda r: mutate(r, 2, humidity_pct=150.0)),
        ("SCHEMA", lambda r: mutate(r, 2, surprise=1)),
        ("DEVICE_ID", lambda r: mutate(r, 2, device_id="kn-other-999")),
        ("FIRST_SEND_MARKED_RESENT", lambda r: mutate(r, 2, resent=True)),
        ("SEQ_NOT_CONSECUTIVE", lambda r: mutate(r, 3, seq=9)),
        ("INTERVAL_CHANGED", lambda r: mutate(r, 4, sample_interval_s=60)),
        ("TOO_LARGE", lambda r: [*r[:2], Record(r[2].doc, 2048, True), *r[3:]]),
        ("RESEND_NOT_MARKED", lambda r: [*r, Record(dict(r[1].doc), 300, True)]),
        ("RESEND_CHANGED", lambda r: [*r, Record(dict(r[1].doc, resent=True, humidity_pct=70.0), 300, True)]),
        ("UNACKED_NOT_RESENT", lambda r: [Record(r[0].doc, r[0].size, acked=False), *r[1:]]),
    ],
)
def test_publisher_check_catches_each_contract_violation(code: str, build: Any) -> None:
    assert code in fails(check_publisher(build(good_records()), DEV, "SIMULATOR"))


def test_publisher_check_decreasing_dropped_counter_is_caught() -> None:
    recs = mutate(mutate(good_records(), 2, dropped_since_boot=5), 3, dropped_since_boot=1)
    assert "DROPPED_DECREASED" in fails(check_publisher(recs, DEV, "SIMULATOR"))


def test_receiver_cases_fail_a_backend_that_acks_everything() -> None:
    def acks_everything(topic: str, payload: bytes) -> dict[str, Any]:
        doc = json.loads(payload) if payload[:1] == b"{" else {"boot_id": "00000000", "seq": 0}
        return {"boot_id": doc.get("boot_id"), "seq": doc.get("seq"), "status": "ACCEPTED"}

    results = run_receiver(acks_everything, DEV, "SIMULATOR", NOW)
    failed = {r.case.case_id for r in results if not r.passed}
    assert {"R02", "R05", "R06", "R07", "R08", "R09", "R12", "R13"} <= failed


def test_receiver_cases_fail_a_backend_that_drops_quarantined_acks() -> None:
    handle, _ = in_process_backend(REG)

    def never_acks_quarantine(topic: str, payload: bytes) -> dict[str, Any] | None:
        ack = handle(topic, payload, NOW)
        return None if ack and ack["status"] == "QUARANTINED" else ack

    failed = {r.case.case_id for r in run_receiver(never_acks_quarantine, DEV, "SIMULATOR", NOW) if not r.passed}
    assert {"R05", "R08", "R09", "R13"} <= failed

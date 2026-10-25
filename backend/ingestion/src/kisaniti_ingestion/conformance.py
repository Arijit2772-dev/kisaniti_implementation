"""Device conformance harness (H-1): is a publisher, or the backend that receives it, true to contract L3B v1?

Two directions, one rulebook (`contracts/sensor/v1` + contract §2):

* `receiver` — acts as a device and sends valid, duplicate, out-of-order and malformed messages; checks that the
  ack (or the absence of one) is what the contract says. In-process (CI) or over a live MQTT broker.
* `publisher` — acts as the backend for a device under test (real node, Wokwi twin or simulator): records what it
  publishes, withholds the first acks, and checks the messages against the contract rules.
* `selftest` — the simulator and the in-process backend check each other. No broker, no hardware; runs in CI.

Only rules stated in the contract are checked. Send order is not specified there, so it is not enforced.
"""

from __future__ import annotations

import argparse
import json
import secrets
import ssl
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from kisaniti_core.timeutil import format_instant

from .service import handle
from .store import CONTENT_FIELDS, ReadingStore, open_store
from .validate import Device, schema_errors

MAX_BYTES = 1024
Send = Callable[[str, bytes], dict[str, Any] | None]


# ---- receiver conformance: the backend must answer a device as the contract says ----------------------------------
@dataclass(frozen=True, slots=True)
class Case:
    case_id: str
    what: str
    expect_ack: str | None  # ACCEPTED, DUPLICATE, QUARANTINED, or None (no ack at all)
    topic_device: str | None  # None: the device's own topic
    payload: bytes


@dataclass(frozen=True, slots=True)
class CaseResult:
    case: Case
    got: str | None

    @property
    def passed(self) -> bool:
        return self.got == self.case.expect_ack


def _doc(device_id: str, boot_id: str, seq: int, origin: str, now: datetime, /, **over: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "schema": "kisaniti.device_telemetry.v1",
        "device_id": device_id,
        "boot_id": boot_id,
        "seq": seq,
        "measured_at": format_instant(now),
        "time_quality": "NTP_SYNCED",
        "uptime_ms": 1000,
        "temperature_c": 26.0,
        "humidity_pct": 60.0,
        "read_status": "OK",
        "sensor_model": "SHT30",
        "firmware_version": "conformance-0.1",
        "source_origin": origin,
        "sample_interval_s": 300,
        "resent": False,
        "dropped_since_boot": 0,
    }
    doc.update(over)
    return doc


def receiver_cases(device_id: str, origin: str, boot_id: str, now: datetime) -> list[Case]:
    """The cases, with fresh sequence numbers per run. `origin` is the device's registered source_origin."""

    def j(d: dict[str, Any]) -> bytes:
        return json.dumps(d, separators=(",", ":")).encode()

    other = "SIMULATOR" if origin != "SIMULATOR" else "DEVICE"

    def d(seq: int, **o: Any) -> dict[str, Any]:
        return _doc(device_id, boot_id, seq, origin, now, **o)

    return [
        Case("R01", "valid message is stored and acknowledged", "ACCEPTED", None, j(d(10))),
        Case(
            "R02", "the same message again (QoS 1 duplicate) gets DUPLICATE", "DUPLICATE", None, j(d(10, resent=True))
        ),
        Case(
            "R03",
            "same identity, different content: first copy kept, DUPLICATE",
            "DUPLICATE",
            None,
            j(d(10, humidity_pct=61.5)),
        ),
        Case(
            "R04",
            "older sequence number after a newer one is accepted (order is not required)",
            "ACCEPTED",
            None,
            j(d(5)),
        ),
        Case(
            "R05",
            "value out of the schema range is quarantined, not dropped silently",
            "QUARANTINED",
            None,
            j(d(11, humidity_pct=150.0)),
        ),
        Case("R06", "not JSON: no ack (nothing to address it to)", None, None, b"\xff not json"),
        Case(
            "R07",
            "payload for another device on this device's own topic is never acknowledged",
            None,
            "kn-other-999",
            j(d(12)),
        ),
        Case(
            "R08",
            "source_origin that differs from the registry is quarantined",
            "QUARANTINED",
            None,
            j(d(13, source_origin=other)),
        ),
        Case(
            "R09",
            "timestamp far in the future is quarantined",
            "QUARANTINED",
            None,
            j(d(14, measured_at=format_instant(now + timedelta(hours=1)))),
        ),
        Case(
            "R10",
            "failed sensor read with null values is stored (values never a false zero)",
            "ACCEPTED",
            None,
            j(d(15, read_status="CRC_ERROR", temperature_c=None, humidity_pct=None)),
        ),
        Case(
            "R11",
            "unsynchronised clock: measured_at null is stored",
            "ACCEPTED",
            None,
            j(d(16, time_quality="UNSYNCED", measured_at=None)),
        ),
        Case("R12", "boot_id of the wrong form leaves no usable identity: no ack", None, None, j(d(17, boot_id="xyz"))),
        Case("R13", "extra unknown field breaks the schema: quarantined", "QUARANTINED", None, j(d(18, surprise=1))),
    ]


def run_receiver(
    send: Send, device_id: str, origin: str, now: datetime, boot_id: str | None = None
) -> list[CaseResult]:
    boot = boot_id or secrets.token_hex(4)
    out = []
    for c in receiver_cases(device_id, origin, boot, now):
        topic = f"kisaniti/v1/devices/{c.topic_device or device_id}/telemetry"
        ack = send(topic, c.payload)
        out.append(CaseResult(c, None if ack is None else str(ack.get("status"))))
    return out


# ---- publisher conformance: a device (or simulator) must behave as the contract says ----------------------------
@dataclass(frozen=True, slots=True)
class Record:
    doc: dict[str, Any]
    size: int
    acked: bool  # the backend side acknowledged this delivery


@dataclass(frozen=True, slots=True)
class Finding:
    level: str  # FAIL or WARN
    code: str
    detail: str


def check_publisher(
    records: list[Record], device_id: str, expected_origin: str, grace_deliveries: int = 3
) -> list[Finding]:
    """Check what a device published, in arrival order. FAIL = contract broken; WARN = cannot be judged from a short
    capture."""
    f: list[Finding] = []
    first_seen: dict[tuple[str, int], dict[str, Any]] = {}
    last_new_seq: dict[str, int] = {}
    interval: dict[str, int] = {}
    dropped: dict[str, int] = {}
    for i, r in enumerate(records):
        d = r.doc
        where = f"record {i} ({d.get('boot_id')}:{d.get('seq')})"
        errs = schema_errors(d, "device_telemetry.schema.json")
        if errs:
            f.append(Finding("FAIL", "SCHEMA", f"{where}: {'; '.join(errs)[:200]}"))
            continue
        if r.size > MAX_BYTES:
            f.append(Finding("FAIL", "TOO_LARGE", f"{where}: {r.size} bytes, limit {MAX_BYTES}"))
        if d["device_id"] != device_id:
            f.append(Finding("FAIL", "DEVICE_ID", f"{where}: device_id {d['device_id']} on {device_id}'s topic"))
        if d["source_origin"] != expected_origin:
            f.append(Finding("FAIL", "SOURCE_ORIGIN", f"{where}: {d['source_origin']}, expected {expected_origin}"))
        boot, seq = d["boot_id"], d["seq"]
        if boot in interval and interval[boot] != d["sample_interval_s"]:
            f.append(Finding("FAIL", "INTERVAL_CHANGED", f"{where}: interval changed within one boot"))
        interval.setdefault(boot, d["sample_interval_s"])
        if d["dropped_since_boot"] < dropped.get(boot, 0):
            f.append(Finding("FAIL", "DROPPED_DECREASED", f"{where}: dropped_since_boot went down"))
        dropped[boot] = max(dropped.get(boot, 0), d["dropped_since_boot"])
        key = (boot, seq)
        if key in first_seen:
            if not d["resent"]:
                f.append(Finding("FAIL", "RESEND_NOT_MARKED", f"{where}: sent again with resent=false"))
            diff = [k for k in CONTENT_FIELDS if d.get(k) != first_seen[key].get(k)]
            if diff:
                f.append(Finding("FAIL", "RESEND_CHANGED", f"{where}: content differs on resend: {diff}"))
        else:
            first_seen[key] = d
            if d["resent"]:
                f.append(Finding("FAIL", "FIRST_SEND_MARKED_RESENT", f"{where}: resent=true on the first send"))
            expected = last_new_seq.get(boot, -1) + 1
            if seq != expected and not (seq > expected and d["dropped_since_boot"] > 0):
                f.append(Finding("FAIL", "SEQ_NOT_CONSECUTIVE", f"{where}: expected seq {expected}"))
            last_new_seq[boot] = max(last_new_seq.get(boot, -1), seq)
    # an unacknowledged sample has to be kept and sent again; a short capture may simply have ended too soon
    for i, r in enumerate(records):
        d = r.doc
        if r.acked or "boot_id" not in d or "seq" not in d:
            continue
        later = records[i + 1 :]
        again = any(x.doc.get("boot_id") == d["boot_id"] and x.doc.get("seq") == d["seq"] for x in later)
        acked_later = any(
            x.acked and x.doc.get("boot_id") == d["boot_id"] and x.doc.get("seq") == d["seq"] for x in later
        )
        overflowed = any(
            x.doc.get("boot_id") == d["boot_id"] and x.doc.get("dropped_since_boot", 0) > d["dropped_since_boot"]
            for x in later
        )
        if not again and overflowed:
            f.append(
                Finding(
                    "WARN",
                    "POSSIBLY_DROPPED_BY_OVERFLOW",
                    f"{d['boot_id']}:{d['seq']} not resent, but the device reported buffer overflow afterwards",
                )
            )
        elif not again and len(later) >= grace_deliveries:
            f.append(
                Finding(
                    "FAIL",
                    "UNACKED_NOT_RESENT",
                    f"{d['boot_id']}:{d['seq']} was not acknowledged and not sent again in {len(later)} later sends",
                )
            )
        elif not again:
            f.append(
                Finding("WARN", "RESEND_NOT_OBSERVED", f"{d['boot_id']}:{d['seq']}: capture ended before a resend")
            )
        elif not acked_later and len(later) >= grace_deliveries:
            f.append(Finding("WARN", "NEVER_ACKED", f"{d['boot_id']}:{d['seq']} never acknowledged in this capture"))
    return f


# ---- no-broker drivers: the simulator and the in-process backend check each other --------------------------------
def in_process_backend(
    devices: dict[str, Device],
) -> tuple[Callable[[str, bytes, datetime], dict[str, Any] | None], ReadingStore]:
    """The real validate -> store -> ack path on an in-memory store, as a function a Link can call."""
    store = open_store(":memory:")

    def call(topic: str, payload: bytes, received_at: datetime) -> dict[str, Any] | None:
        return handle(topic, payload, received_at, devices, store).ack

    return call, store


class RecordingLink:
    """Wraps a Link as the backend side of a publisher test: records every delivery and loses the acks of the first
    `withhold` deliveries on the way back (the backend still stored them, as it would if the ack were lost)."""

    def __init__(self, inner: Any, withhold: int = 0) -> None:
        self.inner, self.withhold = inner, withhold
        self.records: list[Record] = []

    def send(self, topic: str, payload: bytes) -> dict[str, Any] | None:
        ack = self.inner.send(topic, payload)  # ConnectionError passes through: never sent, nothing to record
        lost = self.withhold > 0 and ack is not None
        if lost:
            self.withhold -= 1
        self.records.append(Record(json.loads(payload), len(payload), acked=ack is not None and not lost))
        return None if lost else ack


def selftest(now: datetime | None = None) -> tuple[list[CaseResult], dict[str, list[Finding]]]:
    """Receiver cases against the in-process backend, and every simulator scenario checked as a publisher."""
    from .simulator import SCENARIOS, InProcessLink, SimDevice, run_simulation

    now = now or datetime.now(UTC).replace(microsecond=0)
    devices = {"kn-sim-001": Device("kn-sim-001", "SIMULATOR")}
    handle_fn, _ = in_process_backend(devices)
    results = run_receiver(lambda t, p: handle_fn(t, p, now), "kn-sim-001", "SIMULATOR", now)
    findings: dict[str, list[Finding]] = {}
    for name, scenario in SCENARIOS.items():
        link = RecordingLink(InProcessLink(handle_fn, lambda: now), withhold=2)
        dev = SimDevice("kn-sim-001", link)
        run_simulation(dev, scenario, 1, secrets.token_hex(4), now - timedelta(minutes=5 * 30), 30)
        findings[name] = check_publisher(link.records, "kn-sim-001", "SIMULATOR")
    return results, findings


# ---- live drivers ------------------------------------------------------------------------------------------------
def mqtt_sender(
    host: str, port: int, device_id: str, ca: str | None, user: str | None, password: str | None, wait: float = 4.0
) -> Send:
    from .simulator import MqttLink

    link = MqttLink(host, port, device_id, ca, user or device_id, password, timeout=wait)
    return link.send


def capture_publisher(
    host: str,
    port: int,
    device_id: str,
    ca: str | None,
    user: str | None,
    password: str | None,
    seconds: float,
    withhold: int,
) -> list[Record]:
    """Be the backend for one device: record its telemetry, withhold the first `withhold` acks, ack the rest."""
    import paho.mqtt.client as mqtt
    from paho.mqtt.enums import CallbackAPIVersion

    records: list[Record] = []
    lock = threading.Lock()
    withheld = 0
    c = mqtt.Client(CallbackAPIVersion.VERSION2, client_id=f"conformance-{secrets.token_hex(3)}")
    if user:
        c.username_pw_set(user, password)
    if ca:
        c.tls_set(ca_certs=ca, tls_version=ssl.PROTOCOL_TLS_CLIENT)

    def on_message(cl: Any, _u: Any, msg: Any) -> None:
        nonlocal withheld
        try:
            doc = json.loads(msg.payload)
        except ValueError:
            return
        with lock:
            seen_before = any(
                r.doc.get("boot_id") == doc.get("boot_id") and r.doc.get("seq") == doc.get("seq") and r.acked
                for r in records
            )
            hold = withheld < withhold and not seen_before
            if hold:
                withheld += 1
            records.append(Record(doc, len(msg.payload), acked=not hold))
        if not hold and isinstance(doc.get("boot_id"), str) and isinstance(doc.get("seq"), int):
            ack = {
                "schema": "kisaniti.device_ack.v1",
                "boot_id": doc["boot_id"],
                "seq": doc["seq"],
                "status": "DUPLICATE" if seen_before else "ACCEPTED",
            }
            cl.publish(f"kisaniti/v1/devices/{device_id}/ack", json.dumps(ack), qos=1)

    c.on_connect = lambda cl, *_: cl.subscribe(f"kisaniti/v1/devices/{device_id}/telemetry", qos=1)
    c.on_message = on_message
    c.connect(host, port)
    c.loop_start()
    threading.Event().wait(seconds)
    c.loop_stop()
    c.disconnect()
    return records


def _print(results: list[CaseResult]) -> int:
    for r in results:
        print(
            f"{'PASS' if r.passed else 'FAIL'} {r.case.case_id} {r.case.what} "
            f"(expected {r.case.expect_ack}, got {r.got})"
        )
    bad = sum(1 for r in results if not r.passed)
    print(json.dumps({"cases": len(results), "failed": bad}))
    return 1 if bad else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="KisanNiti device conformance (contract L3B v1).")
    sub = ap.add_subparsers(dest="mode", required=True)
    sub.add_parser("selftest", help="simulator and in-process backend check each other (no broker)")
    for name in ("receiver", "publisher"):
        p = sub.add_parser(name)
        p.add_argument("--host", default="localhost")
        p.add_argument("--port", type=int, default=8883)
        p.add_argument("--ca")
        p.add_argument("--user")
        p.add_argument("--password")
        p.add_argument("--device-id", required=True)
        p.add_argument("--origin", choices=["DEVICE", "WOKWI_TWIN", "SIMULATOR"], required=True)
        if name == "publisher":
            p.add_argument("--seconds", type=float, default=120)
            p.add_argument("--withhold", type=int, default=2, help="acks to withhold, to see that the device resends")
    a = ap.parse_args()
    if a.mode == "selftest":
        results, per_scenario = selftest()
        code = _print(results)
        for name, fs in per_scenario.items():
            bad = [f for f in fs if f.level == "FAIL"]
            print(f"{'FAIL' if bad else 'PASS'} publisher[{name}] {len(fs)} findings")
            for f in fs:
                print(f"  {f.level} {f.code}: {f.detail}")
            code |= int(bool(bad))
        return int(code)
    if a.mode == "receiver":
        send = mqtt_sender(a.host, a.port, a.device_id, a.ca, a.user, a.password)
        return _print(run_receiver(send, a.device_id, a.origin, datetime.now(UTC)))
    recs = capture_publisher(a.host, a.port, a.device_id, a.ca, a.user, a.password, a.seconds, a.withhold)
    found = check_publisher(recs, a.device_id, a.origin)
    for f in found:
        print(f"{f.level} {f.code}: {f.detail}")
    n_fail = sum(1 for f in found if f.level == "FAIL")
    print(json.dumps({"messages": len(recs), "fail": n_fail, "warn": len(found) - n_fail}))
    return 1 if n_fail or not recs else 0


if __name__ == "__main__":
    sys.exit(main())

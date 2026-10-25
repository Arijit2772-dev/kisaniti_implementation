"""Software storage node (H-2): publishes DeviceTelemetryV1 exactly as the firmware does, with no hardware.

Everything it produces is SYNTHETIC. The registry must list its device as source_origin `SIMULATOR`, so the
backend stores the readings with origin SYNTHETIC. The scenarios exercise mechanics (humid rise, sensor faults,
outage with resend, duplicate delivery, clock problems); they are not a model of real grain or a real sensor, and
no accuracy or benefit claim may rest on them.

Pure core: `generate()` makes samples from (scenario, seed). `SimDevice` adds the firmware's buffer-and-ack
behaviour on top of a `Link`. Links: `InProcessLink` (straight into `service.handle`, for tests and CI) and
`MqttLink` (a real broker, used by `kn-sim`).
"""

from __future__ import annotations

import argparse
import json
import math
import random
import secrets
import ssl
import sys
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from kisaniti_core.timeutil import format_instant

BUFFER_SAMPLES = 288  # firmware v1 ring buffer (contract §2): 24 h at 5 min
FIRMWARE_VERSION = "sim-0.1"
SENSOR_MODEL = "SHT30"
FAULTS = ("CRC_ERROR", "NO_ACK", "TIMEOUT")


@dataclass(frozen=True, slots=True)
class Scenario:
    name: str
    description: str
    humidity: Callable[[int, int, random.Random], float]  # (step, steps, rng) -> %RH
    temperature: Callable[[int, datetime, random.Random], float]  # (step, time, rng) -> deg C
    fault_rate: float = 0.0  # share of samples whose read fails
    late_ntp_steps: int = 0  # first N samples taken before NTP sync (sent as UPTIME_BACKFILLED)
    never_synced: bool = False  # clock never syncs: measured_at null, UNSYNCED


def _diurnal(base: float, amp: float) -> Callable[[int, datetime, random.Random], float]:
    return (
        lambda _s, t, r: base + amp * math.sin(2 * math.pi * (t.hour + t.minute / 60 - 9) / 24) + r.uniform(-0.2, 0.2)
    )


SCENARIOS: dict[str, Scenario] = {
    s.name: s
    for s in (
        Scenario(
            "stable_dry",
            "Dry, steady grain; the storage category should stay low.",
            lambda _s, _n, r: 55.0 + r.uniform(-1.5, 1.5),
            _diurnal(25.0, 2.0),
        ),
        Scenario(
            "humid_rise",
            "Humidity climbs steadily from 55 to 82 %RH.",
            lambda s, n, r: 55.0 + 27.0 * s / max(1, n - 1) + r.uniform(-0.8, 0.8),
            _diurnal(27.0, 2.0),
        ),
        Scenario(
            "sensor_fault",
            "Dry grain, but 12 % of reads fail (CRC_ERROR, NO_ACK, TIMEOUT).",
            lambda _s, _n, r: 56.0 + r.uniform(-1.5, 1.5),
            _diurnal(25.0, 2.0),
            fault_rate=0.12,
        ),
        Scenario(
            "late_ntp",
            "The first 12 samples are taken before the clock syncs (UPTIME_BACKFILLED).",
            lambda _s, _n, r: 58.0 + r.uniform(-1.0, 1.0),
            _diurnal(25.0, 2.0),
            late_ntp_steps=12,
        ),
        Scenario(
            "never_synced",
            "The clock never syncs: measured_at is null (UNSYNCED).",
            lambda _s, _n, r: 58.0 + r.uniform(-1.0, 1.0),
            _diurnal(25.0, 2.0),
            never_synced=True,
        ),
    )
}


def generate(
    scenario: Scenario, seed: int, device_id: str, boot_id: str, first_time: datetime, steps: int, interval_s: int = 300
) -> Iterator[dict[str, Any]]:
    """Yield `steps` DeviceTelemetryV1 documents, deterministic in (scenario, seed, boot_id, first_time)."""
    rng = random.Random(seed)
    for step in range(steps):
        t = first_time + timedelta(seconds=interval_s * step)
        failed = rng.random() < scenario.fault_rate
        status = rng.choice(FAULTS) if failed else "OK"
        temp = round(scenario.temperature(step, t, rng), 2)
        rh = round(min(100.0, max(0.0, scenario.humidity(step, steps, rng))), 2)
        if scenario.never_synced:
            quality, measured = "UNSYNCED", None
        elif step < scenario.late_ntp_steps:
            quality, measured = "UPTIME_BACKFILLED", format_instant(t)
        else:
            quality, measured = "NTP_SYNCED", format_instant(t)
        yield {
            "schema": "kisaniti.device_telemetry.v1",
            "device_id": device_id,
            "boot_id": boot_id,
            "seq": step,
            "measured_at": measured,
            "time_quality": quality,
            "uptime_ms": step * interval_s * 1000,
            "temperature_c": temp if status == "OK" else None,
            "humidity_pct": rh if status == "OK" else None,
            "read_status": status,
            "sensor_model": SENSOR_MODEL,
            "firmware_version": FIRMWARE_VERSION,
            "source_origin": "SIMULATOR",
            "sample_interval_s": interval_s,
            "resent": False,
            "dropped_since_boot": 0,
        }


class Link(Protocol):
    """How a device reaches the backend. `send` returns the ack document, or None if the message went out but no
    ack came back. It raises ConnectionError when the message could not be sent at all (link down)."""

    def send(self, topic: str, payload: bytes) -> dict[str, Any] | None: ...


@dataclass
class SimDevice:
    """Firmware behaviour from the contract: keep samples in a ring buffer, send oldest first, drop from the buffer
    only on an ack for that exact (boot_id, seq), mark re-sends with resent=true, count samples lost to overflow."""

    device_id: str
    link: Link
    buffer: list[dict[str, Any]] = field(default_factory=list)
    sent_once: set[int] = field(default_factory=set)
    dropped: int = 0
    log: list[tuple[int, str]] = field(default_factory=list)  # (seq, ack status or NO_ACK)

    def offer(self, sample: dict[str, Any]) -> None:
        self.buffer.append(sample)
        while len(self.buffer) > BUFFER_SAMPLES:
            self.buffer.pop(0)
            self.dropped += 1

    def flush(self) -> None:
        topic = f"kisaniti/v1/devices/{self.device_id}/telemetry"
        for sample in list(self.buffer):
            doc = dict(sample, resent=sample["seq"] in self.sent_once, dropped_since_boot=self.dropped)
            try:
                ack = self.link.send(topic, json.dumps(doc, separators=(",", ":")).encode())
            except ConnectionError:
                self.log.append((sample["seq"], "NOT_SENT"))
                return  # not sent, so a later send is not a re-send
            self.sent_once.add(sample["seq"])
            if ack is None or ack.get("boot_id") != sample["boot_id"] or ack.get("seq") != sample["seq"]:
                self.log.append((sample["seq"], "NO_ACK"))
                return  # keep order: stop at the first unacknowledged sample
            self.log.append((sample["seq"], str(ack["status"])))
            self.buffer.remove(sample)


class InProcessLink:
    """Straight into the backend's message handler, with a simulated clock and optional faults.
    Set `down = True` to simulate an outage (sending raises ConnectionError); `duplicate_every`: deliver
    every n-th message twice (a QoS 1 duplicate)."""

    def __init__(
        self,
        handle: Callable[[str, bytes, datetime], dict[str, Any] | None],
        now: Callable[[], datetime],
        duplicate_every: int = 0,
    ) -> None:
        self._handle, self._now, self.duplicate_every = handle, now, duplicate_every
        self.down = False
        self.sent = 0

    def send(self, topic: str, payload: bytes) -> dict[str, Any] | None:
        if self.down:
            raise ConnectionError("link down")
        self.sent += 1
        ack = self._handle(topic, payload, self._now())
        if self.duplicate_every and self.sent % self.duplicate_every == 0:
            ack = self._handle(topic, payload, self._now())
        return ack


def run_simulation(
    device: SimDevice,
    scenario: Scenario,
    seed: int,
    boot_id: str,
    first_time: datetime,
    steps: int,
    interval_s: int = 300,
    on_step: Callable[[int, datetime], None] | None = None,
) -> None:
    """Take `steps` samples one interval apart, flushing after each. `on_step` can move a simulated clock or end
    an outage between steps."""
    for sample in generate(scenario, seed, device.device_id, boot_id, first_time, steps, interval_s):
        if on_step is not None:
            on_step(sample["seq"], first_time + timedelta(seconds=interval_s * sample["seq"]))
        device.offer(sample)
        device.flush()


class MqttLink:
    """A real broker. Publishes QoS 1 and waits for the matching ack on kisaniti/v1/devices/{id}/ack."""

    def __init__(
        self,
        host: str,
        port: int,
        device_id: str,
        ca: str | None,
        user: str | None,
        password: str | None,
        timeout: float = 10.0,
    ) -> None:
        import paho.mqtt.client as mqtt
        from paho.mqtt.enums import CallbackAPIVersion

        self._timeout = timeout
        self._acks: dict[tuple[str, int], dict[str, Any]] = {}
        self._cond = threading.Condition()
        self._c = mqtt.Client(CallbackAPIVersion.VERSION2, client_id=f"{device_id}-{secrets.token_hex(3)}")
        if user:
            self._c.username_pw_set(user, password)
        if ca:
            self._c.tls_set(ca_certs=ca, tls_version=ssl.PROTOCOL_TLS_CLIENT)
        ready = threading.Event()
        self._c.on_connect = lambda cl, *_: cl.subscribe(f"kisaniti/v1/devices/{device_id}/ack", qos=1)
        self._c.on_subscribe = lambda *_: ready.set()
        self._c.on_message = self._on_message
        self._c.connect(host, port)
        self._c.loop_start()
        if not ready.wait(timeout):
            raise TimeoutError("could not subscribe to the ack topic")

    def _on_message(self, _c: Any, _u: Any, msg: Any) -> None:
        ack = json.loads(msg.payload)
        with self._cond:
            self._acks[(ack.get("boot_id"), ack.get("seq"))] = ack
            self._cond.notify_all()

    def send(self, topic: str, payload: bytes) -> dict[str, Any] | None:
        """Publish and wait for the ack of this (boot_id, seq). A payload without a readable identity (the
        conformance harness sends such on purpose) waits the full timeout for any ack at all."""
        try:
            doc = json.loads(payload)
            key: tuple[Any, Any] | None = (doc["boot_id"], doc["seq"])
            hash(key)
        except (ValueError, KeyError, TypeError):
            key = None
        info = self._c.publish(topic, payload, qos=1)
        if info.rc != 0:  # MQTT_ERR_SUCCESS: not connected or queue full
            raise ConnectionError(f"publish failed (rc={info.rc})")
        with self._cond:
            if key is not None:
                if self._cond.wait_for(lambda: key in self._acks, self._timeout):
                    return self._acks.pop(key)
            elif self._cond.wait_for(lambda: bool(self._acks), self._timeout):
                return self._acks.pop(next(iter(self._acks)))
        return None

    def close(self) -> None:
        self._c.loop_stop()
        self._c.disconnect()


def main() -> int:
    ap = argparse.ArgumentParser(description="Software storage node: publish a synthetic scenario to a broker.")
    ap.add_argument("--scenario", choices=sorted(SCENARIOS), default="stable_dry")
    ap.add_argument("--steps", type=int, default=48)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--device-id", default="kn-sim-001")
    ap.add_argument("--interval-s", type=int, default=300, help="sample spacing in the data (not wall-clock waiting)")
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--port", type=int, default=8883)
    ap.add_argument("--ca")
    ap.add_argument("--user")
    ap.add_argument("--password")
    ap.add_argument("--ack-timeout", type=float, default=10.0, help="seconds to wait for each ack")
    ap.add_argument("--list", action="store_true", help="list scenarios and exit")
    a = ap.parse_args()
    if a.list:
        for s in SCENARIOS.values():
            print(f"{s.name:14s} {s.description}")
        return 0
    boot = secrets.token_hex(4)
    # history ends now, so no sample is dated in the future (the backend quarantines future timestamps)
    first = datetime.now(UTC).replace(microsecond=0) - timedelta(seconds=a.interval_s * a.steps)
    link = MqttLink(a.host, a.port, a.device_id, a.ca, a.user or a.device_id, a.password, a.ack_timeout)
    dev = SimDevice(a.device_id, link)
    try:
        run_simulation(dev, SCENARIOS[a.scenario], a.seed, boot, first, a.steps, a.interval_s)
    finally:
        link.close()
    summary: dict[str, int] = {}
    for _, status in dev.log:
        summary[status] = summary.get(status, 0) + 1
    print(
        json.dumps(
            {
                "device": a.device_id,
                "boot_id": boot,
                "scenario": a.scenario,
                "acks": summary,
                "still_buffered": len(dev.buffer),
            }
        )
    )
    return 0 if not dev.buffer else 1


if __name__ == "__main__":
    sys.exit(main())

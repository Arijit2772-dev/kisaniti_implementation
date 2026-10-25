"""Which assignment applies at a given instant, and which readings count for it (contract devices/v1 §2a.4).

Pure functions. The rule is the one in contract L3B §3: the server decides placement from the assignment history at
the reading's own measurement time, and never guesses when that time is not trustworthy.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from kisaniti_core.storage import Assignment, Reading


def assignment_at(history: Sequence[Assignment], t: datetime) -> Assignment | None:
    """The latest assignment with ``assigned_at <= t`` (an assignment applies from the instant it starts)."""
    best: Assignment | None = None
    for a in history:
        if a.assigned_at <= t and (best is None or a.assigned_at > best.assigned_at):
            best = a
    return best


def counts_for(history: Sequence[Assignment], current: Assignment, reading: Reading) -> bool:
    """True only if the reading has a trusted time and the assignment valid at that time is ``current``.

    A reading with an untrusted or missing time is never matched; a reading from before ``current`` began belongs to an
    earlier assignment (or to none) and does not count for ``current``."""
    if not reading.measured_at_trusted or reading.measured_at is None:
        return False
    at_reading = assignment_at(history, reading.measured_at)
    return at_reading is not None and at_reading.assigned_at == current.assigned_at


def readings_for(history: Sequence[Assignment], current: Assignment, readings: Sequence[Reading]) -> list[Reading]:
    return [r for r in readings if counts_for(history, current, r)]

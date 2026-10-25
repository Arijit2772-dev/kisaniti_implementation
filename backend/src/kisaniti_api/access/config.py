"""Policy values for sessions, throttling and hashing. DEMO policy: configuration, not contract (L0_ACCESS §1, §5-7)."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AccessSettings:
    idle_minutes: int
    absolute_minutes: int
    throttle_failures: int
    throttle_window_minutes: int
    throttle_block_minutes: int
    cookie_secure: bool
    allowed_origins: frozenset[str]
    scrypt_n: int


def load_access_settings() -> AccessSettings:
    """Read on every call so tests and operators can change the environment without a restart of the module."""
    origins = [o.strip().rstrip("/") for o in os.environ.get("KISANITI_ALLOWED_ORIGINS", "").split(",") if o.strip()]
    return AccessSettings(
        idle_minutes=int(os.environ.get("KISANITI_SESSION_IDLE_MIN", "120")),
        absolute_minutes=int(os.environ.get("KISANITI_SESSION_ABSOLUTE_MIN", "720")),
        throttle_failures=int(os.environ.get("KISANITI_THROTTLE_FAILURES", "5")),
        throttle_window_minutes=int(os.environ.get("KISANITI_THROTTLE_WINDOW_MIN", "15")),
        throttle_block_minutes=int(os.environ.get("KISANITI_THROTTLE_BLOCK_MIN", "15")),
        cookie_secure=os.environ.get("KISANITI_COOKIE_SECURE", "true").lower() != "false",
        allowed_origins=frozenset(origins),
        scrypt_n=int(os.environ.get("KISANITI_SCRYPT_N", str(2**17))),
    )

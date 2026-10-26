"""Canonical identifiers (CONTRACT_SPEC_V1_shared.md §1): ``<kind>_<ULID>``."""

from __future__ import annotations

import hashlib
import os
import re
import time

from .errors import ContractError, ErrorCode

KINDS: frozenset[str] = frozenset(
    {"fpo", "mbr", "lot", "mnd", "cmd", "stl", "usr", "dev", "pln", "rev", "run", "src", "dsv", "pol"}
)
TENANT_OWNED_KINDS: frozenset[str] = frozenset({"mbr", "lot", "stl", "usr", "dev", "pln", "rev", "run"})
CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_ID_RE = re.compile(r"^(fpo|mbr|lot|mnd|cmd|stl|usr|dev|pln|rev|run|src|dsv|pol)_[0-9A-HJKMNP-TV-Z]{26}$")
FIXTURE_TIME_PART = "0000000000"


def _b32(value: int, length: int) -> str:
    out = []
    for _ in range(length):
        value, r = divmod(value, 32)
        out.append(CROCKFORD[r])
    if value:
        raise ValueError("value too large for the requested length")
    return "".join(reversed(out))


def validate_id(value: object, kind: str | None = None, path: str | None = None) -> str:
    """Return the ID unchanged if it is canonical (and of ``kind`` when given); otherwise raise INVALID_ID."""
    if not isinstance(value, str) or not _ID_RE.match(value):
        raise ContractError(ErrorCode.INVALID_ID, f"{value!r} is not a canonical ID", path)
    if kind is not None and not value.startswith(kind + "_"):
        raise ContractError(ErrorCode.INVALID_ID, f"{value!r} is not a {kind!r} ID", path)
    return value


def id_kind(value: str) -> str:
    return validate_id(value).split("_", 1)[0]


# Slice-local kinds (messaging/v1): same ULID format, not part of the frozen shared/v1 kind list.
LOCAL_KINDS: frozenset[str] = frozenset({"ins", "att", "sal", "rpl", "hnd"})


def new_local_id(kind: str) -> str:
    """A fresh ID of a slice-local kind (see LOCAL_KINDS): ``ins_<ULID>`` or ``att_<ULID>``."""
    if kind not in LOCAL_KINDS:
        raise ContractError(ErrorCode.INVALID_ID, f"unknown slice-local ID kind {kind!r}")
    return kind + new_id("run")[3:]


def new_id(kind: str, *, now_ms: int | None = None, randomness: bytes | None = None) -> str:
    """A fresh ULID-based ID (48-bit millisecond time + 80 random bits)."""
    if kind not in KINDS:
        raise ContractError(ErrorCode.INVALID_ID, f"unknown ID kind {kind!r}")
    ms = int(time.time() * 1000) if now_ms is None else now_ms
    rnd = os.urandom(10) if randomness is None else randomness
    if len(rnd) != 10:
        raise ValueError("randomness must be 10 bytes")
    return f"{kind}_{_b32(ms, 10)}{_b32(int.from_bytes(rnd, 'big'), 16)}"


def fixture_id(namespace: str, kind: str, key: str | int) -> str:
    """Deterministic fixture ID: time part 0000000000 + first 80 bits of SHA-256('fixture:ns:kind:key')."""
    if kind not in KINDS:
        raise ContractError(ErrorCode.INVALID_ID, f"unknown ID kind {kind!r}")
    digest = hashlib.sha256(f"fixture:{namespace}:{kind}:{key}".encode()).digest()
    return f"{kind}_{FIXTURE_TIME_PART}{_b32(int.from_bytes(digest[:10], 'big'), 16)}"


def is_fixture_id(value: str) -> bool:
    return validate_id(value).split("_", 1)[1].startswith(FIXTURE_TIME_PART)

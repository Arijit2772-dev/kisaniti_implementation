"""Time rules (CONTRACT_SPEC_V1_shared.md §4): UTC instants vs FPO-local business dates."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta, timezone, tzinfo

from .errors import ContractError, ErrorCode

_INSTANT_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})\.(\d{3})Z$")
_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")

# V1 allows only Asia/Kolkata. India has used UTC+05:30 without daylight saving since 1945, so a fixed
# offset is exact for every date this system handles and needs no tz database.
SUPPORTED_TIMEZONES: dict[str, tzinfo] = {"Asia/Kolkata": timezone(timedelta(hours=5, minutes=30), "IST")}


def parse_instant(text: object, path: str | None = None) -> datetime:
    if not isinstance(text, str) or not (m := _INSTANT_RE.match(text)):
        raise ContractError(ErrorCode.INVALID_INSTANT, f"{text!r} is not YYYY-MM-DDTHH:MM:SS.sssZ", path)
    y, mo, d, h, mi, s, ms = (int(g) for g in m.groups())
    try:
        return datetime(y, mo, d, h, mi, s, ms * 1000, tzinfo=UTC)
    except ValueError as e:
        raise ContractError(ErrorCode.INVALID_INSTANT, f"{text!r}: {e}", path) from None


def format_instant(dt: datetime) -> str:
    if dt.tzinfo is None:
        raise ContractError(ErrorCode.INVALID_INSTANT, "naive datetimes are not allowed")
    u = dt.astimezone(UTC)
    return u.strftime("%Y-%m-%dT%H:%M:%S.") + f"{u.microsecond // 1000:03d}Z"


def parse_business_date(text: object, path: str | None = None) -> date:
    if not isinstance(text, str) or not (m := _DATE_RE.match(text)):
        raise ContractError(ErrorCode.INVALID_DATE, f"{text!r} is not YYYY-MM-DD", path)
    try:
        return date(*(int(g) for g in m.groups()))
    except ValueError as e:
        raise ContractError(ErrorCode.INVALID_DATE, f"{text!r}: {e}", path) from None


def resolve_timezone(name: str) -> tzinfo:
    try:
        return SUPPORTED_TIMEZONES[name]
    except KeyError:
        raise ContractError(
            ErrorCode.VALIDATION_FAILED, f"time zone {name!r} is not supported in contract v1"
        ) from None


def business_date_of(instant: datetime, timezone_name: str) -> date:
    """The FPO-local business date of a UTC instant."""
    if instant.tzinfo is None:
        raise ContractError(ErrorCode.INVALID_INSTANT, "naive datetimes are not allowed")
    return instant.astimezone(resolve_timezone(timezone_name)).date()


def date_range(start: date, end: date) -> list[date]:
    """Inclusive at both ends."""
    if end < start:
        raise ContractError(ErrorCode.INVALID_DATE, f"range end {end} is before start {start}")
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]

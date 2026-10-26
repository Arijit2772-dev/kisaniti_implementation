"""Phone numbers, masking and exact message rendering (contract messaging/v1, L5_MESSAGING_CONTRACT_v1.md).

Pure: no I/O. Rendering inserts the stored fact strings unchanged (quantities keep their three decimals, names are not
re-scanned for placeholders), writes dates with month names and Western digits, and refuses an unknown or unfilled
placeholder: a message with a hole in it is an error, never something that is sent."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date

LANGUAGES = ("pa", "hi", "en")
_PLACEHOLDER = re.compile(r"\{(\w+)\}")
_E164 = re.compile(r"^\+[1-9][0-9]{7,14}$")
_INDIAN_MOBILE = re.compile(r"^[6-9][0-9]{9}$")
_QUANTITY = re.compile(r"^(0|[1-9][0-9]*)\.[0-9]{3}$")
BODY_PLACEHOLDERS = frozenset({"farmer", "variety", "lines"})
LINE_PLACEHOLDERS = frozenset({"quantity_kg", "mandi", "date"})
MONTHS: dict[str, tuple[str, ...]] = {
    "en": (
        "January", "February", "March", "April", "May", "June",
        "July", "August", "September", "October", "November", "December",
    ),
    "pa": ("ਜਨਵਰੀ", "ਫ਼ਰਵਰੀ", "ਮਾਰਚ", "ਅਪ੍ਰੈਲ", "ਮਈ", "ਜੂਨ", "ਜੁਲਾਈ", "ਅਗਸਤ", "ਸਤੰਬਰ", "ਅਕਤੂਬਰ", "ਨਵੰਬਰ", "ਦਸੰਬਰ"),
    "hi": ("जनवरी", "फ़रवरी", "मार्च", "अप्रैल", "मई", "जून", "जुलाई", "अगस्त", "सितंबर", "अक्टूबर", "नवंबर", "दिसंबर"),
}  # fmt: skip


class PhoneInvalid(ValueError):
    """The text is not a phone number this system accepts."""


class TemplateError(ValueError):
    """A template or its facts cannot produce a complete message."""


def normalise_phone(text: str) -> str:
    """E.164. A 10-digit Indian mobile number (first digit 6 to 9) becomes +91...; otherwise a leading + and 8 to 15
    digits are required. Spaces, hyphens and brackets are ignored; anything else is refused."""
    compact = re.sub(r"[ \-()]", "", text.strip())
    if _INDIAN_MOBILE.match(compact):
        return "+91" + compact
    if re.match(r"^0?[6-9][0-9]{9}$", compact) and len(compact) == 11:  # a leading trunk 0
        return "+91" + compact[1:]
    if _E164.match(compact):
        return compact
    raise PhoneInvalid("not a valid phone number")


def mask_phone(e164: str) -> str:
    """The first three characters, bullets, the last three digits: '+919000000210' -> '+91•••••••210'."""
    if not _E164.match(e164):
        raise PhoneInvalid("not a valid E.164 number")
    return e164[:3] + "•" * (len(e164) - 6) + e164[-3:]


def localized_date(day: date, language: str) -> str:
    """'20 October 2026' in the language's month name, Western digits."""
    if language not in MONTHS:
        raise TemplateError(f"no month names for {language!r}")
    return f"{day.day} {MONTHS[language][day.month - 1]} {day.year}"


@dataclass(frozen=True, slots=True)
class Line:
    quantity_kg: str
    mandi: str
    day: date


def _fill(text: str, values: Mapping[str, str], allowed: frozenset[str]) -> str:
    names = set(_PLACEHOLDER.findall(text))
    if names - allowed:
        raise TemplateError(f"unknown placeholder(s): {sorted(names - allowed)}")
    missing = names - set(values)
    if missing:
        raise TemplateError(f"unfilled placeholder(s): {sorted(missing)}")
    return _PLACEHOLDER.sub(lambda m: values[m.group(1)], text)


def check_template(body: str, line: str) -> None:
    """Raises TemplateError unless the body uses only farmer, variety, lines and the line only quantity_kg, mandi, date.
    The body must contain {lines}."""
    if "{lines}" not in body:
        raise TemplateError("the body has no {lines} placeholder")
    _fill(body, {k: "x" for k in BODY_PLACEHOLDERS}, BODY_PLACEHOLDERS)
    _fill(line, {k: "x" for k in LINE_PLACEHOLDERS}, LINE_PLACEHOLDERS)


def render(body: str, line: str, language: str, farmer: str, variety: str, lines: Sequence[Line]) -> str:
    """The exact message. Quantities must be 'N.NNN'; there must be at least one line."""
    if not lines:
        raise TemplateError("a message needs at least one line")
    rendered = []
    for ln in lines:
        if not _QUANTITY.match(ln.quantity_kg):
            raise TemplateError(f"quantity {ln.quantity_kg!r} is not an exact kilogram value")
        rendered.append(
            _fill(
                line,
                {"quantity_kg": ln.quantity_kg, "mandi": ln.mandi, "date": localized_date(ln.day, language)},
                LINE_PLACEHOLDERS,
            )
        )
    return _fill(body, {"farmer": farmer, "variety": variety, "lines": "\n".join(rendered)}, BODY_PLACEHOLDERS)

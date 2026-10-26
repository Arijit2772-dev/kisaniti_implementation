"""Phones, masking and rendering. Expected strings are written by hand, not produced by the code under test."""

from __future__ import annotations

from datetime import date

import pytest

from kisaniti_core.ids import new_local_id
from kisaniti_core.messaging import (
    Line,
    PhoneInvalid,
    TemplateError,
    check_template,
    localized_date,
    mask_phone,
    normalise_phone,
    render,
)


@pytest.mark.parametrize(
    ("typed", "expected"),
    [
        ("9000000001", "+919000000001"),
        ("90000 00001", "+919000000001"),
        ("090000-00001", "+919000000001"),
        ("(90000) 00001", "+919000000001"),
        ("+91 90000 00001", "+919000000001"),
        ("+44 7700 900123", "+447700900123"),
        ("  6123456789 ", "+916123456789"),
    ],
)
def test_phone_numbers_are_normalised_to_e164(typed: str, expected: str) -> None:
    assert normalise_phone(typed) == expected


@pytest.mark.parametrize(
    "typed",
    [
        "",
        "12345",
        "5000000001",
        "90000000",
        "+0123456789",
        "+91abc0000001",
        "9000000001x",
        "+9190000000012345678",
        "٩٠٠٠٠٠٠٠٠١",
    ],
)
def test_other_text_is_refused(typed: str) -> None:
    with pytest.raises(PhoneInvalid):
        normalise_phone(typed)


def test_masking_keeps_the_country_code_and_the_last_three_digits() -> None:
    assert mask_phone("+919000000210") == "+91•••••••210"
    assert mask_phone("+447700900123") == "+44•••••••123"
    assert len(mask_phone("+919000000210")) == len("+919000000210")
    with pytest.raises(PhoneInvalid):
        mask_phone("9000000210")


def test_dates_use_the_month_name_of_each_language_and_western_digits() -> None:
    d = date(2026, 10, 20)
    assert localized_date(d, "en") == "20 October 2026"
    assert localized_date(d, "pa") == "20 ਅਕਤੂਬਰ 2026"
    assert localized_date(d, "hi") == "20 अक्टूबर 2026"
    assert localized_date(date(2027, 1, 5), "en") == "5 January 2027"
    with pytest.raises(TemplateError):
        localized_date(d, "fr")


BODY = "Hello {farmer}, plan for your {variety}:\n{lines}\nCall the office."
LINE = "- {quantity_kg} kg at {mandi} on {date}"


def test_rendering_inserts_the_stored_strings_unchanged() -> None:
    lines = [Line("2000.000", "Mansa APMC", date(2026, 10, 20)), Line("400.050", "Dirba APMC", date(2026, 10, 26))]
    got = render(BODY, LINE, "en", "Gurpreet Singh", "1121", lines)
    assert got == (
        "Hello Gurpreet Singh, plan for your 1121:\n"
        "- 2000.000 kg at Mansa APMC on 20 October 2026\n"
        "- 400.050 kg at Dirba APMC on 26 October 2026\n"
        "Call the office."
    )


def test_a_value_that_looks_like_a_placeholder_is_not_expanded_again() -> None:
    got = render(BODY, LINE, "en", "{lines}", "{farmer}", [Line("1.000", "{date}", date(2026, 10, 20))])
    assert got.startswith("Hello {lines}, plan for your {farmer}:\n- 1.000 kg at {date} on 20 October 2026")


def test_unicode_names_and_months_survive() -> None:
    got = render(
        "{farmer} {variety}\n{lines}",
        "{quantity_kg} {mandi} {date}",
        "pa",
        "ਗੁਰਪ੍ਰੀਤ ਸਿੰਘ",
        "1121",
        [Line("5.000", "ਮਾਨਸਾ", date(2026, 11, 2))],
    )
    assert got == "ਗੁਰਪ੍ਰੀਤ ਸਿੰਘ 1121\n5.000 ਮਾਨਸਾ 2 ਨਵੰਬਰ 2026"


@pytest.mark.parametrize("qty", ["1250", "1250.00", "1250.0000", "-1.000", "1,250.000", "01.000", " 1.000"])
def test_a_quantity_that_is_not_exact_is_refused(qty: str) -> None:
    with pytest.raises(TemplateError):
        render(BODY, LINE, "en", "A", "1121", [Line(qty, "M", date(2026, 10, 20))])


def test_no_lines_and_bad_placeholders_are_errors_never_messages() -> None:
    with pytest.raises(TemplateError):
        render(BODY, LINE, "en", "A", "1121", [])
    with pytest.raises(TemplateError):
        render("{farmer} {price}\n{lines}", LINE, "en", "A", "1121", [Line("1.000", "M", date(2026, 10, 20))])
    with pytest.raises(TemplateError):
        render(BODY, "{quantity_kg} {transport}", "en", "A", "1121", [Line("1.000", "M", date(2026, 10, 20))])
    with pytest.raises(TemplateError):
        check_template("{farmer} no lines here", LINE)
    check_template(BODY, LINE)


def test_local_ids_have_the_ulid_shape_and_only_the_two_local_kinds() -> None:
    import re

    from kisaniti_core.errors import ContractError

    for kind in ("ins", "att"):
        assert re.match(rf"^{kind}_[0-9A-HJKMNP-TV-Z]{{26}}$", new_local_id(kind))
    assert new_local_id("ins") != new_local_id("ins")
    with pytest.raises(ContractError):
        new_local_id("mbr")

"""scripts/chainlib.py against the product's own functions: the chain's independent arithmetic must agree with the
product on many inputs, and must be able to tell right from wrong."""

from __future__ import annotations

import importlib.util
import random
import sys
from datetime import date
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

import pytest
from kisaniti_core.messaging import localized_date as core_date
from kisaniti_core.messaging import mask_phone as core_mask
from kisaniti_core.quantity import Quantity, ratio_display

SPEC = importlib.util.spec_from_file_location(
    "chainlib", Path(__file__).resolve().parents[2] / "scripts" / "chainlib.py"
)
assert SPEC is not None and SPEC.loader is not None
cl = importlib.util.module_from_spec(SPEC)
sys.modules["chainlib"] = cl
SPEC.loader.exec_module(cl)


def test_grams_and_kilograms_roundtrip_and_refuse_inexact_text() -> None:
    for g in (0, 1, 999, 1000, 1_250_000, 123_456_789):
        assert cl.grams(cl.kg(g)) == g == Quantity.parse(cl.kg(g)).units
    for bad in ("1250", "1250.00", "1250.0000", "-1.000", "1,250.000", "x.000"):
        with pytest.raises(ValueError):
            cl.grams(bad)


def test_rupees_roundtrip_and_signs() -> None:
    for p in (0, 1, 99, 100, -1, -250, 441_500, -12_345):
        assert cl.paise(cl.rupees(p)) == p
    assert cl.rupees(-5) == "-0.05" and cl.paise("-0.05") == -5


def test_sale_amounts_agree_with_the_product_on_random_inputs_including_ties() -> None:
    from kisaniti_api.sales.routes import _amount_paise

    rnd = random.Random(11)
    cases = [(5000, 10), (25_000, 10), (15_000, 10), (500_000, 440_000)] + [
        (rnd.randint(1, 5_000_000), rnd.randint(1, 900_000)) for _ in range(500)
    ]
    for g, p in cases:
        assert cl.sale_amount_paise(cl.kg(g), cl.rupees(p)) == _amount_paise(g, p), (g, p)
    assert (
        cl.sale_amount_paise("25.000", "0.10") == 2 and cl.sale_amount_paise("15.000", "0.10") == 2
    )  # ties go to even


def test_weighted_average_matches_the_hand_example() -> None:
    assert cl.rupees(cl.weighted_avg_price_paise([("500.000", "4400.00"), ("700.000", "4500.00")])) == "4458.33"


def test_line_net_matches_the_exact_rule() -> None:
    assert cl.line_net_paise("1200.000", "44.1500", "0.3500") == 1200 * 4380  # 1200 kg x 43.80
    assert (
        cl.line_net_paise("0.333", "0.0050", "0.0000") == 0 and cl.line_net_paise("1.000", "0.0150", "0.0000") == 2
    )  # 0.015 -> 0.02 (even)


def test_ratio_and_dates_and_masks_agree_with_the_product() -> None:
    rnd = random.Random(5)
    for _ in range(300):
        d = rnd.randint(1, 5_000_000)
        n = rnd.randint(0, d)
        assert cl.ratio6(cl.kg(n), cl.kg(d)) == ratio_display(Quantity(n), Quantity(d))
    for lang in ("en", "pa", "hi"):
        for m in range(1, 13):
            assert cl.localized_date(f"2026-{m:02d}-09", lang) == core_date(date(2026, m, 9), lang)
    for phone in ("+919000000001", "+447700900123", "+12025550123"):
        assert cl.mask_phone(phone) == core_mask(phone)


def event(i: int, kind: str, q: str, delta: bool = False) -> dict[str, str | int | None]:
    return {"id": i, "kind": kind, "quantity_kg": None if delta else q, "quantity_delta_kg": q if delta else None}


def test_ledger_replay_gives_the_balances_and_catches_every_kind_of_corruption() -> None:
    ok = [
        event(1, "REGISTERED", "1250.000"),
        event(2, "RESERVED", "1200.000"),
        event(3, "RESERVATION_CONSUMED", "500.000"),
        event(4, "SOLD", "500.000"),
        event(5, "ADJUSTED", "-10.000", True),
    ]
    b = cl.ledger_balances(ok)
    assert (b.on_hand_g, b.reserved_g, b.sold_g, b.available_g) == (740_000, 700_000, 500_000, 40_000)
    assert cl.ledger_balances(list(reversed(ok))) == b  # order of listing does not matter, the ids do
    bad = {
        "negative available": [*ok[:2], event(3, "SOLD", "100.000")],
        "negative reserved": [ok[0], event(2, "RESERVATION_RELEASED", "1.000")],
        "no registration": [event(1, "RESERVED", "1.000")],
        "registered twice": [ok[0], event(2, "REGISTERED", "5.000")],
        "unknown kind": [ok[0], event(2, "GIFTED", "1.000")],
        "oversold": [ok[0], event(2, "SOLD", "1250.001")],
    }
    for name, events in bad.items():
        with pytest.raises(AssertionError):
            cl.ledger_balances(events)
        assert name


def test_message_checks_accept_the_real_texts_and_reject_wrong_ones() -> None:
    from datetime import UTC, datetime

    from kisaniti_api.messaging.builder import ConsentFacts, ContactFacts, LineFacts, MemberFacts, build_instruction
    from kisaniti_api.messaging.templates import latest

    lines = [
        LineFacts("lot_x", "mnd_x", "Mansa APMC", date(2026, 10, 20), "2000.000"),
        LineFacts("lot_x", "mnd_x", "Mansa APMC", date(2026, 11, 2), "400.050"),
    ]
    for lang in ("en", "pa", "hi"):
        for kind in ("SALE_INSTRUCTION", "CANCELLATION"):
            doc = build_instruction(
                instruction_id="ins_x",
                plan_id="pln_x",
                revision_no=1,
                kind=kind,
                version=1,
                supersedes=None,
                member=MemberFacts("mbr_x", "Gurpreet", True, lang),
                contact=ContactFacts(1),
                consent=ConsentFacts("GRANTED", 1),
                variety="1121",
                lines=lines,
                template=latest(kind),
                prepared_by="usr_x",
                now=datetime(2026, 10, 26, tzinfo=UTC),
            )
            want = [("2000.000", "Mansa APMC", "2026-10-20"), ("400.050", "Mansa APMC", "2026-11-02")]
            assert cl.check_message(doc["text"], kind, lang, "Gurpreet", want) == [], (lang, kind)
            wrong = [("2000.001", "Mansa APMC", "2026-10-20"), ("400.050", "Mansa APMC", "2026-11-02")]
            assert any("exact amount" in p for p in cl.check_message(doc["text"], kind, lang, "Gurpreet", wrong))
            assert cl.check_message(doc["text"] + " ₹4400", kind, lang, "Gurpreet", want)
            assert cl.check_message(doc["text"], kind, lang, "Someone Else", want)
    assert Fraction(1, 2) == Fraction(1, 2) and Decimal(1) == 1

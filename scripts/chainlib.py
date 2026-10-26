"""Independent arithmetic and text checks for scripts/chain.py (slice 7). Standard library only.

Everything here is written from the contracts' rules, not imported from the product: if the product and these
functions disagree, one of them is wrong and the chain fails. backend/tests/test_chain_lib.py checks them against the
product's own functions on many inputs, so that they cannot drift into agreement by being the same code."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from fractions import Fraction

CENT = Decimal("0.01")
MONTHS = {
    "en": [
        "January", "February", "March", "April", "May", "June",
        "July", "August", "September", "October", "November", "December",
    ],
    "pa": ["ਜਨਵਰੀ", "ਫ਼ਰਵਰੀ", "ਮਾਰਚ", "ਅਪ੍ਰੈਲ", "ਮਈ", "ਜੂਨ", "ਜੁਲਾਈ", "ਅਗਸਤ", "ਸਤੰਬਰ", "ਅਕਤੂਬਰ", "ਨਵੰਬਰ", "ਦਸੰਬਰ"],
    "hi": ["जनवरी", "फ़रवरी", "मार्च", "अप्रैल", "मई", "जून", "जुलाई", "अगस्त", "सितंबर", "अक्टूबर", "नवंबर", "दिसंबर"],
}  # fmt: skip
SALE_OPENING = {
    "en": "Namaste {n}. Your FPO manager has approved this plan",
    "pa": "ਸਤਿ ਸ੍ਰੀ ਅਕਾਲ {n} ਜੀ। ਤੁਹਾਡੇ FPO ਮੈਨੇਜਰ ਨੇ",
    "hi": "नमस्ते {n} जी। आपके FPO मैनेजर ने",
}
CANCEL_OPENING = {
    "en": "Namaste {n}. The earlier plan",
    "pa": "ਸਤਿ ਸ੍ਰੀ ਅਕਾਲ {n} ਜੀ। ਤੁਹਾਡੇ",
    "hi": "नमस्ते {n} जी। आपके",
}
CANCEL_MARK = {"en": "has been cancelled", "pa": "ਰੱਦ ਹੋ ਗਈ ਹੈ", "hi": "रद्द हो गई है"}
UNIT = {"en": "kg", "pa": "ਕਿਲੋ", "hi": "किलो"}


def grams(kg: str) -> int:
    """'1250.000' -> 1250000; anything but exactly three decimals is an error."""
    whole, _, frac = kg.partition(".")
    if not whole.isdigit() or len(frac) != 3 or not frac.isdigit():
        raise ValueError(f"not an exact kilogram value: {kg!r}")
    return int(whole) * 1000 + int(frac)


def kg(g: int) -> str:
    return f"{g // 1000}.{g % 1000:03d}"


def paise(rupees: str) -> int:
    whole, _, frac = rupees.partition(".")
    if not whole.lstrip("-").isdigit() or len(frac) != 2 or not frac.isdigit():
        raise ValueError(f"not an exact rupee value: {rupees!r}")
    return int(whole) * 100 + (int(frac) if not whole.startswith("-") else -int(frac))


def rupees(p: int) -> str:
    sign = "-" if p < 0 else ""
    p = abs(p)
    return f"{sign}{p // 100}.{p % 100:02d}"


def half_even(x: Fraction) -> int:
    """Round a fraction to the nearest integer, ties to even."""
    return round(x)


def sale_amount_paise(quantity_kg: str, price_per_quintal: str) -> int:
    """kg x rupees per quintal / 100, to the paisa, ties to even."""
    return half_even(Fraction(grams(quantity_kg) * paise(price_per_quintal), 100_000))


def line_net_paise(quantity_kg: str, price_per_kg: str, cost_per_kg: str) -> int:
    """kg x (price - cost) per kg, with the rates at four decimals, to the paisa, ties to even."""
    q = Decimal(grams(quantity_kg)) / 1000
    net = (q * (Decimal(price_per_kg) - Decimal(cost_per_kg))).quantize(CENT, rounding=ROUND_HALF_EVEN)
    return int(net * 100)


def weighted_avg_price_paise(sales: list[tuple[str, str]]) -> int:
    """Quantity-weighted average price (paise per quintal): total of the per-sale amounts over the total quantity."""
    total_amount = sum(sale_amount_paise(q, p) for q, p in sales)
    total_g = sum(grams(q) for q, _ in sales)
    return half_even(Fraction(total_amount * 100_000, total_g))


def ratio6(numerator_kg: str, denominator_kg: str) -> str:
    with localcontext() as ctx:
        ctx.prec = 60
        return str(
            (Decimal(grams(numerator_kg)) / Decimal(grams(denominator_kg))).quantize(
                Decimal("0.000001"), rounding=ROUND_HALF_EVEN
            )
        )


def localized_date(iso: str, lang: str) -> str:
    y, m, d = (int(x) for x in iso.split("-"))
    return f"{d} {MONTHS[lang][m - 1]} {y}"


def mask_phone(e164: str) -> str:
    return e164[:3] + "•" * (len(e164) - 6) + e164[-3:]


@dataclass(frozen=True)
class LedgerBalance:
    on_hand_g: int
    reserved_g: int
    sold_g: int

    @property
    def available_g(self) -> int:
        return self.on_hand_g - self.reserved_g


def ledger_balances(events: list[dict[str, str | int | None]]) -> LedgerBalance:
    """Replay a lot's ledger events (as the API lists them) in order of their id and return the balances. Raises if
    on hand, reserved or available stock ever went below zero, or if the first event is not the registration."""
    on_hand = reserved = sold = 0
    for i, e in enumerate(sorted(events, key=lambda x: int(x["id"] or 0))):
        kind = str(e["kind"])
        if (i == 0) != (kind == "REGISTERED"):
            raise AssertionError(f"event {e['id']}: {kind} out of place (registration must be first and only once)")
        if kind == "ADJUSTED":
            on_hand += paise_free_signed(str(e["quantity_delta_kg"]))
        else:
            q = grams(str(e["quantity_kg"]))
            if kind == "REGISTERED":
                on_hand += q
            elif kind == "SOLD":
                on_hand -= q
                sold += q
            elif kind == "RESERVED":
                reserved += q
            elif kind in ("RESERVATION_RELEASED", "RESERVATION_CONSUMED"):
                reserved -= q
            else:
                raise AssertionError(f"event {e['id']}: unknown kind {kind}")
        if on_hand < 0 or reserved < 0 or on_hand - reserved < 0:
            raise AssertionError(f"event {e['id']} ({kind}) made on hand {kg(on_hand)}, reserved {kg(reserved)} kg")
    return LedgerBalance(on_hand, reserved, sold)


def paise_free_signed(delta_kg: str) -> int:
    """A signed kilogram delta ('-12.500') in grams."""
    return -grams(delta_kg[1:]) if delta_kg.startswith("-") else grams(delta_kg)


def check_message(text: str, kind: str, lang: str, name: str, lines: list[tuple[str, str, str]]) -> list[str]:
    """Problems with a farmer's message. lines: (quantity kg, mandi name, ISO date). Empty list: all as it should be."""
    bad = []
    opening = (SALE_OPENING if kind == "SALE_INSTRUCTION" else CANCEL_OPENING)[lang].format(n=name)
    if not text.startswith(opening):
        bad.append(f"does not start as the {lang} {kind} text should: {opening!r}")
    if kind == "CANCELLATION" and CANCEL_MARK[lang] not in text:
        bad.append(f"does not say that the plan is cancelled ({CANCEL_MARK[lang]!r})")
    for q, mandi, iso in lines:
        want = f"{q} {UNIT[lang]}"
        if want not in text:
            bad.append(f"missing the exact amount {want!r}")
        if mandi not in text:
            bad.append(f"missing the mandi {mandi!r}")
        if localized_date(iso, lang) not in text:
            bad.append(f"missing the date {localized_date(iso, lang)!r}")
    if text.count("•") != len(lines):
        bad.append(f"expected {len(lines)} lines, found {text.count('•')}")
    if "₹" in text or "price" in text.lower():
        bad.append("a price or a promise appears in the message")
    return bad

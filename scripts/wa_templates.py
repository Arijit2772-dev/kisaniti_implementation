#!/usr/bin/env python3
"""Prints the WhatsApp templates to create in WhatsApp Manager (messaging/v1 amendment A1.2, decision D1).

Each one is the product's own text, version 2, with {{1}}, {{2}}, {{3}} where the product fills in values. Paste the
body exactly; Meta approves it, and the product sends only when the filled-in text equals its prepared text. Also prints
the KN_WA_TEMPLATES line for the server. Usage: python3 scripts/wa_templates.py   (from the repository root)
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "backend" / "src"), str(ROOT / "core" / "src")]

from kisaniti_api.messaging import templates, whatsapp  # noqa: E402
from kisaniti_core.messaging import localized_date  # noqa: E402

NAMES = {"SALE_INSTRUCTION": "kisaniti_sale_v2", "CANCELLATION": "kisaniti_cancel_v2"}
SAMPLE = {"farmer": "Gurpreet Singh", "variety": "1121", "lines": None}
LANG_NAME = {"en": "English", "pa": "Punjabi", "hi": "Hindi"}


def main() -> None:
    mapping = {}
    for kind, name in NAMES.items():
        t = templates.get(kind, 2)
        assert t is not None, f"{kind} version 2 is missing"
        for lang in ("en", "pa", "hi"):
            v = t.variants[lang]
            form, names = whatsapp.meta_form(v.body)
            first = v.line.format(
                quantity_kg="1250.000", mandi="Mansa APMC", date=localized_date(date(2026, 12, 1), lang)
            )
            second = v.line.format(
                quantity_kg="400.050", mandi="Bathinda APMC", date=localized_date(date(2026, 12, 3), lang)
            )
            samples = [SAMPLE[n] or f"{first}{t.joiner}{second}" for n in names]
            print("=" * 72)
            print(f"Name: {name}    Language: {LANG_NAME[lang]} ({lang})    Category: Utility")
            print(f"Review status in the product: {v.review_status}")
            print("Body (copy exactly, including the line breaks):\n")
            print(form)
            print("\nSample values:")
            for i, s in enumerate(samples, 1):
                print(f"  {{{{{i}}}}} = {s}")
            mapping[whatsapp.template_key(kind, 2, lang)] = f"{name}:{lang}"
    print("=" * 72)
    print("Server setting, once Meta has approved all six:")
    print("KN_WA_TEMPLATES=" + json.dumps(mapping, separators=(",", ":")))


if __name__ == "__main__":
    main()

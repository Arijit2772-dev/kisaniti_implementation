# ruff: noqa: F811, F401  (fixtures imported from test_access by name: pytest uses them as arguments)
"""The shipped Punjabi and Hindi texts are REVIEWED by the authors (28 Oct 2026): with only such texts in a hand-over
the manager is not asked to acknowledge an unreviewed translation. The unreviewed path is in test_handover.py."""

from __future__ import annotations

from typing import Any

from tests.test_access import Fpo, access_env, add_user, clean_up_access_data, two
from tests.test_handover import handover, preview, three_farmers
from tests.test_plan import fresh_market, market, valid


def test_reviewed_texts_need_no_acknowledgement(two: tuple[Fpo, Fpo], market: dict[str, Any]) -> None:
    a, _ = two
    plan, mids, _docs = three_farmers(a, market)  # pa and hi have contact and consent, en has not
    pv = preview(a.admin, plan["id"]).json()
    valid("handover", "preview", pv)
    assert pv["recipient_count"] == 2 and pv["unreviewed_count"] == 0
    assert [(x["member_id"], x["language"], x["review_status"]) for x in pv["ready"]] == [
        (mids[0], "pa", "REVIEWED"),
        (mids[1], "hi", "REVIEWED"),
    ]
    h = handover(a.admin, plan["id"], 2, ack=False)
    assert h.status_code == 201, h.text
    assert h.json()["acknowledged_unreviewed"] is False
    assert {i["review_status"] for i in h.json()["items"] if i["status"] == "ISSUED"} == {"REVIEWED"}

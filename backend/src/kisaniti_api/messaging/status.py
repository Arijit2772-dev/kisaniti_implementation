"""Effective delivery status (messaging/v1 §5, amendment A1.1): an attempt's own status raised by provider events.

QUEUED < SENT < DELIVERED < READ. FAILED applies only while the status is below DELIVERED. Repeated or out-of-order
events never lower it. Pure functions; the webhook and every reader use the same rule.
"""

from __future__ import annotations

from collections.abc import Iterable

ORDER = {"QUEUED": 0, "SENT": 1, "DELIVERED": 2, "READ": 3}
PROVIDER = {"sent": "SENT", "delivered": "DELIVERED", "read": "READ", "failed": "FAILED"}


def apply(current: str, event: str) -> str:
    """The status after one event. Statuses outside the provider's lifecycle (PREVIEWED, BLOCKED) never change."""
    if current not in ORDER and current != "FAILED":
        return current
    if event == "FAILED":
        return "FAILED" if current == "FAILED" or ORDER[current] < ORDER["DELIVERED"] else current
    if current == "FAILED":
        # a later provider report that the message did arrive outranks a failure reported before it
        return event if ORDER[event] >= ORDER["DELIVERED"] else current
    return event if ORDER[event] > ORDER[current] else current


def effective(own: str, events: Iterable[str]) -> str:
    status = own
    for e in events:
        status = apply(status, e)
    return status

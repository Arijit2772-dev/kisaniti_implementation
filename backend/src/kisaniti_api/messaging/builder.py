"""Builds one InstructionV1 from already-loaded facts (messaging/v1). No database, no clock, no network."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from kisaniti_core.messaging import Line, render
from kisaniti_core.timeutil import format_instant

from .templates import Template


@dataclass(frozen=True, slots=True)
class MemberFacts:
    id: str
    name: str
    active: bool
    language: str


@dataclass(frozen=True, slots=True)
class ContactFacts:
    version: int


@dataclass(frozen=True, slots=True)
class ConsentFacts:
    state: str  # GRANTED | WITHDRAWN | NONE
    event_no: int | None


@dataclass(frozen=True, slots=True)
class LineFacts:
    lot_id: str
    mandi_id: str
    mandi_name: str
    day: date
    quantity_kg: str


def eligibility(
    member: MemberFacts, contact: ContactFacts | None, consent: ConsentFacts, template_ok: bool
) -> list[str]:
    """The communication blockers, in a fixed order. Empty means the member can be messaged."""
    issues = []
    if not member.active:
        issues.append("MEMBER_INACTIVE")
    if contact is None:
        issues.append("NO_CONTACT")
    if consent.state == "NONE":
        issues.append("NO_WHATSAPP_CONSENT")
    elif consent.state == "WITHDRAWN":
        issues.append("CONSENT_WITHDRAWN")
    if not template_ok:
        issues.append("TEMPLATE_UNAVAILABLE")
    return issues


def build_instruction(
    *,
    instruction_id: str,
    plan_id: str,
    revision_no: int,
    kind: str,
    version: int,
    supersedes: str | None,
    member: MemberFacts,
    contact: ContactFacts | None,
    consent: ConsentFacts,
    variety: str,
    lines: list[LineFacts],
    template: Template | None,
    prepared_by: str,
    now: datetime,
) -> dict[str, Any]:
    """InstructionV1 without its `delivery` part (the routes add it from the attempts)."""
    variant = None if template is None else template.variants.get(member.language)
    issues = eligibility(member, contact, consent, variant is not None)
    text: str | None = None
    if not issues:
        assert variant is not None
        text = render(
            variant.body,
            variant.line,
            member.language,
            member.name,
            variety,
            [Line(ln.quantity_kg, ln.mandi_name, ln.day) for ln in lines],
        )
    ok = not issues
    return {
        "contract": "messaging.instruction/v1",
        "id": instruction_id,
        "plan_id": plan_id,
        "revision_no": revision_no,
        "member_id": member.id,
        "kind": kind,
        "version": version,
        "status": "PREPARED" if ok else "BLOCKED",
        "issues": issues,
        "language": member.language if ok else None,
        "template": (
            {
                "id": template.id,
                "version": template.version,
                "review_status": variant.review_status,
                "text_sha256": variant.text_sha256,
            }
            if ok and template is not None and variant is not None
            else None
        ),
        "text": text,
        "facts": {
            "variety": variety,
            "lines": [
                {
                    "lot_id": ln.lot_id,
                    "mandi_id": ln.mandi_id,
                    "mandi_name": ln.mandi_name,
                    "date": ln.day.isoformat(),
                    "quantity_kg": ln.quantity_kg,
                }
                for ln in lines
            ],
        },
        "contact_version": contact.version if ok and contact is not None else None,
        "consent_event_no": consent.event_no if ok else None,
        "supersedes": supersedes,
        "prepared_by": prepared_by,
        "prepared_at": format_instant(now),
    }

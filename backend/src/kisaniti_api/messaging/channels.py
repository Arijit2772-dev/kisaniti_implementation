"""The one interface through which a message leaves the system (hosting plan §4). Only the preview channel exists in
slice 4: it renders who would receive what and sends nothing. The real WhatsApp channel (S4.5) will implement the same
``MessageChannel`` and must refuse a variant that is not reviewed."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from kisaniti_core.messaging import mask_phone

PREVIEW_LABEL = "PREVIEW — nothing was sent"


@dataclass(frozen=True, slots=True)
class ChannelResult:
    status: str
    provider_message_id: str | None
    simulated: bool
    preview: dict[str, Any] | None


class MessageChannel(Protocol):
    name: str

    def deliver(self, *, text: str, language: str, recipient_phone: str) -> ChannelResult: ...


class PreviewChannel:
    name = "PREVIEW"

    def deliver(self, *, text: str, language: str, recipient_phone: str) -> ChannelResult:
        """No network, no provider. The recipient is always shown masked, even to a caller who may see numbers."""
        return ChannelResult(
            "PREVIEWED",
            None,
            True,
            {"recipient": mask_phone(recipient_phone), "language": language, "text": text, "label": PREVIEW_LABEL},
        )


CHANNELS: dict[str, MessageChannel] = {"PREVIEW": PreviewChannel()}

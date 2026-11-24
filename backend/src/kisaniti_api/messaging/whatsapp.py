"""WhatsApp Cloud API sending (messaging/v1 amendment A1.2, decisions D1 and D2).

D1: only Meta-approved templates are sent. The product's own template version is turned into Meta's form by putting
{{1}}, {{2}}, {{3}} in place of its placeholders, in the order they appear. The owner creates that exact text in
WhatsApp Manager (``scripts/wa_templates.py`` prints it). Before every send, the prepared message is split back into
its values and refilled; the result must equal the prepared text character for character, or nothing is sent.

D2: Meta's template names and language codes are not invented here. They come from the server's environment:
- ``KN_WA_TOKEN``: the access token (put on the server by the owner, never in git or chat);
- ``KN_WA_PHONE_NUMBER_ID``: the sending number's ID from the Meta dashboard;
- ``KN_WA_API_VERSION``: the Graph API version shown in the dashboard, for example ``v23.0``;
- ``KN_WA_TEMPLATES``: JSON ``{"SALE_INSTRUCTION.v2.pa": "kisaniti_sale_v2:pa", ...}``, mapping the product's template,
  version and language to the Meta template name and language code.
Without the first three, the channel is not available. Without a mapping for a variant, that variant cannot be sent.

The recipient's number is passed to Meta only and is never logged.
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass

from .templates import Template

log = logging.getLogger("kisaniti_api.whatsapp")
GRAPH = "https://graph.facebook.com"
TIMEOUT_S = 10
_PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")
_API_VERSION = re.compile(r"^v\d{1,3}\.\d{1,2}$")
_BAD_VALUE = re.compile(r"[\n\t]| {4}")  # Meta refuses these inside a template value


@dataclass(frozen=True, slots=True)
class Settings:
    token: str
    phone_number_id: str
    api_version: str
    templates: dict[str, tuple[str, str]]  # "SALE_INSTRUCTION.v2.pa" -> (meta name, meta language code)


@dataclass(frozen=True, slots=True)
class Outcome:
    """QUEUED with Meta's message ID, or FAILED with a cause and Meta's error codes."""

    status: str
    provider_message_id: str | None
    failure: str | None
    error_codes: tuple[int, ...]


def settings() -> Settings | None:
    """None when the channel is not configured on this server (or configured wrongly, which is logged)."""
    token = os.environ.get("KN_WA_TOKEN", "")
    phone = os.environ.get("KN_WA_PHONE_NUMBER_ID", "")
    version = os.environ.get("KN_WA_API_VERSION", "")
    if not (token and phone and version):
        return None
    if not phone.isdigit() or not _API_VERSION.match(version):
        log.error("WhatsApp settings refused: KN_WA_PHONE_NUMBER_ID must be digits, KN_WA_API_VERSION like v23.0")
        return None
    try:
        raw = json.loads(os.environ.get("KN_WA_TEMPLATES") or "{}")
        mapping = {}
        for key, value in raw.items():
            name, lang = str(value).split(":")
            if not re.fullmatch(r"[a-z0-9_]{1,512}", name) or not re.fullmatch(r"[a-z]{2,3}(_[A-Z]{2})?", lang):
                raise ValueError(key)
            mapping[str(key)] = (name, lang)
    except (ValueError, AttributeError):
        log.error('WhatsApp settings refused: KN_WA_TEMPLATES must be JSON like {"SALE_INSTRUCTION.v2.pa": "name:pa"}')
        return None
    return Settings(token, phone, version, mapping)


def template_key(template_id: str, version: int, language: str) -> str:
    return f"{template_id}.v{version}.{language}"


def meta_form(body: str) -> tuple[str, list[str]]:
    """The body as Meta stores it, and the product placeholders behind {{1}}, {{2}}, ... in order."""
    names: list[str] = []

    def number(m: re.Match[str]) -> str:
        names.append(m.group(1))
        return "{{" + str(len(names)) + "}}"

    return _PLACEHOLDER.sub(number, body), names


def parameters(body: str, prepared: str) -> list[str] | None:
    """The template values that turn ``body`` into exactly ``prepared``, or None when that is impossible or a value
    contains something Meta does not accept."""
    parts = _PLACEHOLDER.split(body)  # literal, name, literal, name, ..., literal
    pattern = "".join(re.escape(p) if i % 2 == 0 else "(.+?)" for i, p in enumerate(parts))
    m = re.fullmatch(pattern, prepared, re.DOTALL)
    if m is None:
        return None
    values = list(m.groups())
    if any(not v.strip() or _BAD_VALUE.search(v) or len(v) > 1024 for v in values):
        return None
    form, _ = meta_form(body)
    refilled = form
    for i, v in enumerate(values, 1):
        refilled = refilled.replace("{{" + str(i) + "}}", v, 1)
    return values if refilled == prepared else None


def transport(url: str, headers: dict[str, str], data: bytes) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:  # fixed https URL
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def send(s: Settings, *, to_e164: str, meta_name: str, meta_lang: str, values: list[str]) -> Outcome:
    """One template message. 2xx with a message ID: QUEUED. 4xx: PROVIDER_REFUSED with Meta's codes. Anything else
    (5xx, timeout, no connection, an unreadable answer): PROVIDER_OUTCOME_UNKNOWN, because it may have been sent."""
    url = f"{GRAPH}/{s.api_version}/{s.phone_number_id}/messages"
    body = {
        "messaging_product": "whatsapp",
        "to": to_e164.lstrip("+"),
        "type": "template",
        "template": {
            "name": meta_name,
            "language": {"code": meta_lang},
            "components": [{"type": "body", "parameters": [{"type": "text", "text": v} for v in values]}],
        },
    }
    headers = {"Authorization": f"Bearer {s.token}", "Content-Type": "application/json"}
    try:
        code, raw = transport(url, headers, json.dumps(body, ensure_ascii=False).encode())
    except Exception as e:  # any transport failure: an unknown outcome, logged by type only
        log.warning(f"WhatsApp send outcome unknown ({type(e).__name__})")
        return Outcome("FAILED", None, "PROVIDER_OUTCOME_UNKNOWN", ())
    try:
        doc = json.loads(raw or b"{}")
    except ValueError:
        doc = {}
    if 200 <= code < 300:
        try:
            mid = str(doc["messages"][0]["id"])
        except (KeyError, IndexError, TypeError):
            return Outcome("FAILED", None, "PROVIDER_OUTCOME_UNKNOWN", ())
        if not 1 <= len(mid) <= 200:
            return Outcome("FAILED", None, "PROVIDER_OUTCOME_UNKNOWN", ())
        return Outcome("QUEUED", mid, None, ())
    if 400 <= code < 500:
        err = doc.get("error") if isinstance(doc, dict) else None
        codes = []
        if isinstance(err, dict) and isinstance(err.get("code"), int):
            codes.append(err["code"])
        return Outcome("FAILED", None, "PROVIDER_REFUSED", tuple(codes))
    return Outcome("FAILED", None, "PROVIDER_OUTCOME_UNKNOWN", ())


def prepare_send(template: Template | None, language: str, prepared: str) -> list[str] | None:
    """The values for this prepared message, or None if its template variant cannot be sent through Meta."""
    variant = None if template is None else template.variants.get(language)
    return None if variant is None else parameters(variant.body, prepared)

"""Versioned message templates (messaging/v1): files in ``templates/``, three languages each, a review status per
language. A released version is never edited; a change is a new file with a higher version."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from kisaniti_core.messaging import LANGUAGES, check_template

DIRECTORY = Path(__file__).resolve().parent / "templates"


@dataclass(frozen=True, slots=True)
class Variant:
    language: str
    body: str
    line: str
    review_status: str
    reviewer: str | None
    reviewed_at: str | None

    @property
    def text_sha256(self) -> str:
        """Fingerprint of the exact words (body and line pattern) a farmer's message was built from."""
        return hashlib.sha256((self.body + "\n" + self.line).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class Template:
    id: str
    version: int
    variants: dict[str, Variant]


@lru_cache(maxsize=1)
def _all() -> dict[tuple[str, int], Template]:
    out: dict[tuple[str, int], Template] = {}
    for f in sorted(DIRECTORY.glob("*.json")):
        raw = json.loads(f.read_text(encoding="utf-8"))
        variants = {}
        if sorted(raw["variants"]) != sorted(LANGUAGES):
            raise ValueError(f"{f.name}: a template needs exactly the languages {LANGUAGES}")
        for lang, v in raw["variants"].items():
            check_template(v["body"], v["line"])
            r = v["review"]
            if r["status"] not in ("UNREVIEWED", "REVIEWED") or (r["status"] == "REVIEWED") != bool(
                r["reviewer"] and r["reviewed_at"]
            ):
                raise ValueError(
                    f"{f.name}/{lang}: a REVIEWED variant needs a reviewer and a date, an UNREVIEWED one neither"
                )
            variants[lang] = Variant(lang, v["body"], v["line"], r["status"], r["reviewer"], r["reviewed_at"])
        key = (raw["id"], int(raw["version"]))
        if key in out:
            raise ValueError(f"{f.name}: duplicate template {key}")
        out[key] = Template(raw["id"], int(raw["version"]), variants)
    return out


def all_templates() -> list[Template]:
    return [t for _, t in sorted(_all().items())]


def latest(template_id: str) -> Template | None:
    versions = [t for (i, _), t in _all().items() if i == template_id]
    return max(versions, key=lambda t: t.version, default=None)

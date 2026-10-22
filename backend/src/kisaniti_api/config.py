"""Settings from environment variables only (secrets never live in code)."""

from __future__ import annotations

import os
from dataclasses import dataclass

from kisaniti_core.provenance import Mode


@dataclass(frozen=True, slots=True)
class Settings:
    database_url: str
    mode: Mode


def load_settings() -> Settings:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not set (see .env.example)")
    return Settings(database_url=url, mode=Mode(os.environ.get("KISANITI_MODE", "DEMO")))

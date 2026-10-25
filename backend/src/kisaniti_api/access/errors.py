"""Access errors. Codes come from the shared contract; the one addition is TOO_MANY_ATTEMPTS (access/v1)."""

from __future__ import annotations

from dataclasses import dataclass, field

from kisaniti_core.errors import ErrorCode


@dataclass
class AccessError(Exception):
    code: ErrorCode
    detail: str
    issue_code: str | None = None  # a finer code inside `issues`, e.g. CSRF_INVALID, PASSWORD_CHANGE_REQUIRED
    issue_path: str | None = None
    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class Throttled(Exception):
    retry_after_s: int

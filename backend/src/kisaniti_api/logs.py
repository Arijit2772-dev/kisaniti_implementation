"""Structured logs: one JSON object per line on stdout, with the request ID when there is one.

Build identity also lives here: the commit the image was built from (KN_BUILD_COMMIT, set by CI as a Docker build
argument), or "unknown" for local builds. Nothing else about the request is logged: no body, no query string, no
cookie, so no personal data or secret reaches the logs.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from datetime import UTC, datetime

_COMMIT = re.compile(r"^[0-9a-f]{40}$")
REQUEST_ID = re.compile(r"^[A-Za-z0-9-]{8,64}$")


def build_commit() -> str:
    """The full commit hash of this build, or "unknown" when it was not set or is not a commit hash."""
    value = os.environ.get("KN_BUILD_COMMIT", "")
    return value if _COMMIT.match(value) else "unknown"


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key in ("request_id", "method", "path", "status", "duration_ms"):
            if hasattr(record, key):
                out[key] = getattr(record, key)
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, ensure_ascii=False)


def configure() -> None:
    """Send the application's own loggers to stdout as JSON (idempotent)."""
    log = logging.getLogger("kisaniti_api")
    if any(isinstance(h.formatter, JsonFormatter) for h in log.handlers):
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    log.propagate = False

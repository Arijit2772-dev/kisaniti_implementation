"""GET /health/live and GET /health/ready (readiness/v1 body)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from kisaniti_core.timeutil import format_instant
from sqlalchemy.exc import SQLAlchemyError

from .config import load_settings
from .db import current_revision, database_answers, get_engine, head_revision

router = APIRouter()


@router.get("/health/live")
def live() -> dict[str, str]:
    return {"status": "UP"}


@router.get("/health/ready")
def ready() -> JSONResponse:
    settings = load_settings()
    components = {"database": "BLOCKED", "migrations": "BLOCKED"}
    issues: list[dict[str, Any]] = []
    try:
        engine = get_engine(settings.database_url)
        if database_answers(engine):
            components["database"] = "READY"
        have, want = current_revision(engine), head_revision(settings.database_url)
        if have == want:
            components["migrations"] = "READY"
        else:
            issues.append(
                {
                    "code": "NOT_READY",
                    "path": "/components/migrations",
                    "severity": "ERROR",
                    "message": f"database schema revision {have} is not the code's head {want}",
                }
            )
    except SQLAlchemyError as e:
        issues.append(
            {
                "code": "DEPENDENCY_UNAVAILABLE",
                "path": "/components/database",
                "severity": "ERROR",
                "message": f"database did not answer ({type(e).__name__})",
            }
        )
    status = "READY" if all(v == "READY" for v in components.values()) else "BLOCKED"
    body = {
        "contract": "readiness/v1",
        "purpose": "API_SERVE",
        "status": status,
        "components": components,
        "issues": issues,
        "policy_version": "health/v1",
        "evaluated_at": format_instant(datetime.now(UTC)),
    }
    return JSONResponse(body, status_code=200 if status == "READY" else 503)

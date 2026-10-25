"""Database operations of the access slice. Two kinds of connection (L0_ACCESS §4):

* the **owner connection** (`auth_conn`) for the narrow pre-principal path: sign-in, session lookup, own-account
  changes;
* the **tenant connection** (`tenant_conn`): `SET LOCAL ROLE kisaniti_app` plus `app.fpo_id`, so row-level security
  applies. Every route that serves FPO data must use this one.
"""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from kisaniti_core.timeutil import format_instant
from sqlalchemy import Connection, Engine, text

from .config import AccessSettings

BASE_CAPABILITIES: dict[str, frozenset[str]] = {
    "FPO_ADMIN": frozenset(
        {"users.manage", "plans.approve", "data.read", "data.write", "contacts.view_phone", "messages.send"}
    ),
    "STAFF": frozenset({"data.read", "data.write"}),
    "VIEWER": frozenset({"data.read"}),
    "PLATFORM_OPERATOR": frozenset({"platform.admin"}),
}
USER_COLUMNS = (
    "id, fpo_id, username, display_name, role, extra_capabilities, language, active, must_change_password, "
    "origin, created_at"
)


@dataclass(frozen=True, slots=True)
class Principal:
    user_id: str
    fpo_id: str | None
    role: str
    capabilities: frozenset[str]
    language: str
    must_change_password: bool
    csrf_token: str
    session_expires_at: datetime
    session_hash: str
    request_id: str


def capabilities_for(role: str, extra: list[str] | None) -> frozenset[str]:
    return BASE_CAPABILITIES[role] | frozenset(extra or [])


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@contextmanager
def auth_conn(engine: Engine) -> Iterator[Connection]:
    with engine.begin() as conn:
        yield conn


@contextmanager
def tenant_conn(engine: Engine, fpo_id: str) -> Iterator[Connection]:
    """A transaction restricted to one FPO by the database itself."""
    with engine.begin() as conn:
        conn.execute(text("SET LOCAL ROLE kisaniti_app"))
        conn.execute(text("SELECT set_config('app.fpo_id', :f, true)"), {"f": fpo_id})
        yield conn


def user_json(row: Any) -> dict[str, Any]:
    """UserV1 (contract access/v1). Never includes the password hash."""
    return {
        "contract": "access.user/v1",
        "id": row.id,
        "fpo_id": row.fpo_id,
        "username": row.username,
        "display_name": row.display_name,
        "role": row.role,
        "extra_capabilities": sorted(row.extra_capabilities or []),
        "language": row.language,
        "active": row.active,
        "must_change_password": row.must_change_password,
        "origin": row.origin,
        "created_at": format_instant(row.created_at),
    }


def principal_json(p: Principal) -> dict[str, Any]:
    return {
        "contract": "access.principal/v1",
        "user_id": p.user_id,
        "fpo_id": p.fpo_id,
        "role": p.role,
        "capabilities": sorted(p.capabilities),
        "language": p.language,
        "must_change_password": p.must_change_password,
        "csrf_token": p.csrf_token,
        "session_expires_at": format_instant(p.session_expires_at),
    }


# ---- throttling ------------------------------------------------------------------------------------------------
def throttle_key(username: str) -> str:
    return sha256_hex(username.strip().lower())


def blocked_seconds(conn: Connection, key: str) -> int:
    row = conn.execute(
        text(
            "SELECT ceil(extract(epoch FROM blocked_until - now()))::int AS s FROM login_attempts "
            "WHERE key_sha256 = :k AND blocked_until > now()"
        ),
        {"k": key},
    ).first()
    return int(row.s) if row else 0


def register_failure(conn: Connection, key: str, s: AccessSettings) -> None:
    n = conn.execute(
        text(
            """
            INSERT INTO login_attempts (key_sha256, window_start, failures) VALUES (:k, now(), 1)
            ON CONFLICT (key_sha256) DO UPDATE SET
              failures = CASE WHEN login_attempts.window_start < now() - make_interval(mins => :win)
                              THEN 1 ELSE login_attempts.failures + 1 END,
              window_start = CASE WHEN login_attempts.window_start < now() - make_interval(mins => :win)
                                  THEN now() ELSE login_attempts.window_start END
            RETURNING failures
            """
        ),
        {"k": key, "win": s.throttle_window_minutes},
    ).scalar_one()
    if n >= s.throttle_failures:
        conn.execute(
            text("UPDATE login_attempts SET blocked_until = now() + make_interval(mins => :b) WHERE key_sha256 = :k"),
            {"k": key, "b": s.throttle_block_minutes},
        )


def clear_failures(conn: Connection, key: str) -> None:
    conn.execute(text("DELETE FROM login_attempts WHERE key_sha256 = :k"), {"k": key})


# ---- sessions --------------------------------------------------------------------------------------------------
def create_session(conn: Connection, user_id: str, s: AccessSettings) -> tuple[str, str, datetime]:
    token = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(32)
    expires = conn.execute(
        text(
            "INSERT INTO sessions (token_sha256, user_id, csrf_token, expires_at) "
            "VALUES (:h, :u, :c, now() + make_interval(mins => :abs)) RETURNING expires_at"
        ),
        {"h": sha256_hex(token), "u": user_id, "c": csrf, "abs": s.absolute_minutes},
    ).scalar_one()
    return token, csrf, expires


def load_principal(conn: Connection, token: str, s: AccessSettings, request_id: str) -> Principal | None:
    h = sha256_hex(token)
    row = conn.execute(
        text(
            """
            SELECT s.csrf_token, s.expires_at, u.id, u.fpo_id, u.role, u.extra_capabilities, u.language,
                   u.must_change_password
            FROM sessions s JOIN users u ON u.id = s.user_id
            WHERE s.token_sha256 = :h AND s.revoked_at IS NULL AND s.expires_at > now()
              AND s.last_seen_at > now() - make_interval(mins => :idle) AND u.active
            """
        ),
        {"h": h, "idle": s.idle_minutes},
    ).first()
    if row is None:
        return None
    conn.execute(text("UPDATE sessions SET last_seen_at = now() WHERE token_sha256 = :h"), {"h": h})
    return Principal(
        user_id=row.id,
        fpo_id=row.fpo_id,
        role=row.role,
        capabilities=capabilities_for(row.role, row.extra_capabilities),
        language=row.language,
        must_change_password=row.must_change_password,
        csrf_token=row.csrf_token,
        session_expires_at=row.expires_at,
        session_hash=h,
        request_id=request_id,
    )


def revoke_session(conn: Connection, session_hash: str) -> None:
    conn.execute(
        text("UPDATE sessions SET revoked_at = now() WHERE token_sha256 = :h AND revoked_at IS NULL"),
        {"h": session_hash},
    )


def revoke_user_sessions(conn: Connection, user_id: str, keep_hash: str | None = None) -> None:
    conn.execute(
        text(
            "UPDATE sessions SET revoked_at = now() WHERE user_id = :u AND revoked_at IS NULL "
            "AND (CAST(:keep AS text) IS NULL OR token_sha256 <> :keep)"
        ),
        {"u": user_id, "keep": keep_hash},
    )


# ---- audit -----------------------------------------------------------------------------------------------------
def audit(
    conn: Connection,
    request_id: str,
    event: str,
    actor: str | None = None,
    fpo_id: str | None = None,
    subject: str | None = None,
) -> None:
    """Append one audit row. Never pass secrets, phone numbers or user-supplied free text."""
    conn.execute(
        text(
            "INSERT INTO audit_log (request_id, event, actor_user_id, fpo_id, subject_user_id) "
            "VALUES (:r, :e, :a, :f, :s)"
        ),
        {"r": request_id, "e": event, "a": actor, "f": fpo_id, "s": subject},
    )

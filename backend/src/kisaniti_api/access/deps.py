"""FastAPI dependencies: the principal (from the session cookie only), CSRF, capabilities, and the tenant connection."""

from __future__ import annotations

import hmac
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Annotated

from fastapi import Depends, Request
from kisaniti_core.errors import ErrorCode
from sqlalchemy import Connection, Engine

from ..config import load_settings
from ..db import get_engine
from .config import load_access_settings
from .errors import AccessError
from .service import Principal, audit, auth_conn, load_principal, tenant_conn

COOKIE = "kn_session"
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def engine() -> Engine:
    return get_engine(load_settings().database_url)


def deny(request: Request, p: Principal | None, issue_code: str, detail: str) -> AccessError:
    """Record ACCESS_DENIED (in its own transaction, so it survives the failed request) and build the 403."""
    with auth_conn(engine()) as conn:
        audit(conn, request.state.request_id, "ACCESS_DENIED", p.user_id if p else None, p.fpo_id if p else None)
    return AccessError(ErrorCode.FORBIDDEN, detail, issue_code)


def current_principal(request: Request) -> Principal:
    """The acting user, built from the session. Nothing in the request body, path or query can change it."""
    token = request.cookies.get(COOKIE)
    if not token:
        raise AccessError(ErrorCode.UNAUTHENTICATED, "sign in required")
    s = load_access_settings()
    with auth_conn(engine()) as conn:
        p = load_principal(conn, token, s, request.state.request_id)
    if p is None:
        raise AccessError(ErrorCode.UNAUTHENTICATED, "the session is not valid; sign in again")
    request.state.principal = p
    if request.method in UNSAFE_METHODS:
        origin = request.headers.get("origin")
        if origin is not None and origin.rstrip("/") not in s.allowed_origins:
            raise deny(request, p, "CSRF_INVALID", "request origin is not allowed")
        if not hmac.compare_digest(request.headers.get("x-csrf-token", ""), p.csrf_token):
            raise deny(request, p, "CSRF_INVALID", "missing or wrong CSRF token")
    return p


CurrentPrincipal = Annotated[Principal, Depends(current_principal)]


def active_principal(request: Request, p: CurrentPrincipal) -> Principal:
    """Like current_principal, but refuses while a password change is pending."""
    if p.must_change_password:
        raise deny(request, p, "PASSWORD_CHANGE_REQUIRED", "change your password before continuing")
    return p


ActivePrincipal = Annotated[Principal, Depends(active_principal)]


def require(capability: str) -> Callable[..., Principal]:
    def check(request: Request, p: ActivePrincipal) -> Principal:
        if capability not in p.capabilities:
            raise deny(request, p, "CAPABILITY_MISSING", f"requires {capability}")
        return p

    return check


@contextmanager
def tenant_scope(request: Request, p: Principal) -> Iterator[Connection]:
    """The only database connection a tenant-scoped route may use: restricted to the principal's FPO by PostgreSQL.

    Used as `with tenant_scope(request, p) as conn:` inside the endpoint, so the transaction is committed (or rolled
    back) before the response is built, never after it is sent."""
    if p.fpo_id is None:
        raise deny(request, p, "CAPABILITY_MISSING", "this account is not tied to an FPO")
    with tenant_conn(engine(), p.fpo_id) as conn:
        yield conn

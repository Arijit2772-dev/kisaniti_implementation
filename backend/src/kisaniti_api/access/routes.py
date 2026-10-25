"""Endpoints of access/v1 (L0_ACCESS_CONTRACT_v1.md §3)."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, FastAPI, Request, Response
from fastapi.responses import JSONResponse
from kisaniti_core.errors import ErrorCode
from kisaniti_core.ids import new_id
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Connection, text
from sqlalchemy.exc import IntegrityError

from ..problems import problem
from .config import load_access_settings
from .deps import COOKIE, CurrentPrincipal, engine, require, tenant_scope
from .errors import AccessError, Throttled
from .passwords import (
    dummy_verify,
    generate_temporary_password,
    hash_password,
    password_problems,
    verify_password,
)
from .service import (
    USER_COLUMNS,
    Principal,
    audit,
    auth_conn,
    blocked_seconds,
    capabilities_for,
    clear_failures,
    create_session,
    principal_json,
    register_failure,
    revoke_session,
    revoke_user_sessions,
    throttle_key,
    user_json,
)

router = APIRouter(prefix="/api/v1")
ManageUsers = Annotated[Principal, Depends(require("users.manage"))]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LoginBody(Strict):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class ChangePasswordBody(Strict):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=10, max_length=256)


class LanguageBody(Strict):
    language: Literal["pa", "en"]


class UserCreateBody(Strict):
    username: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{2,63}$")
    display_name: str = Field(min_length=1, max_length=120)
    role: Literal["FPO_ADMIN", "STAFF", "VIEWER"]
    extra_capabilities: list[Literal["contacts.view_phone", "messages.send"]] = Field(default_factory=list)
    language: Literal["pa", "en"] = "en"


def _set_cookie(response: Response, token: str, max_age: int) -> None:
    s = load_access_settings()
    response.set_cookie(COOKIE, token, max_age=max_age, httponly=True, samesite="lax", secure=s.cookie_secure, path="/")


# ---- sign-in and session ---------------------------------------------------------------------------------------
@router.post("/auth/login")
def login(body: LoginBody, request: Request, response: Response) -> dict[str, Any]:
    s = load_access_settings()
    rid = request.state.request_id
    username = body.username.strip().lower()
    key = throttle_key(username)
    outcome: str
    token = csrf = ""
    principal: Principal | None = None
    wait = 0
    with auth_conn(engine()) as conn:
        wait = blocked_seconds(conn, key)
        if wait:
            audit(conn, rid, "LOGIN_THROTTLED")
            outcome = "throttled"
        else:
            row = conn.execute(
                text(f"SELECT {USER_COLUMNS}, password_hash FROM users WHERE username = :u"), {"u": username}
            ).first()
            ok = False
            if row is None:
                dummy_verify(body.password, s.scrypt_n)
            else:
                ok = verify_password(body.password, row.password_hash) and row.active
            if not ok:
                register_failure(conn, key, s)
                audit(conn, rid, "LOGIN_FAILURE", fpo_id=row.fpo_id if row else None, subject=row.id if row else None)
                outcome = "failed"
            else:
                assert row is not None  # ok is only true for a found user
                clear_failures(conn, key)
                token, csrf, expires = create_session(conn, row.id, s)
                audit(conn, rid, "LOGIN_SUCCESS", actor=row.id, fpo_id=row.fpo_id, subject=row.id)
                principal = Principal(
                    user_id=row.id,
                    fpo_id=row.fpo_id,
                    role=row.role,
                    capabilities=capabilities_for(row.role, row.extra_capabilities),
                    language=row.language,
                    must_change_password=row.must_change_password,
                    csrf_token=csrf,
                    session_expires_at=expires,
                    session_hash="",
                    request_id=rid,
                )
                outcome = "ok"
    # the transaction is committed here, so the failure count and audit rows survive the error responses below
    if outcome == "throttled":
        raise Throttled(wait)
    if outcome == "failed" or principal is None:
        raise AccessError(ErrorCode.UNAUTHENTICATED, "wrong username or password")
    _set_cookie(response, token, s.absolute_minutes * 60)
    return principal_json(principal)


@router.get("/auth/me")
def me(p: CurrentPrincipal) -> dict[str, Any]:
    return principal_json(p)


@router.post("/auth/logout", status_code=204)
def logout(request: Request, p: CurrentPrincipal) -> Response:
    with auth_conn(engine()) as conn:
        revoke_session(conn, p.session_hash)
        audit(conn, request.state.request_id, "LOGOUT", actor=p.user_id, fpo_id=p.fpo_id, subject=p.user_id)
    resp = Response(status_code=204)
    resp.delete_cookie(COOKIE, path="/")
    return resp


@router.post("/auth/change-password", status_code=204)
def change_password(body: ChangePasswordBody, request: Request, p: CurrentPrincipal) -> Response:
    s = load_access_settings()
    with auth_conn(engine()) as conn:
        row = conn.execute(text("SELECT username, password_hash FROM users WHERE id = :u"), {"u": p.user_id}).one()
        if not verify_password(body.current_password, row.password_hash):
            raise AccessError(ErrorCode.UNAUTHENTICATED, "the current password is wrong")
        problems = password_problems(body.new_password, row.username)
        if body.new_password == body.current_password:
            problems.append("SAME_AS_CURRENT")
        if problems:
            raise AccessError(
                ErrorCode.VALIDATION_FAILED, "the new password is not acceptable", "WEAK_PASSWORD", "/new_password"
            )
        conn.execute(
            text(
                "UPDATE users SET password_hash = :h, must_change_password = false, password_changed_at = now() "
                "WHERE id = :u"
            ),
            {"h": hash_password(body.new_password, s.scrypt_n), "u": p.user_id},
        )
        revoke_user_sessions(conn, p.user_id, keep_hash=p.session_hash)
        audit(conn, request.state.request_id, "PASSWORD_CHANGED", actor=p.user_id, fpo_id=p.fpo_id, subject=p.user_id)
    return Response(status_code=204)


@router.put("/auth/language")
def set_language(body: LanguageBody, request: Request, p: CurrentPrincipal) -> dict[str, Any]:
    with auth_conn(engine()) as conn:
        conn.execute(text("UPDATE users SET language = :l WHERE id = :u"), {"l": body.language, "u": p.user_id})
        audit(conn, request.state.request_id, "LANGUAGE_CHANGED", actor=p.user_id, fpo_id=p.fpo_id, subject=p.user_id)
    return principal_json(_with_language(p, body.language))


def _with_language(p: Principal, language: str) -> Principal:
    return Principal(
        user_id=p.user_id,
        fpo_id=p.fpo_id,
        role=p.role,
        capabilities=p.capabilities,
        language=language,
        must_change_password=p.must_change_password,
        csrf_token=p.csrf_token,
        session_expires_at=p.session_expires_at,
        session_hash=p.session_hash,
        request_id=p.request_id,
    )


# ---- users of the caller's own FPO ------------------------------------------------------------------------------
def _missing_user(request: Request, p: Principal, user_id: str) -> AccessError:
    """404 for a missing user and for another FPO's user alike; the second case is also written to the audit log."""
    with auth_conn(engine()) as conn:
        elsewhere = conn.execute(
            text("SELECT 1 FROM users WHERE id = :i AND fpo_id IS DISTINCT FROM :f"), {"i": user_id, "f": p.fpo_id}
        ).first()
        if elsewhere:
            audit(conn, request.state.request_id, "CROSS_TENANT_REFERENCE", actor=p.user_id, fpo_id=p.fpo_id)
    return AccessError(ErrorCode.NOT_FOUND, "user not found")


def _own_user(conn: Connection, p: Principal, user_id: str) -> Any:
    return conn.execute(
        text(f"SELECT {USER_COLUMNS} FROM users WHERE id = :i AND fpo_id = :f"), {"i": user_id, "f": p.fpo_id}
    ).first()


@router.get("/users")
def list_users(request: Request, p: ManageUsers) -> list[dict[str, Any]]:
    with tenant_scope(request, p) as conn:
        rows = conn.execute(
            text(f"SELECT {USER_COLUMNS} FROM users WHERE fpo_id = :f ORDER BY username"), {"f": p.fpo_id}
        ).all()
    return [user_json(r) for r in rows]


@router.get("/users/{user_id}")
def get_user(user_id: str, request: Request, p: ManageUsers) -> dict[str, Any]:
    with tenant_scope(request, p) as conn:
        row = _own_user(conn, p, user_id)
    if row is None:
        raise _missing_user(request, p, user_id)
    return user_json(row)


@router.post("/users", status_code=201)
def create_user(body: UserCreateBody, request: Request, p: ManageUsers) -> dict[str, Any]:
    if body.extra_capabilities and body.role != "STAFF":
        raise AccessError(
            ErrorCode.VALIDATION_FAILED,
            "only staff can have extra capabilities",
            "ROLE_MISMATCH",
            "/extra_capabilities",
        )
    s = load_access_settings()
    temp = generate_temporary_password()
    uid = new_id("usr")
    taken = False
    with tenant_scope(request, p) as conn:
        try:
            with conn.begin_nested():
                conn.execute(
                    text(
                        "INSERT INTO users (id, fpo_id, username, display_name, role, extra_capabilities, language, "
                        "active, must_change_password, password_hash, origin) VALUES (:i, :f, :u, :d, :r, :x, :l, "
                        "true, true, :h, (SELECT origin FROM users WHERE id = :a))"
                    ),
                    {
                        "i": uid,
                        "f": p.fpo_id,
                        "u": body.username,
                        "d": body.display_name,
                        "r": body.role,
                        "x": body.extra_capabilities,
                        "l": body.language,
                        "h": hash_password(temp, s.scrypt_n),
                        "a": p.user_id,
                    },
                )
        except IntegrityError:
            taken = True
        if not taken:
            audit(conn, request.state.request_id, "USER_CREATED", actor=p.user_id, fpo_id=p.fpo_id, subject=uid)
            row = _own_user(conn, p, uid)
    if taken:
        raise AccessError(ErrorCode.CONFLICT, "that username is already taken")
    return {"contract": "access.temporary_password/v1", "user": user_json(row), "temporary_password": temp}


@router.post("/users/{user_id}/reset-password")
def reset_password(user_id: str, request: Request, p: ManageUsers) -> dict[str, Any]:
    s = load_access_settings()
    temp = generate_temporary_password()
    with tenant_scope(request, p) as conn:
        row = _own_user(conn, p, user_id)
        if row is not None:
            conn.execute(
                text(
                    "UPDATE users SET password_hash = :h, must_change_password = true, password_changed_at = now() "
                    "WHERE id = :i AND fpo_id = :f"
                ),
                {"h": hash_password(temp, s.scrypt_n), "i": user_id, "f": p.fpo_id},
            )
            audit(conn, request.state.request_id, "PASSWORD_RESET", actor=p.user_id, fpo_id=p.fpo_id, subject=user_id)
            row = _own_user(conn, p, user_id)
    if row is None:
        raise _missing_user(request, p, user_id)
    with auth_conn(engine()) as conn:  # sessions are outside the tenant role's reach
        revoke_user_sessions(conn, user_id)
    return {"contract": "access.temporary_password/v1", "user": user_json(row), "temporary_password": temp}


@router.post("/users/{user_id}/deactivate", status_code=204)
def deactivate_user(user_id: str, request: Request, p: ManageUsers) -> Response:
    if user_id == p.user_id:
        raise AccessError(ErrorCode.CONFLICT, "you cannot deactivate your own account")
    with tenant_scope(request, p) as conn:
        found = _own_user(conn, p, user_id) is not None
        if found:
            conn.execute(
                text("UPDATE users SET active = false WHERE id = :i AND fpo_id = :f"), {"i": user_id, "f": p.fpo_id}
            )
            audit(conn, request.state.request_id, "USER_DEACTIVATED", actor=p.user_id, fpo_id=p.fpo_id, subject=user_id)
    if not found:
        raise _missing_user(request, p, user_id)
    with auth_conn(engine()) as conn:
        revoke_user_sessions(conn, user_id)
    return Response(status_code=204)


def install(app: FastAPI) -> None:
    @app.exception_handler(AccessError)
    async def access_error(request: Request, exc: AccessError) -> JSONResponse:
        issues = None
        if exc.issue_code:
            issues = [
                {
                    "code": exc.issue_code,
                    "message": exc.detail,
                    "severity": "ERROR",
                    **({"path": exc.issue_path} if exc.issue_path else {}),
                }
            ]
        resp = problem(exc.code, exc.detail, request, issues)
        for k, v in exc.headers.items():
            resp.headers[k] = v
        return resp

    @app.exception_handler(Throttled)
    async def throttled(request: Request, exc: Throttled) -> JSONResponse:
        body = {
            "type": "https://kisaniti.invalid/errors/TOO_MANY_ATTEMPTS",
            "title": "Too many attempts",
            "status": 429,
            "code": "TOO_MANY_ATTEMPTS",
            "detail": "too many failed sign-ins; try again later",
            "instance": request.url.path,
            "request_id": getattr(request.state, "request_id", ""),
            "retry_after_s": max(1, exc.retry_after_s),
        }
        return JSONResponse(
            body,
            status_code=429,
            media_type="application/problem+json",
            headers={"Retry-After": str(body["retry_after_s"])},
        )

    app.include_router(router)

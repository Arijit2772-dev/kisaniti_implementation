"""RFC 9457 problem responses with the frozen shared error codes (CONTRACT_SPEC_V1_shared.md §6)."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from kisaniti_core.errors import HTTP_STATUS, ContractError, ErrorCode
from starlette.exceptions import HTTPException as StarletteHTTPException

log = logging.getLogger("kisaniti_api")

_HTTP_TO_CODE = {
    400: ErrorCode.INVALID_REQUEST,
    401: ErrorCode.UNAUTHENTICATED,
    403: ErrorCode.FORBIDDEN,
    404: ErrorCode.NOT_FOUND,
    405: ErrorCode.INVALID_REQUEST,
    409: ErrorCode.CONFLICT,
}


def problem(
    code: ErrorCode,
    detail: str,
    request: Request,
    issues: list[dict[str, Any]] | None = None,
    status: int | None = None,
) -> JSONResponse:
    body: dict[str, Any] = {
        "type": f"https://kisaniti.invalid/errors/{code.value}",
        "title": code.value.replace("_", " ").capitalize(),
        "status": status or HTTP_STATUS[code],
        "code": code.value,
        "detail": detail,
        "instance": request.url.path,
        "request_id": getattr(request.state, "request_id", ""),
    }
    if issues:
        body["issues"] = issues
    return JSONResponse(body, status_code=body["status"], media_type="application/problem+json")


def _pointer(loc: tuple[Any, ...]) -> str:
    parts = [str(p) for p in loc if p not in ("body", "query", "path")]
    return "/" + "/".join(parts) if parts else ""


def install(app: FastAPI) -> None:
    @app.middleware("http")
    async def request_id(request: Request, call_next: Any) -> Any:
        request.state.request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        return response

    @app.exception_handler(ContractError)
    async def contract_error(request: Request, exc: ContractError) -> JSONResponse:
        issue = {"code": exc.code.value, "message": exc.message, "severity": "ERROR"}
        if exc.path:
            issue["path"] = exc.path
        return problem(exc.code, exc.message, request, [issue])

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        issues = [
            {
                "code": ErrorCode.VALIDATION_FAILED.value,
                "path": _pointer(tuple(e.get("loc", ()))),
                "message": str(e.get("msg", "invalid")),
                "severity": "ERROR",
            }
            for e in exc.errors()
        ]
        return problem(ErrorCode.VALIDATION_FAILED, "request validation failed", request, issues)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = _HTTP_TO_CODE.get(exc.status_code, ErrorCode.INVALID_REQUEST)
        return problem(code, str(exc.detail), request, status=exc.status_code)

    @app.exception_handler(Exception)
    async def unexpected(request: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled error request_id=%s", getattr(request.state, "request_id", ""))
        return problem(ErrorCode.INTERNAL, "unexpected server error; see logs with the request_id", request)

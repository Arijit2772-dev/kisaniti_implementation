"""Error codes of the frozen shared contract (CONTRACT_SPEC_V1_shared.md §6)."""

from __future__ import annotations

from enum import StrEnum


class ErrorCode(StrEnum):
    INVALID_REQUEST = "INVALID_REQUEST"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    INVALID_DECIMAL = "INVALID_DECIMAL"
    PRECISION_EXCEEDED = "PRECISION_EXCEEDED"
    OUT_OF_RANGE = "OUT_OF_RANGE"
    UNIT_MISMATCH = "UNIT_MISMATCH"
    INVALID_ID = "INVALID_ID"
    INVALID_INSTANT = "INVALID_INSTANT"
    INVALID_DATE = "INVALID_DATE"
    TENANT_SCOPE_VIOLATION = "TENANT_SCOPE_VIOLATION"
    SYNTHETIC_INPUT_IN_LIVE = "SYNTHETIC_INPUT_IN_LIVE"
    UNAUTHENTICATED = "UNAUTHENTICATED"
    FORBIDDEN = "FORBIDDEN"
    NOT_FOUND = "NOT_FOUND"
    CONFLICT = "CONFLICT"
    IDEMPOTENCY_KEY_REUSED = "IDEMPOTENCY_KEY_REUSED"
    NOT_READY = "NOT_READY"
    DEPENDENCY_UNAVAILABLE = "DEPENDENCY_UNAVAILABLE"
    INTERNAL = "INTERNAL"


HTTP_STATUS: dict[ErrorCode, int] = {
    ErrorCode.INVALID_REQUEST: 400,
    ErrorCode.VALIDATION_FAILED: 422,
    ErrorCode.INVALID_DECIMAL: 422,
    ErrorCode.PRECISION_EXCEEDED: 422,
    ErrorCode.OUT_OF_RANGE: 422,
    ErrorCode.UNIT_MISMATCH: 422,
    ErrorCode.INVALID_ID: 422,
    ErrorCode.INVALID_INSTANT: 422,
    ErrorCode.INVALID_DATE: 422,
    ErrorCode.TENANT_SCOPE_VIOLATION: 422,
    ErrorCode.SYNTHETIC_INPUT_IN_LIVE: 422,
    ErrorCode.UNAUTHENTICATED: 401,
    ErrorCode.FORBIDDEN: 403,
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.CONFLICT: 409,
    ErrorCode.IDEMPOTENCY_KEY_REUSED: 409,
    ErrorCode.NOT_READY: 503,
    ErrorCode.DEPENDENCY_UNAVAILABLE: 503,
    ErrorCode.INTERNAL: 500,
}


class ContractError(ValueError):
    """A value or input breaks a frozen contract. Carries a contract error code and an optional JSON Pointer."""

    def __init__(self, code: ErrorCode, message: str, path: str | None = None) -> None:
        super().__init__(f"{code}: {message}" + (f" (at {path})" if path else ""))
        self.code = code
        self.message = message
        self.path = path

"""Standard error responses: every error carries a stable `code`, a
human-readable `message` (also exposed as `detail`), a correlation id, a
retryability flag, per-field errors for validation failures, and the HTTP
status. `detail` stays a plain string so existing consumers can show it as-is.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from slowapi.errors import RateLimitExceeded
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)

CORRELATION_ID_HEADER = "X-Correlation-ID"

# Default code per status. Endpoints whose clients must tell two failures with
# the same status apart raise ApiError with an explicit code instead.
_CODE_BY_STATUS: dict[int, str] = {
    400: "BAD_REQUEST",
    401: "UNAUTHORIZED",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    409: "CONFLICT",
    422: "VALIDATION_ERROR",
    429: "RATE_LIMITED",
    500: "INTERNAL_ERROR",
    502: "BAD_GATEWAY",
    503: "SERVICE_UNAVAILABLE",
    504: "GATEWAY_TIMEOUT",
}

# A client can reasonably retry the exact same request unchanged for these --
# a transient/capacity condition, not something the request itself got
# wrong. 4xx codes other than 429 mean the request needs to change first.
_RETRYABLE_STATUSES = {429, 500, 502, 503, 504}


class ApiError(StarletteHTTPException):
    """An HTTP error with a domain-specific `code` (e.g. CONVERSATION_BUSY)
    that clients branch on instead of inferring meaning from the status."""

    def __init__(self, status_code: int, detail: str, code: str, *, retryable: bool = False):
        super().__init__(status_code=status_code, detail=detail)
        self.code = code
        self.retryable = retryable


def _code_for_status(status_code: int) -> str:
    return _CODE_BY_STATUS.get(status_code, f"HTTP_{status_code}")


def _correlation_id(request: Request) -> str:
    # Set by the correlation-id middleware; the fallback keeps a missing id
    # from becoming its own error.
    return getattr(request.state, "correlation_id", None) or str(uuid.uuid4())


def error_body(
    request: Request,
    status_code: int,
    message: str,
    *,
    field_errors: list[dict] | None = None,
    code: str | None = None,
    retryable: bool | None = None,
) -> dict:
    return {
        "detail": message,
        "code": code or _code_for_status(status_code),
        "message": message,
        "correlation_id": _correlation_id(request),
        "retryable": status_code in _RETRYABLE_STATUSES if retryable is None else retryable,
        "field_errors": field_errors or [],
        "status": status_code,
    }


async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    message = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
    body = error_body(
        request, exc.status_code, message,
        code=getattr(exc, "code", None), retryable=getattr(exc, "retryable", None),
    )
    return JSONResponse(status_code=exc.status_code, content=body, headers=exc.headers)


async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    field_errors = [
        {"field": ".".join(str(p) for p in e["loc"] if p != "body"), "message": e["msg"]}
        for e in exc.errors()
    ]
    summary = "; ".join(f"{fe['field']}: {fe['message']}" for fe in field_errors) or "Invalid request."
    body = error_body(request, status.HTTP_422_UNPROCESSABLE_CONTENT, summary, field_errors=field_errors)
    return JSONResponse(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, content=body)


async def rate_limit_exception_handler(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    message = f"Rate limit exceeded: {exc.detail}" if exc.detail else "Rate limit exceeded."
    body = error_body(request, status.HTTP_429_TOO_MANY_REQUESTS, message)
    return JSONResponse(status_code=status.HTTP_429_TOO_MANY_REQUESTS, content=body)


def unhandled_exception_body(request: Request, dev_mode: bool, exc: Exception) -> JSONResponse | None:
    """Returns None in dev mode to signal "re-raise the real exception" so a
    traceback is never masked during local development."""
    if dev_mode:
        return None
    logger.exception("Unhandled exception (correlation_id=%s)", _correlation_id(request))
    body = error_body(request, status.HTTP_500_INTERNAL_SERVER_ERROR, "An unexpected error occurred.")
    return JSONResponse(status_code=500, content=body)

"""Phase 1 (narrowed scope, 2026-08-26): "Standardize safe errors: code,
display message, correlation ID, retryability, field errors, and HTTP
status." Every error response across this app used to be FastAPI's bare
default -- `{"detail": "..."}` for a raised HTTPException, or `{"detail":
[{"loc": [...], "msg": ..., "type": ...}]}` (a LIST, not a string) for a
422 validation error -- with no error code, no correlation id, no
machine-readable retryability signal, and, for 422s, `detail` isn't even a
string a client can safely display.

`detail` is kept in every response, unchanged in meaning from before this
file existed (a human-readable message), because two existing consumers --
`app/web/static/js/app.js` and `admin.js` -- already read `body.detail` to
show the real error text, and neither should have to change for this. The
new fields (`code`, `message`, `correlation_id`, `retryable`,
`field_errors`, `status`) are additive.
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

# Deliberately coarse -- one code per status actually raised in this app
# today, not a large enum of hypothetical codes nothing returns yet.
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


def _code_for_status(status_code: int) -> str:
    return _CODE_BY_STATUS.get(status_code, f"HTTP_{status_code}")


def _correlation_id(request: Request) -> str:
    # Set by CorrelationIdMiddleware for every request; this fallback only
    # matters if an exception handler somehow runs before that middleware
    # does (it shouldn't, but a missing id must never itself be the error).
    return getattr(request.state, "correlation_id", None) or str(uuid.uuid4())


def error_body(
    request: Request,
    status_code: int,
    message: str,
    *,
    field_errors: list[dict] | None = None,
) -> dict:
    return {
        "detail": message,
        "code": _code_for_status(status_code),
        "message": message,
        "correlation_id": _correlation_id(request),
        "retryable": status_code in _RETRYABLE_STATUSES,
        "field_errors": field_errors or [],
        "status": status_code,
    }


async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    message = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
    body = error_body(request, exc.status_code, message)
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
    """Returns None in dev mode to signal "re-raise the real exception",
    matching this handler's pre-existing behavior of never masking a
    traceback during local development."""
    if dev_mode:
        return None
    logger.exception("Unhandled exception (correlation_id=%s)", _correlation_id(request))
    body = error_body(request, status.HTTP_500_INTERNAL_SERVER_ERROR, "An unexpected error occurred.")
    return JSONResponse(status_code=500, content=body)

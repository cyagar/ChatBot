"""Rate limiting, keyed by authenticated session where available (falls back to
client IP for unauthenticated requests like login/register)."""

from __future__ import annotations

from slowapi import Limiter
from slowapi.util import get_remote_address

from app.auth.deps import SESSION_COOKIE
from app.config import get_settings


def _key_func(request) -> str:
    token = request.cookies.get(SESSION_COOKIE)
    return f"session:{token}" if token else f"ip:{get_remote_address(request)}"


limiter = Limiter(key_func=_key_func)


def default_limit_string() -> str:
    return f"{get_settings().rate_limit_per_minute}/minute"

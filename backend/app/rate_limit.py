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


def auth_key_func(request) -> str:
    """External review finding P1-24 (2026-09-21): login/register must NOT use
    the default _key_func above. That function trusts any client-supplied
    tma_session cookie value as a rate-limit bucket key without verifying it's
    a real signed session -- login/register are exactly the unauthenticated
    routes where the client controls that cookie completely, so an attacker
    sending a fresh unsigned/garbage cookie on every request gets a fresh
    bucket every time and the IP-based fallback never engages. Reproduced:
    12 login attempts with a different fake cookie each time all returned 401
    (no 429), while 12 attempts with no cookie at all correctly hit the limit
    after 10. Applied per-route via @limiter.limit(..., key_func=auth_key_func)
    on /register and /login specifically -- unlike the default, this always
    keys on IP, since a login/register request has no legitimate session yet
    for this to protect."""
    return f"ip:{get_remote_address(request)}"


limiter = Limiter(key_func=_key_func)


def default_limit_string() -> str:
    return f"{get_settings().rate_limit_per_minute}/minute"


# Deliberately tighter and fixed (not settings-derived): login/register are
# brute-force/enumeration targets, not normal usage traffic, so this shouldn't
# scale with the general chat rate limit (concern #20: "rate-limit login and
# registration, not only chat").
AUTH_RATE_LIMIT = "10/minute"

"""Regressions from the 2026-09-21 external release review (FinalChanges.txt),
tracked one test per finding ID so each fix stays independently verifiable.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_p1_24_login_rate_limit_cannot_be_bypassed_by_rotating_cookies(test_env, monkeypatch):
    """P1-24: the default rate-limit key function keys on whatever tma_session
    cookie value a client happens to send, without verifying it's a real
    signed session -- login/register are exactly the routes an unauthenticated
    attacker controls that cookie completely for. Reproduced before the fix:
    12 login attempts, each with a different unsigned/garbage cookie, all
    returned 401 with no 429 at all, while the same 12 attempts with NO cookie
    correctly hit 429 after AUTH_RATE_LIMIT (10/minute) was exceeded. Fixed by
    routing /api/auth/login and /api/auth/register through a dedicated
    key_func (app/rate_limit.py's auth_key_func) that always keys on IP,
    applied per-route via @limiter.limit(..., key_func=...), leaving the
    session-aware default key_func for other rate-limited routes unchanged."""
    monkeypatch.setenv("RATE_LIMIT_PER_MINUTE", "60")  # irrelevant here; AUTH_RATE_LIMIT is fixed at 10/minute
    from app.config import get_settings
    get_settings.cache_clear()

    statuses = []
    for i in range(11):
        resp = client.post(
            "/api/auth/login",
            json={"email": "nobody@hmwagner.com", "password": "wrong-password"},
            cookies={"tma_session": f"garbage-token-{i}"},
        )
        statuses.append(resp.status_code)

    assert statuses[:10] == [401] * 10, f"expected 10 failed-credentials responses, got {statuses[:10]}"
    assert statuses[10] == 429, (
        f"rotating a different fake cookie on every request must NOT reset the rate-limit bucket "
        f"-- expected the 11th attempt to be throttled, got status sequence {statuses}"
    )


def test_p1_04_stale_corpus_status_accepts_a_real_psycopg_datetime(test_env, monkeypatch):
    """P1-04: _corpus_status's stale-corpus check called
    datetime.fromisoformat() on ingestion_runs.finished_at, a TIMESTAMPTZ
    column psycopg already returns as a real, aware datetime (not a string)
    -- fromisoformat(datetime_obj) raised TypeError every time, and the
    function's own blanket `except Exception: return "ok", ""` silently
    swallowed it, so a corpus that hadn't synced in days (or ever) always
    reported healthy. Reproduced directly against this test before the fix
    (asserted "ok" for a 2020 run, which passed -- the bug). Fixed by using
    the datetime value directly instead of re-parsing it as a string."""
    from app.api.routes_config import _corpus_status
    from app.config import get_settings
    from app.db import get_conn

    monkeypatch.setenv("GOOGLE_DRIVE_FOLDER_ID", "test-folder-id")
    monkeypatch.setenv("INGESTION_STALENESS_THRESHOLD_HOURS", "48")
    get_settings.cache_clear()
    settings = get_settings()

    with get_conn() as conn:
        conn.execute(
            "INSERT INTO ingestion_runs (started_at, finished_at, status) "
            "VALUES (now() - interval '200 hours', now() - interval '199 hours', 'completed')"
        )
    status_, message = _corpus_status(settings)
    assert status_ == "degraded", f"a sync from 199 hours ago must not report healthy, got ({status_!r}, {message!r})"

    with get_conn() as conn:
        conn.execute(
            "INSERT INTO ingestion_runs (started_at, finished_at, status) "
            "VALUES (now() - interval '1 hour', now(), 'completed')"
        )
    status_, message = _corpus_status(settings)
    assert status_ == "ok", f"a just-completed sync must report healthy, got ({status_!r}, {message!r})"

    get_settings.cache_clear()

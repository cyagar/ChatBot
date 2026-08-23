"""P1-4 (independent follow-up review): "visible last-success
timestamp/source snapshot" and a stale-corpus alert against the configured
operational SLA -- GET /api/admin/ingestion/status."""

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.db import get_conn
from app.main import app
from tests.conftest import register_test_user

client = TestClient(app)


def _register_admin(email="admin@example.com"):
    return register_test_user(client, email, role="administrator", admin_email=email)


def _insert_run(conn, *, status, trigger, started_at, finished_at):
    conn.execute(
        "INSERT INTO ingestion_runs (status, trigger, started_at, finished_at) VALUES (?, ?, ?, ?)",
        (status, trigger, started_at, finished_at),
    )


def test_no_runs_yet_is_reported_as_stale(test_env):
    _register_admin()
    resp = client.get("/api/admin/ingestion/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["is_stale"] is True
    assert body["last_success_at"] is None
    assert body["last_success_run_id"] is None
    assert body["hours_since_last_success"] is None


def test_recent_successful_run_is_not_stale(test_env):
    one_hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    with get_conn() as conn:
        _insert_run(
            conn, status="completed", trigger="scheduled",
            started_at=one_hour_ago, finished_at=one_hour_ago,
        )
    _register_admin()

    resp = client.get("/api/admin/ingestion/status")
    body = resp.json()
    assert body["last_success_run_id"] is not None
    assert body["last_success_trigger"] == "scheduled"
    assert 0.9 < body["hours_since_last_success"] < 1.1
    assert body["is_stale"] is False, "1 hour ago must be well under the default 48h SLA"


def test_old_failed_run_only_does_not_count_as_success(test_env):
    with get_conn() as conn:
        _insert_run(
            conn, status="failed", trigger="manual",
            started_at="2020-01-01 00:00:00", finished_at="2020-01-01 00:05:00",
        )
    _register_admin()

    resp = client.get("/api/admin/ingestion/status")
    body = resp.json()
    assert body["is_stale"] is True
    assert body["last_success_at"] is None, "a run that never finished successfully must not count as the last success"
    assert body["last_attempt_status"] == "failed"


def test_old_success_far_past_the_sla_is_flagged_stale(test_env):
    with get_conn() as conn:
        _insert_run(
            conn, status="completed", trigger="manual",
            started_at="2000-01-01 00:00:00", finished_at="2000-01-01 00:05:00",
        )
    _register_admin()

    resp = client.get("/api/admin/ingestion/status")
    body = resp.json()
    assert body["is_stale"] is True
    assert body["hours_since_last_success"] > body["staleness_threshold_hours"]


def test_completed_with_errors_still_counts_as_a_successful_sync(test_env):
    """A run that finished but had some individual file failures still means
    the corpus WAS reconciled against Drive -- only a run that never finished
    at all is not a freshness success."""
    with get_conn() as conn:
        _insert_run(
            conn, status="completed_with_errors", trigger="scheduled",
            started_at="2026-08-23 10:00:00", finished_at="2026-08-23 10:05:00",
        )
    _register_admin()

    resp = client.get("/api/admin/ingestion/status")
    body = resp.json()
    assert body["last_success_run_id"] is not None
    assert body["last_success_trigger"] == "scheduled"


def test_status_requires_admin(test_env):
    _register_admin("admin2@example.com")
    register_test_user(client, "tech@example.com", admin_email="admin2@example.com")
    resp = client.get("/api/admin/ingestion/status")
    assert resp.status_code == 403


def test_scheduler_disabled_by_default_when_drive_is_not_configured(test_env):
    """test_env always leaves GOOGLE_DRIVE_FOLDER_ID blank -- scheduler_enabled
    must reflect that, not silently claim the automated mechanism is active."""
    _register_admin()
    resp = client.get("/api/admin/ingestion/status")
    assert resp.json()["scheduler_enabled"] is False

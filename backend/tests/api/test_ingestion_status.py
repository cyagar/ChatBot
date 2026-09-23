"""P1-4 (independent follow-up review): "visible last-success
timestamp/source snapshot" and a stale-corpus alert against the configured
operational SLA -- GET /api/admin/ingestion/status."""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.db import get_conn
from app.main import app
from tests.conftest import register_test_user

client = TestClient(app)


def _register_admin(email="admin@example.com"):
    return register_test_user(client, email, role="administrator", admin_email=email)


def _insert_run(conn, *, status, trigger, started_at, finished_at):
    conn.execute(
        "INSERT INTO ingestion_runs (status, trigger, started_at, finished_at) VALUES (%s, %s, %s, %s)",
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


def test_p1_15_status_reports_chunks_needing_reembedding(test_env):
    """A chunk whose only embedding is from a different model/revision than
    the currently configured one (see embedding_fingerprint's docstring)
    must be counted here -- otherwise an admin who bumps
    EMBEDDING_MODEL_REVISION has no way to know a re-index is needed;
    search would just silently degrade to lexical-only for those chunks."""
    from app.retrieval.embeddings import vector_to_blob
    import numpy as np

    with get_conn() as conn:
        conn.execute("INSERT INTO manufacturers (name) VALUES ('Bunn-O-Matic Corporation')")
        conn.execute("INSERT INTO machines (manufacturer_id, model_name) VALUES (1, 'Axiom')")
        conn.execute(
            "INSERT INTO documents (original_filename, storage_path, source_system, source_ref, "
            "file_type, sha256, byte_size, status, review_status) VALUES ('axiom.pdf', 'axiom.pdf', "
            "'local_directory', 'axiom.pdf', 'pdf', 'hash1', 100, 'indexed', 'approved')"
        )
        cur = conn.execute(
            "INSERT INTO chunks (document_id, page_number, chunk_type, content, char_count, ordinal) "
            "VALUES (1, 1, 'text', 'Some manual content here.', 25, 0) RETURNING id"
        )
        chunk_id = cur.fetchone()["id"]
        conn.execute(
            "INSERT INTO embeddings (chunk_id, model_name, dim, vector) VALUES (%s, %s, %s, %s)",
            (chunk_id, "some-old-model@old-revision", 4, vector_to_blob(np.zeros(4, dtype=np.float32))),
        )
    _register_admin()

    body = client.get("/api/admin/ingestion/status").json()
    assert body["chunks_needing_reembedding"] == 1


def test_last_success_status_distinguishes_clean_from_error_runs(test_env):
    """Independent follow-up review 2026-08-24 P0-6: staleness correctly
    treats completed_with_errors as a success (the test above), but the
    response previously gave no way to tell a clean success from one with
    individual file failures without a second call to /ingestion/runs."""
    with get_conn() as conn:
        _insert_run(
            conn, status="completed_with_errors", trigger="scheduled",
            started_at="2026-08-23 10:00:00", finished_at="2026-08-23 10:05:00",
        )
    _register_admin()

    resp = client.get("/api/admin/ingestion/status")
    assert resp.json()["last_success_status"] == "completed_with_errors"


# --- Run row persisted before the 202 (P0-6) -------------------------------

def test_reindex_run_row_exists_synchronously_before_the_background_task_runs(test_env, monkeypatch):
    """Independent follow-up review 2026-08-24 P0-6: the ingestion_runs row
    used to be created inside ingest_all(), which only executes once the
    BackgroundTask actually runs -- after the 202 response was already sent.
    If the process restarted in that window, an admin told a run started
    would see no evidence one ever was. The row must now exist by the time
    trigger_reindex() calls background_tasks.add_task(), which this proves
    by stubbing ingest_all() to record what run_id it was handed instead of
    doing any real ingestion work."""
    calls = []

    def fake_ingest_all(run_id=None, **kwargs):
        calls.append(run_id)

    import app.api.routes_admin as routes_admin
    monkeypatch.setattr(routes_admin, "ingest_all", fake_ingest_all)
    _register_admin()

    resp = client.post("/api/admin/ingestion/reindex")
    assert resp.status_code == 202
    run_id = resp.json()["run_id"]
    assert run_id is not None

    with get_conn() as conn:
        row = conn.execute("SELECT status, trigger FROM ingestion_runs WHERE id = %s", (run_id,)).fetchone()
    assert row is not None, "the run row must exist by the time the 202 response is returned"
    assert row["status"] == "running"
    assert row["trigger"] == "manual"
    assert calls == [run_id], "ingest_all() must be handed the same run_id the row was created with"


def test_ingest_all_marks_the_passed_in_run_failed_if_it_cannot_get_the_lock(test_env):
    """The row is created before the lock is actually acquired inside
    ingest_all() (the /reindex endpoint only checks .locked(), a
    check-then-act race against the scheduler's own timer). If ingest_all()
    then can't acquire the lock, the pre-created row must not be left
    dangling at status='running' forever -- it has to be marked failed with
    a clear reason."""
    from app.ingestion.pipeline import _INGEST_LOCK, ingest_all

    with get_conn() as conn:
        cur = conn.execute("INSERT INTO ingestion_runs (status, trigger) VALUES ('running', 'manual') RETURNING id")
        run_id = cur.fetchone()["id"]

    _INGEST_LOCK.acquire(blocking=False)
    try:
        with pytest.raises(RuntimeError):
            ingest_all(run_id=run_id)
    finally:
        _INGEST_LOCK.release()

    with get_conn() as conn:
        row = conn.execute("SELECT status, finished_at FROM ingestion_runs WHERE id = %s", (run_id,)).fetchone()
    assert row["status"] == "failed"
    assert row["finished_at"] is not None


def test_ingest_all_marks_the_run_failed_if_another_process_holds_the_db_advisory_lock(test_env):
    """P1-14 (external review, 2026-09-21): _INGEST_LOCK is a threading.Lock,
    process-local -- it does nothing against a second worker process (or two
    app instances briefly overlapping during a rolling deploy) starting a
    concurrent run. Simulates "another process" by holding the same
    Postgres advisory lock on a separate connection, bypassing this
    process's own (necessarily free) threading.Lock entirely."""
    import psycopg

    from app.config import get_settings
    from app.ingestion.pipeline import _ADVISORY_LOCK_KEY, ingest_all

    with get_conn() as conn:
        cur = conn.execute("INSERT INTO ingestion_runs (status, trigger) VALUES ('running', 'manual') RETURNING id")
        run_id = cur.fetchone()["id"]

    other_session = psycopg.connect(get_settings().database_url_unpooled, autocommit=True)
    other_session.execute("SELECT pg_try_advisory_lock(%s)", (_ADVISORY_LOCK_KEY,))
    try:
        with pytest.raises(RuntimeError):
            ingest_all(run_id=run_id)
    finally:
        other_session.execute("SELECT pg_advisory_unlock(%s)", (_ADVISORY_LOCK_KEY,))
        other_session.close()

    with get_conn() as conn:
        row = conn.execute("SELECT status, finished_at FROM ingestion_runs WHERE id = %s", (run_id,)).fetchone()
    assert row["status"] == "failed"
    assert row["finished_at"] is not None


def test_ingest_all_releases_the_db_advisory_lock_once_the_run_finishes(test_env, tmp_path):
    """Regression guard against a leaked session/lock: if _release_db_lock
    didn't run (or didn't actually close/unlock), a second, genuinely
    sequential run would be permanently blocked."""
    import psycopg

    from app.config import get_settings
    from app.ingestion.pipeline import _ADVISORY_LOCK_KEY, ingest_all
    from tests.ingestion.fakes import FakeDirectorySource

    ingest_all(source=FakeDirectorySource(tmp_path), embed=False)

    probe = psycopg.connect(get_settings().database_url_unpooled, autocommit=True)
    try:
        held = probe.execute("SELECT pg_try_advisory_lock(%s)", (_ADVISORY_LOCK_KEY,)).fetchone()[0]
        assert held is True, "the advisory lock from the finished run was never released"
        probe.execute("SELECT pg_advisory_unlock(%s)", (_ADVISORY_LOCK_KEY,))
    finally:
        probe.close()


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

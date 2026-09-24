"""/healthz cannot prove readiness for serving a citation. GET /readyz
checks the concrete dependencies a real answer needs -- database, object
storage, corpus freshness -- instead of always reporting healthy."""
from __future__ import annotations

from contextlib import contextmanager

from fastapi.testclient import TestClient

from app.db import get_conn
from app.main import app

client = TestClient(app)


def _seed_retrievable_document(review_status="approved", link_status="approved", current=True):
    with get_conn() as conn:
        conn.execute("INSERT INTO manufacturers (name) VALUES ('Bunn-O-Matic Corporation')")
        conn.execute("INSERT INTO machines (manufacturer_id, model_name) VALUES (1, 'Axiom')")
        doc_id = conn.execute(
            "INSERT INTO documents (original_filename, storage_path, source_system, source_ref, "
            "file_type, sha256, byte_size, status, review_status, is_current_revision) VALUES "
            "('a.pdf', 'a.pdf', 'google_drive', 'ref', 'pdf', 'h', 1, 'indexed', %s, %s) RETURNING id",
            (review_status, current),
        ).fetchone()["id"]
        conn.execute(
            "INSERT INTO document_machines (document_id, machine_id, review_status) VALUES (%s, 1, %s)",
            (doc_id, link_status),
        )


def test_readyz_reports_ok_when_database_and_storage_are_healthy(test_env):
    _seed_retrievable_document()
    resp = client.get("/readyz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["database"] == "ok"
    assert body["storage"] == "ok"
    assert body["corpus"] in ("ok", "degraded")


def test_readyz_returns_503_when_the_database_is_unreachable(test_env, monkeypatch):
    @contextmanager
    def broken_get_conn():
        raise RuntimeError("simulated database outage")
        yield  # pragma: no cover -- unreachable, satisfies contextmanager's generator shape

    monkeypatch.setattr("app.main.get_conn", broken_get_conn)

    resp = client.get("/readyz")
    assert resp.status_code == 503
    body = resp.json()
    assert body["ok"] is False
    assert body["database"] == "error"


def test_readyz_returns_503_when_storage_is_unreadable(test_env, monkeypatch):
    from pathlib import Path

    from app.config import Settings

    monkeypatch.setattr(
        Settings, "local_storage_dir_resolved", property(lambda self: Path("/definitely/does/not/exist/anywhere"))
    )

    resp = client.get("/readyz")
    assert resp.status_code == 503
    body = resp.json()
    assert body["ok"] is False
    assert body["storage"] == "error"


def test_readyz_fails_when_nothing_is_retrievable(test_env):
    resp = client.get("/readyz")
    assert resp.status_code == 503
    assert resp.json()["corpus"] == "unusable"


def test_readyz_fails_when_the_only_document_is_unapproved(test_env):
    _seed_retrievable_document(review_status="pending")
    assert client.get("/readyz").json()["corpus"] == "unusable"


def test_readyz_fails_when_the_only_link_is_unapproved(test_env):
    _seed_retrievable_document(link_status="pending")
    assert client.get("/readyz").json()["corpus"] == "unusable"


def test_readyz_fails_when_corpus_status_cannot_be_determined(test_env, monkeypatch):
    _seed_retrievable_document()
    calls = {"n": 0}
    real_get_conn = get_conn

    @contextmanager
    def flaky_get_conn():
        calls["n"] += 1
        # The database probe succeeds; the corpus query then fails.
        if calls["n"] >= 2:
            raise RuntimeError("simulated query failure")
        with real_get_conn() as conn:
            yield conn

    monkeypatch.setattr("app.main.get_conn", flaky_get_conn)
    resp = client.get("/readyz")
    assert resp.status_code == 503
    assert resp.json()["corpus"] == "error"


def test_readyz_reports_a_stuck_ingestion_run_without_failing(test_env):
    _seed_retrievable_document()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO ingestion_runs (status, started_at) VALUES ('running', now() - interval '5 hours')"
        )
    resp = client.get("/readyz")
    assert resp.status_code == 200
    assert resp.json()["ingestion"] == "stuck"


def test_readyz_response_never_includes_filenames_or_manual_content(test_env):
    """Public, unauthenticated endpoint -- the response body must stay
    limited to booleans/status strings, never a filename or anything else
    from the documents/chunks tables."""
    resp = client.get("/readyz")
    body = resp.json()
    assert set(body.keys()) == {"ok", "database", "storage", "corpus", "ingestion"}
    for value in body.values():
        assert isinstance(value, (bool, str)), f"unexpected value type in /readyz response: {value!r}"

"""P2-07 (external review, 2026-09-21): "/healthz ... cannot prove
readiness for serving a citation." GET /readyz checks the concrete
dependencies a real answer needs -- database, object storage, corpus
freshness -- instead of always reporting healthy."""
from __future__ import annotations

from contextlib import contextmanager

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_readyz_reports_ok_when_database_and_storage_are_healthy(test_env):
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


def test_readyz_response_never_includes_filenames_or_manual_content(test_env):
    """The review's own constraint: "without exposing proprietary filenames
    publicly." Public, unauthenticated endpoint -- the response body must
    stay limited to booleans/status strings, never anything from the
    documents/chunks tables."""
    resp = client.get("/readyz")
    body = resp.json()
    assert set(body.keys()) == {"ok", "database", "storage", "corpus"}
    for value in body.values():
        assert isinstance(value, (bool, str)), f"unexpected value type in /readyz response: {value!r}"

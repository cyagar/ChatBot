"""Regression coverage for the server-rendered HTML routes. A real smoke test
against a live uvicorn server caught a bug here that no API-only test did:
`TemplateResponse(name, {"request": request})` (the old positional-context
signature) crashes with the installed Starlette/Jinja2 versions
(`TypeError: cannot use 'tuple' as a dict key`) — the fix is the current
`TemplateResponse(request, name)` signature. TestClient alone wouldn't have
caught this without actually asserting on these routes, which is why they're
pinned here now."""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_index_page_renders(test_env):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "app.js" in resp.text


def test_admin_page_renders(test_env):
    resp = client.get("/admin")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "admin.js" in resp.text


def test_manifest_is_valid_json(test_env):
    resp = client.get("/manifest.webmanifest")
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"]
    assert body["icons"]


def test_service_worker_served(test_env):
    resp = client.get("/service-worker.js")
    assert resp.status_code == 200

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


def test_admin_page_renders(test_env):
    resp = client.get("/admin")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "admin.js" in resp.text


def test_technician_pwa_routes_are_gone(test_env):
    """Owner decision (2026-09-16): Android is the only technician client in
    production -- the technician PWA (index.html, app.js, service worker,
    manifest) was removed entirely. The admin web UI stays."""
    assert client.get("/").status_code == 404
    assert client.get("/manifest.webmanifest").status_code == 404
    assert client.get("/service-worker.js").status_code == 404

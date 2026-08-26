"""Phase 1 (narrowed scope, decided 2026-08-26 -- see docs/OWNER_DECISION_GATE.md
section 9): stay on the existing FastAPI app, no new Node.js/TS service, no
OAuth/PKCE, no Android refactor. This file covers the three items that
aren't the UTC-timestamp change (see tests/unit/test_iso_utc.py and
test_conversation_and_message_timestamps_carry_an_explicit_utc_offset in
test_auth_and_chat.py for that one): the safe-error envelope, GET
/api/config, and GET /api/auth/me's new capabilities field. Cursor
pagination is covered in test_machines.py/test_auth_and_chat.py alongside
the endpoints it was added to, not duplicated here.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app
from tests.conftest import register_test_user

client = TestClient(app)


def test_404_error_body_has_the_full_envelope_and_keeps_detail_for_old_consumers(test_env):
    register_test_user(client, "phase1-errors@example.com", role="technician")
    resp = client.get("/api/conversations/999999/messages")
    assert resp.status_code == 404
    body = resp.json()
    # `detail` kept, unchanged in meaning, for app.js/admin.js which already
    # read it -- neither should have to change for this.
    assert body["detail"] == "Conversation not found."
    assert body["message"] == "Conversation not found."
    assert body["code"] == "NOT_FOUND"
    assert body["status"] == 404
    assert body["retryable"] is False
    assert body["field_errors"] == []
    assert isinstance(body["correlation_id"], str) and body["correlation_id"]


def test_correlation_id_in_body_matches_the_response_header(test_env):
    resp = client.get("/api/conversations/999999/messages")  # unauthenticated -> 401
    assert resp.status_code == 401
    assert resp.headers["X-Correlation-ID"] == resp.json()["correlation_id"]


def test_correlation_id_header_present_on_success_too(test_env):
    resp = client.get("/api/config")
    assert resp.status_code == 200
    assert resp.headers.get("X-Correlation-ID")


def test_422_validation_error_has_string_detail_and_structured_field_errors(test_env):
    register_test_user(client, "phase1-422@example.com", role="technician")
    # content is required with min_length=1 -- omit it entirely.
    resp = client.post("/api/conversations", json={"machine_id": "not-an-int"})
    assert resp.status_code == 422
    body = resp.json()
    assert body["code"] == "VALIDATION_ERROR"
    assert isinstance(body["detail"], str)  # FastAPI's own default here is a list, not a string
    assert isinstance(body["message"], str)
    assert len(body["field_errors"]) >= 1
    assert "field" in body["field_errors"][0] and "message" in body["field_errors"][0]
    assert body["retryable"] is False


def test_rate_limit_error_body_matches_the_same_envelope_and_is_retryable(test_env, monkeypatch):
    # Same technique as test_rate_limit_enforced in test_auth_and_chat.py --
    # @limiter.limit(default_limit_string) binds that function reference at
    # decoration time, so patching app.rate_limit.default_limit_string
    # itself has no effect; the settings value it reads does.
    monkeypatch.setenv("RATE_LIMIT_PER_MINUTE", "1")
    from app.config import get_settings
    get_settings.cache_clear()

    register_test_user(client, "phase1-ratelimit@example.com", role="technician")
    conv = client.post("/api/conversations", json={"machine_id": None}).json()
    statuses_and_bodies = [
        client.post(f"/api/conversations/{conv['id']}/messages", json={"content": f"question {i}"})
        for i in range(4)
    ]
    limited = next((r for r in statuses_and_bodies if r.status_code == 429), None)
    assert limited is not None, f"expected a 429 among {[r.status_code for r in statuses_and_bodies]}"
    body = limited.json()
    assert body["code"] == "RATE_LIMITED"
    assert body["retryable"] is True
    assert isinstance(body["detail"], str)


def test_config_is_public_and_has_the_documented_shape(test_env):
    fresh_client = TestClient(app)  # no session cookie at all
    resp = fresh_client.get("/api/config")
    assert resp.status_code == 200
    body = resp.json()
    assert body["maintenance_mode"] is False
    assert body["feature_flags"] == {}
    assert isinstance(body["minimum_supported_version"], str) and body["minimum_supported_version"]
    assert isinstance(body["support_contact"], str) and body["support_contact"]
    assert body["status"] == "ok"  # google_drive_folder_id is blank in test_env -- no corpus-freshness concept applies


def test_config_reflects_maintenance_mode(test_env, monkeypatch):
    from app.config import get_settings
    monkeypatch.setenv("MAINTENANCE_MODE", "true")
    monkeypatch.setenv("MAINTENANCE_MESSAGE", "Upgrading the database, back in 10 minutes.")
    get_settings.cache_clear()
    try:
        resp = client.get("/api/config")
        body = resp.json()
        assert body["maintenance_mode"] is True
        assert body["status"] == "degraded"
        assert body["status_message"] == "Upgrading the database, back in 10 minutes."
    finally:
        get_settings.cache_clear()


def test_me_capabilities_differ_by_role(test_env):
    register_test_user(client, "phase1-tech@example.com", role="technician")
    tech_caps = client.get("/api/auth/me").json()["capabilities"]
    assert "ask_questions" in tech_caps
    assert "manage_users" not in tech_caps

    register_test_user(client, "bootstrap-admin@example.com", role="administrator")
    admin_caps = client.get("/api/auth/me").json()["capabilities"]
    assert "ask_questions" in admin_caps  # admins can still use the technician app (P0A-1, owner-approved)
    assert "manage_users" in admin_caps
    assert "manage_invitations" in admin_caps

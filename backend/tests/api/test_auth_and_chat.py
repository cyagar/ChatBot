import pytest
from fastapi.testclient import TestClient

from app.db import get_conn
from app.main import app

client = TestClient(app)


def _register(email="tech1@example.com", password="password123"):
    return client.post("/api/auth/register", json={"email": email, "password": password})


def test_first_registered_user_becomes_administrator(test_env):
    resp = _register()
    assert resp.status_code == 201
    assert resp.json()["role"] == "administrator"


def test_second_registered_user_is_technician(test_env):
    _register("admin@example.com")
    resp = _register("tech2@example.com")
    assert resp.status_code == 201
    assert resp.json()["role"] == "technician"


def test_duplicate_email_registration_rejected(test_env):
    _register("dup@example.com")
    resp = _register("dup@example.com")
    assert resp.status_code == 409


def test_login_wrong_password_rejected(test_env):
    _register("user@example.com", "correct-password")
    resp = client.post("/api/auth/login", json={"email": "user@example.com", "password": "wrong-password"})
    assert resp.status_code == 401


def test_unauthenticated_request_rejected(test_env):
    fresh_client = TestClient(app)  # no session cookie
    resp = fresh_client.post("/api/conversations", json={"machine_id": None})
    assert resp.status_code == 401


def test_conversation_without_machine_asks_clarifying_question(test_env):
    _register("tech3@example.com")
    conv = client.post("/api/conversations", json={"machine_id": None}).json()
    resp = client.post(f"/api/conversations/{conv['id']}/messages", json={"content": "Why won't it heat up?"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["is_clarifying_question"] is True
    assert body["is_no_answer"] is False


def test_question_on_machine_with_no_manuals_is_honest_no_answer(test_env):
    with get_conn() as conn:
        conn.execute("INSERT INTO manufacturers (id, name) VALUES (1, 'Bunn-O-Matic Corporation')")
        conn.execute(
            "INSERT INTO machines (id, manufacturer_id, model_name) VALUES (1, 1, 'Axiom')"
        )

    _register("tech4@example.com")
    conv = client.post("/api/conversations", json={"machine_id": 1}).json()
    resp = client.post(f"/api/conversations/{conv['id']}/messages", json={"content": "What does error E4 mean?"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["is_no_answer"] is True
    assert "verify" in body["content"].lower() or "not" in body["content"].lower()


def test_empty_question_rejected(test_env):
    _register("tech5@example.com")
    conv = client.post("/api/conversations", json={"machine_id": None}).json()
    resp = client.post(f"/api/conversations/{conv['id']}/messages", json={"content": "   "})
    assert resp.status_code == 422


def test_cannot_access_another_users_conversation(test_env):
    _register("owner@example.com", "password123")
    conv = client.post("/api/conversations", json={"machine_id": None}).json()
    client.post("/api/auth/logout")

    _register("intruder@example.com", "password123")
    resp = client.get(f"/api/conversations/{conv['id']}/messages")
    assert resp.status_code == 404


def test_admin_endpoint_forbidden_for_technician(test_env):
    _register("admin2@example.com")  # first user -> administrator
    client.post("/api/auth/logout")
    _register("plaintech@example.com")  # second user -> technician
    resp = client.get("/api/admin/documents")
    assert resp.status_code == 403


def test_admin_endpoint_allowed_for_administrator(test_env):
    _register("admin3@example.com")
    resp = client.get("/api/admin/documents")
    assert resp.status_code == 200


def test_feedback_rejects_invalid_rating(test_env):
    _register("tech6@example.com")
    conv = client.post("/api/conversations", json={"machine_id": None}).json()
    msg = client.post(f"/api/conversations/{conv['id']}/messages", json={"content": "test"}).json()
    resp = client.post(f"/api/messages/{msg['id']}/feedback", json={"rating": "not_a_real_rating"})
    assert resp.status_code == 422


def test_rate_limit_enforced(test_env, monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_PER_MINUTE", "2")
    from app.config import get_settings
    get_settings.cache_clear()

    _register("ratelimited@example.com")
    conv = client.post("/api/conversations", json={"machine_id": None}).json()

    statuses = [
        client.post(f"/api/conversations/{conv['id']}/messages", json={"content": f"question {i}"}).status_code
        for i in range(4)
    ]
    assert 429 in statuses, f"expected a 429 among {statuses} after exceeding the 2/minute limit"

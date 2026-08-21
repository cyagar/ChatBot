import pytest
from fastapi.testclient import TestClient

from app.db import get_conn
from app.main import app
from tests.conftest import register_test_user

client = TestClient(app)


def _register(email="tech1@example.com", password="password123"):
    return register_test_user(client, email, role="technician", password=password)


def test_registration_without_invite_is_rejected(test_env):
    """Independent follow-up review P0-5: public self-registration used to
    always succeed (the first registrant even became administrator). Now a
    request with no invite_token at all must fail validation, and critically
    must not create a user row -- a 4xx alone doesn't prove that."""
    resp = client.post("/api/auth/register", json={"email": "uninvited@example.com", "password": "password123"})
    assert resp.status_code == 422

    with get_conn() as conn:
        row = conn.execute("SELECT id FROM users WHERE email = ?", ("uninvited@example.com",)).fetchone()
    assert row is None


def test_registration_with_bogus_invite_token_is_rejected(test_env):
    resp = client.post(
        "/api/auth/register",
        json={"email": "nope@example.com", "password": "password123", "invite_token": "not-a-real-token"},
    )
    assert resp.status_code == 403
    with get_conn() as conn:
        row = conn.execute("SELECT id FROM users WHERE email = ?", ("nope@example.com",)).fetchone()
    assert row is None


def test_invite_is_bound_to_its_email_and_single_use(test_env):
    register_test_user(client, "bootstrap-admin@example.com", role="administrator")
    invite = client.post(
        "/api/admin/invitations", json={"email": "invited@example.com", "role": "technician"}
    ).json()
    token = invite["token"]
    client.post("/api/auth/logout")

    # Wrong email for this token.
    wrong_email = client.post(
        "/api/auth/register",
        json={"email": "someone-else@example.com", "password": "password123", "invite_token": token},
    )
    assert wrong_email.status_code == 403

    # Correct email consumes it.
    ok = client.post(
        "/api/auth/register",
        json={"email": "invited@example.com", "password": "password123", "invite_token": token},
    )
    assert ok.status_code == 201
    assert ok.json()["role"] == "technician"
    client.post("/api/auth/logout")

    # Same token again, even with the right email, must fail -- single-use.
    reuse = client.post(
        "/api/auth/register",
        json={"email": "invited@example.com", "password": "password123", "invite_token": token},
    )
    assert reuse.status_code == 403


def test_bootstrap_admin_refuses_once_a_user_exists(test_env):
    from app.auth.bootstrap import bootstrap_admin

    bootstrap_admin("first-admin@example.com", "password123")
    with pytest.raises(RuntimeError, match="Refusing to bootstrap"):
        bootstrap_admin("second-admin@example.com", "password123")


def test_duplicate_email_registration_rejected(test_env):
    """Defense-in-depth check at the register endpoint itself, independent of
    invite creation already refusing to issue an invite for an email that has
    an account (covered by test_invite_creation_rejects_existing_email)."""
    register_test_user(client, "bootstrap-admin@example.com", role="administrator")
    invite = client.post(
        "/api/admin/invitations", json={"email": "raceduplicate@example.com", "role": "technician"}
    ).json()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO users (email, password_hash, role) VALUES (?, 'x', 'technician')",
            ("raceduplicate@example.com",),
        )
    client.post("/api/auth/logout")
    resp = client.post(
        "/api/auth/register",
        json={"email": "raceduplicate@example.com", "password": "password123", "invite_token": invite["token"]},
    )
    assert resp.status_code == 409


def test_disabled_user_cannot_log_in_or_use_an_existing_session(test_env):
    register_test_user(client, "bootstrap-admin@example.com", role="administrator")
    reg = register_test_user(client, "todisable@example.com")
    user_id = reg.json()["id"]

    # A second client holds the disabled-to-be user's own session cookie,
    # captured before the admin disables them, so we can prove an ALREADY
    # ISSUED token stops working -- not just that a fresh login is blocked
    # (independent follow-up review P0-5: "session revocation").
    tech_client = TestClient(app)
    tech_client.cookies.set("tma_session", client.cookies.get("tma_session"))
    assert tech_client.get("/api/auth/me").status_code == 200

    register_test_user(client, "bootstrap-admin@example.com", role="administrator")
    disable_resp = client.post(f"/api/admin/users/{user_id}/disable")
    assert disable_resp.status_code == 200

    assert tech_client.get("/api/auth/me").status_code == 401, (
        "a session token issued before disable must stop working immediately, not just at its natural expiry"
    )

    login_resp = client.post("/api/auth/login", json={"email": "todisable@example.com", "password": "password123"})
    assert login_resp.status_code == 401


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
    _register("plaintech@example.com")
    resp = client.get("/api/admin/documents")
    assert resp.status_code == 403


def test_admin_endpoint_allowed_for_administrator(test_env):
    register_test_user(client, "bootstrap-admin@example.com", role="administrator")
    resp = client.get("/api/admin/documents")
    assert resp.status_code == 200


def test_feedback_rejects_invalid_rating(test_env):
    _register("tech6@example.com")
    conv = client.post("/api/conversations", json={"machine_id": None}).json()
    msg = client.post(f"/api/conversations/{conv['id']}/messages", json={"content": "test"}).json()
    resp = client.post(f"/api/messages/{msg['id']}/feedback", json={"rating": "not_a_real_rating"})
    assert resp.status_code == 422


def test_save_and_list_saved_answer_roundtrip(test_env):
    _register("tech7@example.com")
    conv = client.post("/api/conversations", json={"machine_id": None}).json()
    msg = client.post(f"/api/conversations/{conv['id']}/messages", json={"content": "test question"}).json()

    save_resp = client.post(f"/api/messages/{msg['id']}/save")
    assert save_resp.status_code == 201

    list_resp = client.get("/api/saved-answers")
    assert list_resp.status_code == 200
    saved = list_resp.json()
    assert any(m["id"] == msg["id"] for m in saved)


def test_confirm_machine_endpoint_sets_and_persists_machine(test_env):
    with get_conn() as conn:
        conn.execute("INSERT INTO manufacturers (id, name) VALUES (1, 'Bunn-O-Matic Corporation')")
        conn.execute("INSERT INTO machines (id, manufacturer_id, model_name) VALUES (1, 1, 'Axiom')")

    _register("tech8@example.com")
    conv = client.post("/api/conversations", json={"machine_id": None}).json()
    assert conv["machine_id"] is None

    resp = client.post(f"/api/conversations/{conv['id']}/machine", json={"machine_id": 1})
    assert resp.status_code == 200
    body = resp.json()
    assert body["machine_id"] == 1
    assert "Axiom" in body["machine_label"]

    # A question now resolves straight through -- no clarifying question,
    # since the machine was set via the explicit endpoint, not inferred.
    ask = client.post(f"/api/conversations/{conv['id']}/messages", json={"content": "What does error E4 mean?"})
    assert ask.status_code == 200
    assert ask.json()["is_clarifying_question"] is False


def test_confirm_machine_rejects_unknown_machine(test_env):
    _register("tech9@example.com")
    conv = client.post("/api/conversations", json={"machine_id": None}).json()
    resp = client.post(f"/api/conversations/{conv['id']}/machine", json={"machine_id": 999})
    assert resp.status_code == 404


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

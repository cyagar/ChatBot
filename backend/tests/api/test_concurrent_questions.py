"""Owner decision (2026-09-16): concurrent questions in one conversation are
not supported -- a technician must wait for (or stop) an in-flight question
before asking another, enforced server-side via
conversations.is_processing (app/api/routes_chat.py's
_claim_conversation_processing/_release_conversation_processing), not just a
disabled client button.
"""
from __future__ import annotations

import threading

from fastapi.testclient import TestClient

from app.db import get_conn
from app.main import app
from tests.conftest import register_test_user

client = TestClient(app)


def _register(email):
    return register_test_user(client, email, role="technician")


def _seed_machine(conn):
    conn.execute("INSERT INTO manufacturers (name) VALUES ('Bunn-O-Matic Corporation')")
    conn.execute("INSERT INTO machines (manufacturer_id, model_name) VALUES (1, 'Axiom')")


def _blocking_hybrid_search(monkeypatch):
    """Patches routes_chat.hybrid_search to block until released, so a test
    can reliably observe a request mid-flight instead of racing a fast real
    call."""
    import app.api.routes_chat as routes_chat

    entered = threading.Event()
    release = threading.Event()

    def blocked(*args, **kwargs):
        entered.set()
        release.wait(timeout=5)
        return []

    monkeypatch.setattr(routes_chat, "hybrid_search", blocked)
    return entered, release


def test_second_question_is_rejected_while_the_first_is_in_flight(test_env, monkeypatch):
    with get_conn() as conn:
        _seed_machine(conn)
    entered, release = _blocking_hybrid_search(monkeypatch)
    _register("busyconv@example.com")
    conv = client.post("/api/conversations", json={"machine_id": 1}).json()

    responses = []

    def ask_first():
        responses.append(
            client.post(f"/api/conversations/{conv['id']}/messages", json={"content": "first question"})
        )

    t = threading.Thread(target=ask_first)
    t.start()
    assert entered.wait(timeout=5), "first request never reached retrieval"

    second = client.post(f"/api/conversations/{conv['id']}/messages", json={"content": "second question"})
    assert second.status_code == 409

    release.set()
    t.join(timeout=5)

    assert responses[0].status_code == 200

    messages = client.get(f"/api/conversations/{conv['id']}/messages").json()
    user_messages = [m for m in messages if m["role"] == "user"]
    assert len(user_messages) == 1, "the rejected second question must not create a user turn"
    assert user_messages[0]["content"] == "first question"


def test_a_question_is_askable_again_once_the_lock_is_released(test_env, monkeypatch):
    with get_conn() as conn:
        _seed_machine(conn)
    _register("releaseconv@example.com")
    conv = client.post("/api/conversations", json={"machine_id": 1}).json()

    first = client.post(f"/api/conversations/{conv['id']}/messages", json={"content": "first question"})
    assert first.status_code == 200

    second = client.post(f"/api/conversations/{conv['id']}/messages", json={"content": "second question"})
    assert second.status_code == 200

    with get_conn() as conn:
        is_processing = conn.execute(
            "SELECT is_processing FROM conversations WHERE id = %s", (conv["id"],)
        ).fetchone()["is_processing"]
    assert is_processing is False


def test_retry_and_a_new_question_cannot_run_concurrently(test_env, monkeypatch):
    """A retry also calls the provider, so it must hold the same lock a
    fresh question does -- a technician cannot ask something new while a
    retry of an earlier failed answer is in flight."""
    with get_conn() as conn:
        _seed_machine(conn)
    import app.api.routes_chat as routes_chat

    real_hybrid_search = routes_chat.hybrid_search
    state = {"should_fail": True}

    def flaky(*args, **kwargs):
        if state["should_fail"]:
            raise RuntimeError("simulated retrieval failure")
        return real_hybrid_search(*args, **kwargs)

    monkeypatch.setattr(routes_chat, "hybrid_search", flaky)

    _register("retryblocksnew@example.com")
    conv = client.post("/api/conversations", json={"machine_id": 1}).json()
    ask = client.post(f"/api/conversations/{conv['id']}/messages", json={"content": "first question"})
    failed_id = ask.json()["id"]
    assert ask.json()["answer_status"] == "failed"

    entered, release = _blocking_hybrid_search(monkeypatch)
    responses = []

    def do_retry():
        responses.append(client.post(f"/api/conversations/{conv['id']}/messages/{failed_id}/retry"))

    t = threading.Thread(target=do_retry)
    t.start()
    assert entered.wait(timeout=5), "retry never reached retrieval"

    blocked_new_question = client.post(
        f"/api/conversations/{conv['id']}/messages", json={"content": "a totally different question"}
    )
    assert blocked_new_question.status_code == 409

    release.set()
    t.join(timeout=5)
    assert responses[0].status_code == 200

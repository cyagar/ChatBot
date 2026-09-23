"""POST /conversations/{id}/messages/{message_id}/retry regenerates a failed
assistant message IN PLACE -- these tests prove no new user turn, no new
assistant message, idempotency under a double-tap (the same claim-UPDATE
pattern already proven for pending_message_id in test_multiturn.py), and
that only a genuinely failed answer is retryable.
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


def _seed_heater_chunk(conn):
    cur = conn.execute(
        "INSERT INTO documents (original_filename, storage_path, source_system, source_ref, "
        "file_type, sha256, byte_size, status, review_status) VALUES "
        "('axiom.pdf', 'axiom.pdf', 'local_directory', 'axiom.pdf', 'pdf', 'hash1', 100, "
        "'indexed', 'approved') RETURNING id"
    )
    doc_id = cur.fetchone()["id"]
    conn.execute("INSERT INTO document_machines (document_id, machine_id, review_status) VALUES (%s, 1, 'approved')", (doc_id,))
    conn.execute(
        "INSERT INTO chunks (document_id, page_number, chunk_type, content, char_count, ordinal) "
        "VALUES (%s, 4, 'text', "
        "'TANK HEATER FAILURE CHECK: If the Axiom brewer is not heating, check the tank heater "
        "and thermistor circuit for continuity.', 140, 0)",
        (doc_id,),
    )


def _make_failing_then_recovering_search(monkeypatch):
    """Patches routes_chat.hybrid_search to raise until flipped off, so a
    real 'failed' answer_status can be produced, then a real, different
    successful answer on retry -- proving actual regeneration, not just a
    status flip."""
    import app.api.routes_chat as routes_chat
    real_hybrid_search = routes_chat.hybrid_search
    state = {"should_fail": True}

    def flaky(*args, **kwargs):
        if state["should_fail"]:
            raise RuntimeError("simulated retrieval failure")
        return real_hybrid_search(*args, **kwargs)

    monkeypatch.setattr(routes_chat, "hybrid_search", flaky)
    return state


def test_retry_regenerates_a_failed_answer_in_place(test_env, monkeypatch):
    with get_conn() as conn:
        _seed_machine(conn)
        _seed_heater_chunk(conn)
    state = _make_failing_then_recovering_search(monkeypatch)

    _register("retry1@example.com")
    conv = client.post("/api/conversations", json={"machine_id": 1}).json()
    ask = client.post(f"/api/conversations/{conv['id']}/messages", json={"content": "Why is it not heating?"})
    assert ask.json()["answer_status"] == "failed"
    failed_id = ask.json()["id"]
    original_content = ask.json()["content"]

    state["should_fail"] = False
    retried = client.post(f"/api/conversations/{conv['id']}/messages/{failed_id}/retry")
    assert retried.status_code == 200
    body = retried.json()

    assert body["id"] == failed_id, "retry updates the same message id, it does not create a new one"
    assert body["answer_status"] != "failed"
    assert body["content"] != original_content, "the retried answer must actually be regenerated, not just re-labeled"
    assert body["retry_count"] == 1

    messages = client.get(f"/api/conversations/{conv['id']}/messages").json()
    assert len(messages) == 2, "exactly the original user question and one assistant message -- no duplicate turns"
    assert messages[0]["role"] == "user"
    assert messages[0]["content"] == "Why is it not heating?"
    assert messages[1]["id"] == failed_id


def test_retry_rejects_a_completed_answer(test_env, monkeypatch):
    with get_conn() as conn:
        _seed_machine(conn)
        _seed_heater_chunk(conn)

    _register("retry2@example.com")
    conv = client.post("/api/conversations", json={"machine_id": 1}).json()
    ask = client.post(f"/api/conversations/{conv['id']}/messages", json={"content": "Why is it not heating?"})
    assert ask.json()["answer_status"] != "failed"

    resp = client.post(f"/api/conversations/{conv['id']}/messages/{ask.json()['id']}/retry")
    assert resp.status_code == 409


def test_retry_rejects_a_user_message(test_env):
    with get_conn() as conn:
        _seed_machine(conn)
        _seed_heater_chunk(conn)

    _register("retry3@example.com")
    conv = client.post("/api/conversations", json={"machine_id": 1}).json()
    ask = client.post(f"/api/conversations/{conv['id']}/messages", json={"content": "Why is it not heating?"})
    user_message_id = [m for m in client.get(f"/api/conversations/{conv['id']}/messages").json()
                        if m["role"] == "user"][0]["id"]

    resp = client.post(f"/api/conversations/{conv['id']}/messages/{user_message_id}/retry")
    assert resp.status_code == 404


def test_retry_requires_ownership(test_env, monkeypatch):
    with get_conn() as conn:
        _seed_machine(conn)
        _seed_heater_chunk(conn)
    state = _make_failing_then_recovering_search(monkeypatch)

    _register("owner@example.com")
    conv = client.post("/api/conversations", json={"machine_id": 1}).json()
    ask = client.post(f"/api/conversations/{conv['id']}/messages", json={"content": "Why is it not heating?"})
    failed_id = ask.json()["id"]
    state["should_fail"] = False

    _register("intruder@example.com")
    resp = client.post(f"/api/conversations/{conv['id']}/messages/{failed_id}/retry")
    assert resp.status_code == 404


def test_concurrent_retry_does_not_duplicate_or_double_call_the_provider(test_env, monkeypatch):
    """A double-tap on the retry button fires two near-simultaneous requests.
    Only one may claim the 'failed' -> 'retrying' transition; the other must
    see a 409, not trigger a second provider call or a second message."""
    with get_conn() as conn:
        _seed_machine(conn)
        _seed_heater_chunk(conn)
    state = _make_failing_then_recovering_search(monkeypatch)

    _register("doubletapretry@example.com")
    conv = client.post("/api/conversations", json={"machine_id": 1}).json()
    ask = client.post(f"/api/conversations/{conv['id']}/messages", json={"content": "Why is it not heating?"})
    failed_id = ask.json()["id"]
    state["should_fail"] = False

    responses = []

    def retry():
        responses.append(client.post(f"/api/conversations/{conv['id']}/messages/{failed_id}/retry"))

    threads = [threading.Thread(target=retry) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    statuses = sorted(r.status_code for r in responses)
    assert statuses == [200, 409], "exactly one retry succeeds, the other is rejected as already in progress"

    messages = client.get(f"/api/conversations/{conv['id']}/messages").json()
    assert len(messages) == 2, "no duplicate assistant message from the losing concurrent retry"

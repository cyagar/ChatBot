"""An answer is tied to the exact question it answers (reply_to_message_id),
and a replayed Idempotency-Key can only ever return, resume, or refuse its own
question -- never another turn's answer."""
from __future__ import annotations

import psycopg
import pytest
from fastapi.testclient import TestClient

from app.db import get_conn
from app.main import app
from tests.api.test_auth_and_chat import _ANSWERABLE_QUESTION, _seed_answerable_machine
from tests.conftest import register_test_user

client = TestClient(app)


def _conversation() -> int:
    register_test_user(client, "attempts@example.com", role="technician")
    _seed_answerable_machine()
    return client.post("/api/conversations", json={"machine_id": 1}).json()["id"]


def _ask(conv_id: int, text: str, key: str | None):
    headers = {"Idempotency-Key": key} if key else {}
    return client.post(f"/api/conversations/{conv_id}/messages", json={"content": text}, headers=headers)


def _orphan_question(conv_id: int, text: str, key: str) -> int:
    """A user turn committed by an attempt that died before writing a reply."""
    with get_conn() as conn:
        return conn.execute(
            "INSERT INTO messages (conversation_id, role, content, idempotency_key) "
            "VALUES (%s, 'user', %s, %s) RETURNING id",
            (conv_id, text, key),
        ).fetchone()["id"]


def _hold_lease(conv_id: int) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE conversations SET is_processing = true, processing_attempt_id = 'other-worker', "
            "processing_claimed_at = now() WHERE id = %s",
            (conv_id,),
        )


def _assistant_rows(conv_id: int):
    with get_conn() as conn:
        return conn.execute(
            "SELECT id, reply_to_message_id FROM messages WHERE conversation_id = %s AND role = 'assistant' "
            "ORDER BY id",
            (conv_id,),
        ).fetchall()


def test_an_answer_records_the_question_it_answers(test_env):
    conv_id = _conversation()
    answer = _ask(conv_id, _ANSWERABLE_QUESTION, "k1").json()

    with get_conn() as conn:
        user_id = conn.execute(
            "SELECT id FROM messages WHERE conversation_id = %s AND role = 'user'", (conv_id,)
        ).fetchone()["id"]
    assert answer["reply_to_message_id"] == user_id

    listed = client.get(f"/api/conversations/{conv_id}/messages").json()
    assert [m["reply_to_message_id"] for m in listed if m["role"] == "assistant"] == [user_id]
    assert [m["idempotency_key"] for m in listed if m["role"] == "user"] == ["k1"]


def test_the_same_key_with_a_different_question_is_refused(test_env):
    conv_id = _conversation()
    assert _ask(conv_id, _ANSWERABLE_QUESTION, "k1").status_code == 200

    resp = _ask(conv_id, "something else entirely", "k1")
    assert resp.status_code == 409
    assert resp.json()["code"] == "IDEMPOTENCY_PAYLOAD_MISMATCH"
    assert resp.json()["retryable"] is False


def test_a_dead_attempt_is_resumed_under_its_own_key_and_answered_once(test_env):
    conv_id = _conversation()
    question_id = _orphan_question(conv_id, _ANSWERABLE_QUESTION, "orphan-key")

    first = _ask(conv_id, _ANSWERABLE_QUESTION, "orphan-key")
    assert first.status_code == 200
    assert first.json()["reply_to_message_id"] == question_id

    again = _ask(conv_id, _ANSWERABLE_QUESTION, "orphan-key")
    assert again.status_code == 200
    assert again.json()["id"] == first.json()["id"]
    assert [r["reply_to_message_id"] for r in _assistant_rows(conv_id)] == [question_id]
    with get_conn() as conn:
        assert conn.execute(
            "SELECT count(*) AS n FROM messages WHERE conversation_id = %s AND role = 'user'", (conv_id,)
        ).fetchone()["n"] == 1


def test_a_live_attempt_reports_in_progress_not_a_new_answer(test_env):
    conv_id = _conversation()
    _orphan_question(conv_id, _ANSWERABLE_QUESTION, "live-key")
    _hold_lease(conv_id)

    resp = _ask(conv_id, _ANSWERABLE_QUESTION, "live-key")
    assert resp.status_code == 409
    assert resp.json()["code"] == "IDEMPOTENCY_IN_PROGRESS"
    assert resp.json()["retryable"] is True
    assert _assistant_rows(conv_id) == []


def test_a_different_question_while_busy_is_a_busy_conversation_not_an_idempotent_replay(test_env):
    conv_id = _conversation()
    _hold_lease(conv_id)

    resp = _ask(conv_id, _ANSWERABLE_QUESTION, "new-key")
    assert resp.status_code == 409
    assert resp.json()["code"] == "CONVERSATION_BUSY"
    with get_conn() as conn:
        assert conn.execute(
            "SELECT count(*) AS n FROM messages WHERE conversation_id = %s", (conv_id,)
        ).fetchone()["n"] == 0


def test_replaying_q1_after_q2_was_answered_never_returns_q2s_answer(test_env):
    """Q1 is committed and its worker dies; after the lease frees, Q2 is
    accepted and answered. Replaying Q1's key must not hand back Q2's answer."""
    conv_id = _conversation()
    q1 = _orphan_question(conv_id, _ANSWERABLE_QUESTION, "key-a")

    q2 = _ask(conv_id, _ANSWERABLE_QUESTION + " again", "key-b")
    assert q2.status_code == 200

    replay = _ask(conv_id, _ANSWERABLE_QUESTION, "key-a")
    assert replay.status_code == 409
    assert replay.json()["code"] == "IDEMPOTENCY_SUPERSEDED"
    assert q2.json()["reply_to_message_id"] != q1
    assert [r["reply_to_message_id"] for r in _assistant_rows(conv_id)] == [q2.json()["reply_to_message_id"]]


def test_the_database_allows_one_real_answer_per_question(test_env):
    conv_id = _conversation()
    answer = _ask(conv_id, _ANSWERABLE_QUESTION, "k1").json()

    with pytest.raises(psycopg.errors.UniqueViolation):
        with get_conn() as conn:
            conn.execute(
                "INSERT INTO messages (conversation_id, role, content, reply_to_message_id) "
                "VALUES (%s, 'assistant', 'second answer', %s)",
                (conv_id, answer["reply_to_message_id"]),
            )


def test_retry_regenerates_the_answer_for_its_own_question(test_env, monkeypatch):
    import app.api.routes_chat as routes_chat

    conv_id = _conversation()
    real_search = routes_chat.hybrid_search
    fail = {"on": True}

    def flaky(*args, **kwargs):
        if fail["on"]:
            raise RuntimeError("simulated retrieval failure")
        return real_search(*args, **kwargs)

    monkeypatch.setattr(routes_chat, "hybrid_search", flaky)
    failed = _ask(conv_id, _ANSWERABLE_QUESTION, "k1").json()
    assert failed["answer_status"] == "failed"

    # A later question lands between the failed answer and its retry.
    fail["on"] = False
    assert _ask(conv_id, "unrelated follow up", "k2").status_code == 200

    retried = client.post(f"/api/conversations/{conv_id}/messages/{failed['id']}/retry")
    assert retried.status_code == 200
    assert retried.json()["id"] == failed["id"]
    assert retried.json()["reply_to_message_id"] == failed["reply_to_message_id"]


def test_the_latest_page_of_a_long_conversation_can_be_paged_backward(test_env):
    conv_id = _conversation()
    with get_conn() as conn:
        for i in range(25):
            conn.execute(
                "INSERT INTO messages (conversation_id, role, content) VALUES (%s, 'user', %s)",
                (conv_id, f"message {i}"),
            )

    newest = client.get(f"/api/conversations/{conv_id}/messages", params={"latest": "true", "limit": 10})
    assert [m["content"] for m in newest.json()] == [f"message {i}" for i in range(15, 25)]
    assert newest.headers["X-Has-More"] == "true"

    older = client.get(
        f"/api/conversations/{conv_id}/messages",
        params={"limit": 10, "before": newest.headers["X-Next-Cursor"]},
    )
    assert [m["content"] for m in older.json()] == [f"message {i}" for i in range(5, 15)]

    oldest = client.get(
        f"/api/conversations/{conv_id}/messages",
        params={"limit": 10, "before": older.headers["X-Next-Cursor"]},
    )
    assert [m["content"] for m in oldest.json()] == [f"message {i}" for i in range(0, 5)]
    assert oldest.headers["X-Has-More"] == "false"

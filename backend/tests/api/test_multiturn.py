"""P1-8 (independent follow-up review): pending-message resumption and
standalone-query resolution, exercised end to end through the real chat API
with the real (local_extractive, no API key) default provider.

No embeddings rows are seeded in any test here, so vector_search's own
early-return (P1-5) means these never load the embedding model -- lexical/FTS
matching alone is enough to prove both behaviors and keeps the suite fast.
"""
from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

from app.db import get_conn
from app.main import app
from tests.conftest import register_test_user

client = TestClient(app)


def _embed_seeded_chunks():
    """Real embeddings for the chunks seeded by _seed_axiom_with_heater_chunks
    -- local_extractive's relevance gate requires an actual vector_score (see
    MIN_VECTOR_SIMILARITY_FOR_ANSWER in app/providers/extractive.py), so
    without this both test chunks fail the gate and the provider returns its
    generic "nothing clearly answers" template instead of quoting either
    chunk -- which would give the resolver no real content words to chain
    forward, defeating the point of this test."""
    from app.retrieval.embeddings import embed_texts, vector_to_blob

    with get_conn() as conn:
        rows = conn.execute("SELECT id, content FROM chunks ORDER BY id").fetchall()
        vectors = embed_texts([r["content"] for r in rows])
        for row, vec in zip(rows, vectors):
            conn.execute(
                "INSERT INTO embeddings (chunk_id, model_name, dim, vector) VALUES (?, ?, ?, ?)",
                (row["id"], "test-model", len(vec), vector_to_blob(vec)),
            )


def _register(email):
    return register_test_user(client, email, role="technician")


def _seed_axiom_with_heater_chunks(conn):
    conn.execute("INSERT INTO manufacturers (id, name) VALUES (1, 'Bunn-O-Matic Corporation')")
    conn.execute("INSERT INTO machines (id, manufacturer_id, model_name) VALUES (1, 1, 'Axiom')")
    cur = conn.execute(
        "INSERT INTO documents (original_filename, storage_path, source_system, source_ref, "
        "file_type, sha256, byte_size, status, review_status) VALUES "
        "('axiom.pdf', 'axiom.pdf', 'local_directory', 'axiom.pdf', 'pdf', 'hash1', 100, "
        "'indexed', 'approved')"
    )
    doc_id = cur.lastrowid
    conn.execute("INSERT INTO document_machines (document_id, machine_id, review_status) VALUES (?, 1, 'approved')", (doc_id,))
    conn.execute(
        "INSERT INTO chunks (id, document_id, page_number, chunk_type, content, char_count, ordinal) "
        "VALUES (1, ?, 4, 'text', "
        "'TANK HEATER FAILURE CHECK: If the Axiom brewer is not heating, check the tank heater "
        "and thermistor circuit for continuity.', 140, 0)",
        (doc_id,),
    )
    conn.execute(
        "INSERT INTO chunks (id, document_id, page_number, chunk_type, content, char_count, ordinal) "
        "VALUES (2, ?, 5, 'text', "
        "'TANK HEATER SERVICE PROCEDURE: To install a new tank heater on the Axiom, disconnect "
        "power, remove the four screws, and unplug the wire harness connector J12.', 160, 1)",
        (doc_id,),
    )
    conn.execute("INSERT INTO chunks_fts (rowid, content) SELECT id, content FROM chunks")
    return doc_id


def test_pending_message_is_resumed_without_a_duplicate_user_turn(test_env):
    with get_conn() as conn:
        conn.execute("INSERT INTO manufacturers (id, name) VALUES (1, 'Bunn-O-Matic Corporation')")
        conn.execute("INSERT INTO machines (id, manufacturer_id, model_name) VALUES (1, 1, 'Axiom')")

    _register("resume@example.com")
    conv = client.post("/api/conversations", json={"machine_id": None}).json()

    ask = client.post(f"/api/conversations/{conv['id']}/messages", json={"content": "What does error E4 mean?"})
    assert ask.status_code == 200
    assert ask.json()["is_clarifying_question"] is True

    with get_conn() as conn:
        pending = conn.execute(
            "SELECT pending_message_id FROM conversations WHERE id = ?", (conv["id"],)
        ).fetchone()
    assert pending["pending_message_id"] is not None

    resp = client.post(f"/api/conversations/{conv['id']}/machine", json={"machine_id": 1})
    assert resp.status_code == 200

    with get_conn() as conn:
        pending_after = conn.execute(
            "SELECT pending_message_id FROM conversations WHERE id = ?", (conv["id"],)
        ).fetchone()
    assert pending_after["pending_message_id"] is None

    messages = client.get(f"/api/conversations/{conv['id']}/messages").json()
    user_messages = [m for m in messages if m["role"] == "user"]
    assert len(user_messages) == 1, "the original question must not be duplicated as a new user turn"
    assert user_messages[0]["content"] == "What does error E4 mean?"

    assistant_messages = [m for m in messages if m["role"] == "assistant"]
    assert len(assistant_messages) == 2, "clarifying question + exactly one resumed answer"
    assert assistant_messages[0]["is_clarifying_question"] is True
    assert assistant_messages[1]["is_clarifying_question"] is False


def test_concurrent_machine_confirmation_does_not_duplicate_the_answer(test_env):
    """A double-tap on a clarify-option button (easy on a tablet) fires two
    near-simultaneous POST /machine requests. Without an atomic claim on
    pending_message_id, both would read the same pending question, both call
    the provider, and both persist an assistant answer -- two answers to one
    question. Server-side, only one request may resume it."""
    with get_conn() as conn:
        conn.execute("INSERT INTO manufacturers (id, name) VALUES (1, 'Bunn-O-Matic Corporation')")
        conn.execute("INSERT INTO machines (id, manufacturer_id, model_name) VALUES (1, 1, 'Axiom')")

    _register("doubletap@example.com")
    conv = client.post("/api/conversations", json={"machine_id": None}).json()
    ask = client.post(f"/api/conversations/{conv['id']}/messages", json={"content": "What does error E4 mean?"})
    assert ask.json()["is_clarifying_question"] is True

    responses = []

    def confirm():
        responses.append(client.post(f"/api/conversations/{conv['id']}/machine", json={"machine_id": 1}))

    threads = [threading.Thread(target=confirm) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert all(r.status_code == 200 for r in responses)

    messages = client.get(f"/api/conversations/{conv['id']}/messages").json()
    user_messages = [m for m in messages if m["role"] == "user"]
    assistant_messages = [m for m in messages if m["role"] == "assistant"]
    assert len(user_messages) == 1
    assert len(assistant_messages) == 2, "clarifying question + exactly one resumed answer, even with a double-tap"

    with get_conn() as conn:
        pending = conn.execute(
            "SELECT pending_message_id FROM conversations WHERE id = ?", (conv["id"],)
        ).fetchone()
    assert pending["pending_message_id"] is None


def test_asking_a_new_question_clears_a_stale_pending_clarification(test_env):
    with get_conn() as conn:
        conn.execute("INSERT INTO manufacturers (id, name) VALUES (1, 'Bunn-O-Matic Corporation')")
        conn.execute("INSERT INTO machines (id, manufacturer_id, model_name) VALUES (1, 1, 'Axiom')")

    _register("stalepending@example.com")
    conv = client.post("/api/conversations", json={"machine_id": None}).json()

    client.post(f"/api/conversations/{conv['id']}/messages", json={"content": "What does error E4 mean?"})
    with get_conn() as conn:
        pending = conn.execute(
            "SELECT pending_message_id FROM conversations WHERE id = ?", (conv["id"],)
        ).fetchone()
    assert pending["pending_message_id"] is not None

    # The technician moves on and asks something new that resolves the
    # machine directly via the mention itself, instead of picking a
    # clarifying option.
    resp = client.post(
        f"/api/conversations/{conv['id']}/messages", json={"content": "On the Axiom, what does E4 mean?"}
    )
    assert resp.status_code == 200
    assert resp.json()["is_clarifying_question"] is False

    with get_conn() as conn:
        pending_after = conn.execute(
            "SELECT pending_message_id FROM conversations WHERE id = ?", (conv["id"],)
        ).fetchone()
    assert pending_after["pending_message_id"] is None


@pytest.mark.slow
def test_follow_up_question_persists_a_resolved_retrieval_query(test_env):
    with get_conn() as conn:
        _seed_axiom_with_heater_chunks(conn)
    _embed_seeded_chunks()

    _register("resolvedquery@example.com")
    conv = client.post("/api/conversations", json={"machine_id": 1}).json()

    client.post(f"/api/conversations/{conv['id']}/messages", json={"content": "Why is it not heating?"})
    followup = client.post(
        f"/api/conversations/{conv['id']}/messages", json={"content": "What about replacing it?"}
    )
    assert followup.status_code == 200

    with get_conn() as conn:
        rows = conn.execute(
            "SELECT content, resolved_query FROM messages WHERE conversation_id = ? AND role = 'user' ORDER BY id",
            (conv["id"],),
        ).fetchall()
    assert rows[0]["content"] == "Why is it not heating?"
    assert rows[0]["resolved_query"] is None, "a standalone first question needs no resolution"
    assert rows[1]["content"] == "What about replacing it?"
    assert rows[1]["resolved_query"] is not None
    assert rows[1]["resolved_query"].startswith("What about replacing it?")
    assert rows[1]["resolved_query"] != rows[1]["content"]


@pytest.mark.slow
def test_follow_up_resolution_changes_which_passage_retrieval_surfaces(test_env):
    """The actual point of resolution, not just that a column got populated:
    the literal follow-up wording alone doesn't lexically match the
    replacement-procedure chunk (it contains neither 'replac' nor 'it'
    meaningfully), but the resolved query -- carrying 'heater'/'thermistor'
    forward from the first answer -- does."""
    with get_conn() as conn:
        _seed_axiom_with_heater_chunks(conn)
    _embed_seeded_chunks()

    _register("retrievalchange@example.com")
    conv = client.post("/api/conversations", json={"machine_id": 1}).json()

    first = client.post(f"/api/conversations/{conv['id']}/messages", json={"content": "Why is it not heating?"})
    assert first.status_code == 200
    assert any(c["chunk_id"] == 1 for c in first.json()["citations"]), "first answer should surface the failure-check chunk"

    followup = client.post(
        f"/api/conversations/{conv['id']}/messages", json={"content": "What about replacing it?"}
    )
    assert followup.status_code == 200
    followup_chunk_ids = {c["chunk_id"] for c in followup.json()["citations"]}
    assert 2 in followup_chunk_ids, (
        "resolved query should surface the service-procedure chunk even though "
        "the follow-up's own literal wording doesn't lexically match it"
    )

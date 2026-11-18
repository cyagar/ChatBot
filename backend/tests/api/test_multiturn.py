"""Pending-message resumption and standalone-query resolution, exercised
end to end through the real chat API with the real (local_extractive, no
API key) default provider.

No embeddings rows are seeded in any test here, so vector_search's own
early-return on an empty eligible-chunk set means these never load the
embedding model -- lexical/FTS matching alone is enough to prove both
behaviors and keeps the suite fast.
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
    from app.retrieval.embeddings import embed_texts, embedding_fingerprint, vector_to_blob

    with get_conn() as conn:
        rows = conn.execute("SELECT id, content FROM chunks ORDER BY id").fetchall()
        vectors = embed_texts([r["content"] for r in rows])
        for row, vec in zip(rows, vectors):
            conn.execute(
                "INSERT INTO embeddings (chunk_id, model_name, dim, vector) VALUES (%s, %s, %s, %s)",
                (row["id"], embedding_fingerprint(), len(vec), vector_to_blob(vec)),
            )


def _register(email):
    return register_test_user(client, email, role="technician")


def _seed_axiom_with_heater_chunks(conn):
    conn.execute("INSERT INTO manufacturers (name) VALUES ('Bunn-O-Matic Corporation')")
    conn.execute("INSERT INTO machines (manufacturer_id, model_name) VALUES (1, 'Axiom')")
    cur = conn.execute(
        "INSERT INTO documents (original_filename, storage_path, source_system, source_ref, "
        "file_type, sha256, byte_size, status, review_status) VALUES "
        "('axiom.pdf', 'axiom.pdf', 'local_directory', 'axiom.pdf', 'pdf', 'hash1', 100, "
        "'indexed', 'approved') RETURNING id"
    )
    doc_id = cur.fetchone()["id"]
    conn.execute("INSERT INTO document_machines (document_id, machine_id, review_status) VALUES (%s, 1, 'approved')", (doc_id,))
    # chunk_id 1/2 assertions below (test_follow_up_resolution_changes_which_
    # passage_retrieval_surfaces) rely on these landing as ids 1 and 2 --
    # RESTART IDENTITY (tests/conftest.py) plus this fixed insertion order
    # guarantees that, same as elsewhere in this test suite.
    conn.execute(
        "INSERT INTO chunks (document_id, page_number, chunk_type, content, char_count, ordinal) "
        "VALUES (%s, 4, 'text', "
        "'TANK HEATER FAILURE CHECK: If the Axiom brewer is not heating, check the tank heater "
        "and thermistor circuit for continuity.', 140, 0)",
        (doc_id,),
    )
    conn.execute(
        "INSERT INTO chunks (document_id, page_number, chunk_type, content, char_count, ordinal) "
        "VALUES (%s, 5, 'text', "
        "'TANK HEATER SERVICE PROCEDURE: To install a new tank heater on the Axiom, disconnect "
        "power, remove the four screws, and unplug the wire harness connector J12.', 160, 1)",
        (doc_id,),
    )
    return doc_id


def test_pending_message_is_resumed_without_a_duplicate_user_turn(test_env):
    with get_conn() as conn:
        conn.execute("INSERT INTO manufacturers (name) VALUES ('Bunn-O-Matic Corporation')")
        conn.execute("INSERT INTO machines (manufacturer_id, model_name) VALUES (1, 'Axiom')")

    _register("resume@example.com")
    conv = client.post("/api/conversations", json={"machine_id": None}).json()

    ask = client.post(f"/api/conversations/{conv['id']}/messages", json={"content": "What does error E4 mean?"})
    assert ask.status_code == 200
    assert ask.json()["is_clarifying_question"] is True

    with get_conn() as conn:
        pending = conn.execute(
            "SELECT pending_message_id FROM conversations WHERE id = %s", (conv["id"],)
        ).fetchone()
    assert pending["pending_message_id"] is not None

    resp = client.post(f"/api/conversations/{conv['id']}/machine", json={"machine_id": 1})
    assert resp.status_code == 200

    with get_conn() as conn:
        pending_after = conn.execute(
            "SELECT pending_message_id FROM conversations WHERE id = %s", (conv["id"],)
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
        conn.execute("INSERT INTO manufacturers (name) VALUES ('Bunn-O-Matic Corporation')")
        conn.execute("INSERT INTO machines (manufacturer_id, model_name) VALUES (1, 'Axiom')")

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
            "SELECT pending_message_id FROM conversations WHERE id = %s", (conv["id"],)
        ).fetchone()
    assert pending["pending_message_id"] is None


def test_asking_a_new_question_clears_a_stale_pending_clarification(test_env):
    # Needs an eligible document, not just a bare machine row -- the second
    # question below resolves "Axiom" directly via _resolve_machine_mention's
    # own name match, which (like search_machines/recent_machines) only
    # matches a machine with something actually retrievable.
    with get_conn() as conn:
        _seed_axiom_with_heater_chunks(conn)

    _register("stalepending@example.com")
    conv = client.post("/api/conversations", json={"machine_id": None}).json()

    client.post(f"/api/conversations/{conv['id']}/messages", json={"content": "What does error E4 mean?"})
    with get_conn() as conn:
        pending = conn.execute(
            "SELECT pending_message_id FROM conversations WHERE id = %s", (conv["id"],)
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
            "SELECT pending_message_id FROM conversations WHERE id = %s", (conv["id"],)
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
            "SELECT content, resolved_query FROM messages WHERE conversation_id = %s AND role = 'user' ORDER BY id",
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


def test_history_does_not_leak_across_a_machine_switch(test_env, monkeypatch):
    """After switching machines mid-conversation, neither the provider
    prompt's history nor query resolution's context words may carry a
    turn from the OLD machine -- a similar sibling model's own different
    values must never reach an answer for the new one."""
    import app.api.routes_chat as routes_chat
    from app.providers.base import AIProvider, GeneratedAnswer

    with get_conn() as conn:
        conn.execute("INSERT INTO manufacturers (name) VALUES ('Bunn-O-Matic Corporation')")
        conn.execute("INSERT INTO machines (manufacturer_id, model_name) VALUES (1, 'Machine A')")
        conn.execute("INSERT INTO machines (manufacturer_id, model_name) VALUES (1, 'Machine B')")

    captured: list[list] = []

    class _CapturingProvider(AIProvider):
        name = "test_capturing"

        def generate(self, question, machine_label, passages, history=None):
            captured.append(list(history or []))
            return GeneratedAnswer(
                answer=f"Answer about {machine_label}.", citations=[], provider=self.name, is_no_answer=False,
            )

    monkeypatch.setattr(routes_chat, "hybrid_search", lambda *a, **k: [])
    monkeypatch.setattr(routes_chat, "has_eligible_manual", lambda *a, **k: True)
    monkeypatch.setattr(routes_chat, "get_provider", lambda: _CapturingProvider())

    _register("historyleak@example.com")
    conv = client.post("/api/conversations", json={"machine_id": 1}).json()
    resp_a = client.post(
        f"/api/conversations/{conv['id']}/messages", json={"content": "What is the fill temperature for Machine A?"}
    )
    assert resp_a.status_code == 200
    assert captured[-1] == [], "the very first question has no prior history to leak"

    switch = client.post(f"/api/conversations/{conv['id']}/machine", json={"machine_id": 2})
    assert switch.status_code == 200, switch.text

    resp_b = client.post(
        f"/api/conversations/{conv['id']}/messages", json={"content": "What is the fill temperature?"}
    )
    assert resp_b.status_code == 200
    history_for_b = captured[-1]
    assert history_for_b == [], (
        f"Machine A's turn must not appear in Machine B's history, got {history_for_b}"
    )


def test_a_legacy_null_machine_id_row_is_not_treated_as_a_switch_cutoff(test_env, monkeypatch):
    """A row from before messages.machine_id existed has NULL there, same
    as a row from before any machine was ever resolved -- _fetch_history
    must not mistake either for a machine-switch boundary and truncate
    history that's actually still relevant."""
    import app.api.routes_chat as routes_chat
    from app.providers.base import AIProvider, GeneratedAnswer

    with get_conn() as conn:
        conn.execute("INSERT INTO manufacturers (name) VALUES ('Bunn-O-Matic Corporation')")
        conn.execute("INSERT INTO machines (manufacturer_id, model_name) VALUES (1, 'Machine A')")

    captured: list[list] = []

    class _CapturingProvider(AIProvider):
        name = "test_capturing"

        def generate(self, question, machine_label, passages, history=None):
            captured.append(list(history or []))
            return GeneratedAnswer(
                answer="A real answer about the heater.", citations=[], provider=self.name, is_no_answer=False,
            )

    monkeypatch.setattr(routes_chat, "hybrid_search", lambda *a, **k: [])
    monkeypatch.setattr(routes_chat, "has_eligible_manual", lambda *a, **k: True)
    monkeypatch.setattr(routes_chat, "get_provider", lambda: _CapturingProvider())

    _register("legacynullrow@example.com")
    conv = client.post("/api/conversations", json={"machine_id": 1}).json()
    client.post(f"/api/conversations/{conv['id']}/messages", json={"content": "Why is it not heating?"})

    # Simulate a pre-migration row: a real answer with machine_id explicitly
    # NULLed out, as if this column never existed when it was written.
    with get_conn() as conn:
        conn.execute(
            "UPDATE messages SET machine_id = NULL WHERE conversation_id = %s AND role = 'assistant'",
            (conv["id"],),
        )

    client.post(f"/api/conversations/{conv['id']}/messages", json={"content": "What about replacing it?"})
    history_for_followup = captured[-1]
    # "heater" is unique to the NULLed assistant row's own content (the
    # user turn never carries machine_id and would pass regardless) --
    # proving specifically that THIS row survived the cutoff check.
    assert any("heater" in h.content.lower() for h in history_for_followup), (
        f"a legacy NULL machine_id row must not be mistaken for a switch boundary, got {history_for_followup}"
    )

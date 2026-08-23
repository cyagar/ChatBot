"""P1-5 (independent follow-up review): "the API must return an honest
not_found response even when the model is unavailable." vector_search()
already skips embed_query() entirely when there are no eligible chunks, AND
now swallows an embed_query() failure itself to degrade to lexical-only
results rather than raising (see tests/retrieval/test_search.py -- an
advisor-caught gap in the first pass at this fix: throwing away a working
lexical result set just because the *vector* half failed was stricter than
the review asked for). So an embedding-model failure specifically no longer
reaches this module at all.

This file covers the remaining backstop: hybrid_search() failing for some
other reason entirely (a corrupted FTS index, an unexpected bug in fusion/
hydration, etc.) must still degrade to an honest, persisted no-answer message
instead of an unhandled 500 that leaves the technician's question answered by
nothing."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.db import get_conn
from app.main import app
from tests.conftest import register_test_user

client = TestClient(app)


def test_retrieval_failure_degrades_to_an_honest_no_answer_response(test_env, monkeypatch):
    import app.api.routes_chat as routes_chat

    def exploding_hybrid_search(*args, **kwargs):
        raise RuntimeError("simulated retrieval failure unrelated to the embedding model")

    monkeypatch.setattr(routes_chat, "hybrid_search", exploding_hybrid_search)

    with get_conn() as conn:
        conn.execute("INSERT INTO manufacturers (id, name) VALUES (1, 'Bunn-O-Matic Corporation')")
        conn.execute("INSERT INTO machines (id, manufacturer_id, model_name) VALUES (1, 1, 'Axiom')")

    register_test_user(client, "retrievalfail@example.com", role="technician")
    conv = client.post("/api/conversations", json={"machine_id": 1}).json()

    resp = client.post(f"/api/conversations/{conv['id']}/messages", json={"content": "Why is it not heating?"})

    assert resp.status_code == 200, "a retrieval failure must never surface as an unhandled 500"
    body = resp.json()
    assert body["is_no_answer"] is True
    assert body["answer_status"] == "failed"
    assert body["citations"] == []
    # Must not claim a search concluded and found nothing -- that's a
    # different, more misleading statement than "the search itself broke."
    assert "no relevant" not in body["content"].lower()
    assert "temporary technical problem" in body["content"].lower() or "try again" in body["content"].lower()


def test_retrieval_failure_is_still_persisted_not_silently_dropped(test_env, monkeypatch):
    import app.api.routes_chat as routes_chat

    def exploding_hybrid_search(*args, **kwargs):
        raise RuntimeError("simulated retrieval failure")

    monkeypatch.setattr(routes_chat, "hybrid_search", exploding_hybrid_search)

    with get_conn() as conn:
        conn.execute("INSERT INTO manufacturers (id, name) VALUES (1, 'Bunn-O-Matic Corporation')")
        conn.execute("INSERT INTO machines (id, manufacturer_id, model_name) VALUES (1, 1, 'Axiom')")

    register_test_user(client, "retrievalfail2@example.com", role="technician")
    conv = client.post("/api/conversations", json={"machine_id": 1}).json()
    client.post(f"/api/conversations/{conv['id']}/messages", json={"content": "Why is it not heating?"})

    messages = client.get(f"/api/conversations/{conv['id']}/messages").json()
    assert len(messages) == 2, "the user's question and a persisted (failed) assistant answer, not an orphaned question"
    assert messages[0]["role"] == "user"
    assert messages[1]["role"] == "assistant"
    assert messages[1]["answer_status"] == "failed"

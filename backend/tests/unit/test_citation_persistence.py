"""Concern #7 (citation persistence) covers two things that the local_extractive
smoke test can't: (1) a provider citing a SUBSET of retrieved passages must
persist and reload as exactly that subset, not every passage that was merely
retrieved; (2) an answer with no valid citations and no is_no_answer flag must
never fall back to "cite everything" -- it must be rejected outright.
local_extractive always cites every passage it shows, so neither case is
exercised by the live smoke test recorded in docs/PRODUCTION_READINESS.md.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.db import get_conn
from app.main import app
from app.providers.base import AIProvider, GeneratedAnswer, parse_and_validate
from app.retrieval.search import RetrievedChunk
from tests.conftest import register_test_user

client = TestClient(app)


def _passage(chunk_id: int, document_id: int, content: str) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=chunk_id, document_id=document_id, content=content,
        page_number=1, section_heading=None, chunk_type="text",
        original_filename="manual.pdf", title="Manual", doc_type="service_repair",
        revision=None, manufacturer="Bunn-O-Matic Corporation", is_current_revision=True,
        lexical_score=1.0, vector_score=1.0, combined_score=1.0,
    )


def test_parse_and_validate_rejects_unsupported_answer_instead_of_citing_everything():
    """The single most safety-critical branch: no claims/steps and no
    is_no_answer flag must return None, forcing the caller to retry/fall back
    -- never silently mark an unsupported answer as fully cited."""
    passages = [_passage(1, 1, "excerpt one"), _passage(2, 1, "excerpt two")]
    raw = json.dumps({"is_no_answer": False, "claims": [], "steps": [], "warnings": []})
    assert parse_and_validate(raw, passages, "test") is None


def test_parse_and_validate_keeps_only_the_cited_subset():
    passages = [_passage(i, 1, f"excerpt {i}") for i in range(1, 7)]
    raw = json.dumps({
        "is_no_answer": False,
        "claims": [{"text": "Two of the six retrieved excerpts support this.", "cited_excerpt_numbers": [2, 5]}],
        "steps": [],
        "warnings": [],
    })
    result = parse_and_validate(raw, passages, "test")
    assert result is not None
    assert [c.chunk_id for c in result.citations] == [2, 5]


class _SubsetCitingProvider(AIProvider):
    """Fake provider that always cites only the 2nd and 5th of whatever
    passages it's given, via the real parse_and_validate path -- exercises
    the same validation the real Anthropic/OpenAI providers rely on."""

    name = "test_subset"

    def generate(self, question, machine_label, passages, history=None):
        raw = json.dumps({
            "is_no_answer": False,
            "claims": [{"text": "Two of the six retrieved excerpts support this.", "cited_excerpt_numbers": [2, 5]}],
            "steps": [],
            "warnings": [],
        })
        result = parse_and_validate(raw, passages, self.name)
        assert result is not None
        return result


@pytest.fixture
def six_passages(test_env):
    with get_conn() as conn:
        conn.execute("INSERT INTO manufacturers (id, name) VALUES (1, 'Bunn-O-Matic Corporation')")
        conn.execute("INSERT INTO machines (id, manufacturer_id, model_name) VALUES (1, 1, 'Axiom')")
        conn.execute(
            "INSERT INTO documents (id, original_filename, storage_path, source_system, "
            "file_type, sha256, byte_size, status) VALUES "
            "(1, 'manual.pdf', 'manual.pdf', 'local_directory', 'pdf', 'deadbeef', 100, 'indexed')"
        )
        conn.execute("INSERT INTO document_machines (document_id, machine_id, confidence) VALUES (1, 1, 1.0)")
        for i in range(1, 7):
            conn.execute(
                "INSERT INTO chunks (id, document_id, page_number, chunk_type, content, char_count, ordinal) "
                "VALUES (?, 1, 1, 'text', ?, 20, ?)",
                (i, f"excerpt content number {i}", i),
            )
    return [_passage(i, 1, f"excerpt content number {i}") for i in range(1, 7)]


def test_subset_citation_persists_and_reloads_as_exactly_that_subset(monkeypatch, six_passages):
    import app.api.routes_chat as routes_chat

    monkeypatch.setattr(routes_chat, "hybrid_search", lambda *a, **k: six_passages)
    monkeypatch.setattr(routes_chat, "get_provider", lambda: _SubsetCitingProvider())

    register_test_user(client, "citetest@example.com")
    conv = client.post("/api/conversations", json={"machine_id": 1}).json()

    ask = client.post(f"/api/conversations/{conv['id']}/messages", json={"content": "How do I fix it?"})
    assert ask.status_code == 200
    body = ask.json()
    assert sorted(c["chunk_id"] for c in body["citations"]) == [2, 5]

    reloaded = client.get(f"/api/conversations/{conv['id']}/messages").json()
    assistant_msg = [m for m in reloaded if m["role"] == "assistant"][-1]
    assert sorted(c["chunk_id"] for c in assistant_msg["citations"]) == [2, 5]


class _ReverseOrderCitingProvider(AIProvider):
    """Cites excerpts in an order that deliberately disagrees with retrieval
    order (5 before 2), so a reload that sorts by retrieval rank produces a
    different order than the live response."""

    name = "test_reverse"

    def generate(self, question, machine_label, passages, history=None):
        raw = json.dumps({
            "is_no_answer": False,
            "claims": [
                {"text": "The later excerpt states the primary fact.", "cited_excerpt_numbers": [5]},
                {"text": "The earlier excerpt adds a supporting detail.", "cited_excerpt_numbers": [2]},
            ],
            "steps": [], "warnings": [],
        })
        result = parse_and_validate(raw, passages, self.name)
        assert result is not None
        return result


def test_citation_order_is_identical_live_and_after_reload(monkeypatch, six_passages):
    """P1-7: message_sources.rank is RETRIEVAL order, but the live response
    returns citations in PROVIDER order. Reload previously ordered by rank, so
    a reloaded conversation could show citations in a different order than the
    technician originally saw -- the numbering under an answer would stop
    matching the answer's own claims. Strict list equality, not sorted()."""
    import app.api.routes_chat as routes_chat

    monkeypatch.setattr(routes_chat, "hybrid_search", lambda *a, **k: six_passages)
    monkeypatch.setattr(routes_chat, "get_provider", lambda: _ReverseOrderCitingProvider())

    register_test_user(client, "ordertest@example.com")
    conv = client.post("/api/conversations", json={"machine_id": 1}).json()

    ask = client.post(f"/api/conversations/{conv['id']}/messages", json={"content": "How do I fix it?"})
    assert ask.status_code == 200
    live_order = [c["chunk_id"] for c in ask.json()["citations"]]
    assert live_order == [5, 2], "provider order should be preserved in the live response"

    reloaded = client.get(f"/api/conversations/{conv['id']}/messages").json()
    assistant_msg = [m for m in reloaded if m["role"] == "assistant"][-1]
    reload_order = [c["chunk_id"] for c in assistant_msg["citations"]]
    assert reload_order == live_order, "reload must reproduce the live citation order exactly"


class _DuplicateCitingProvider(AIProvider):
    """Returns the same chunk twice. Persistence keys by chunk_id, so without
    an explicit order-preserving dedupe the duplicate would collapse on
    reload while still appearing twice live (P1-7)."""

    name = "test_duplicate"

    def generate(self, question, machine_label, passages, history=None):
        from app.providers.base import Citation

        p = passages[1]
        c = Citation(
            chunk_id=p.chunk_id, document_id=p.document_id, filename=p.original_filename,
            title=p.title, page_number=p.page_number, section_heading=p.section_heading,
            revision=p.revision, excerpt=p.content[:500],
        )
        return GeneratedAnswer(answer="Duplicated citation answer.", citations=[c, c], provider=self.name)


def test_duplicate_citations_are_deduplicated_consistently_live_and_on_reload(monkeypatch, six_passages):
    import app.api.routes_chat as routes_chat

    monkeypatch.setattr(routes_chat, "hybrid_search", lambda *a, **k: six_passages)
    monkeypatch.setattr(routes_chat, "get_provider", lambda: _DuplicateCitingProvider())

    register_test_user(client, "duptest@example.com")
    conv = client.post("/api/conversations", json={"machine_id": 1}).json()

    ask = client.post(f"/api/conversations/{conv['id']}/messages", json={"content": "How do I fix it?"})
    live_order = [c["chunk_id"] for c in ask.json()["citations"]]
    assert live_order == [2], "duplicate citations must collapse in the live response too"

    reloaded = client.get(f"/api/conversations/{conv['id']}/messages").json()
    assistant_msg = [m for m in reloaded if m["role"] == "assistant"][-1]
    assert [c["chunk_id"] for c in assistant_msg["citations"]] == live_order

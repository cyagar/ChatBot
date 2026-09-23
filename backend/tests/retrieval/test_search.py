"""Retrieval tests against a real (small) SQLite DB with real embeddings.
Uses the actual local sentence-transformer model — slower than a mock, but the
whole point of these tests is to prove the machine-scoping filter and FTS
sanitization work end-to-end, which a mock would hide."""

import pytest

from app.db import get_conn


def _seed_two_machines_with_similar_language(conn):
    """Two machines whose manuals use overlapping vocabulary ('brewer',
    'heating element', 'error') but different specifics — the exact scenario
    plan requirement 11 guards against."""
    # None of these rows write an explicit id -- Postgres's GENERATED ALWAYS
    # AS IDENTITY columns reject that, and RESTART IDENTITY (tests/conftest.
    # py's test_env fixture) plus this function's fixed insertion order
    # already guarantee the generated ids come out as 1, 2, ... exactly as
    # this file's hardcoded cross-references (machine_id=1, document_id=2,
    # etc.) assume. content_tsv (chunks' generated tsvector column) is
    # auto-maintained by Postgres -- unlike SQLite's chunks_fts virtual
    # table, there is no separate sync step to run after inserting chunks.
    conn.execute("INSERT INTO manufacturers (name) VALUES ('Bunn-O-Matic Corporation')")
    conn.execute(
        "INSERT INTO machines (manufacturer_id, model_name, family, machine_type) "
        "VALUES (1, 'Axiom', 'Axiom Series', 'coffee brewer')"
    )
    conn.execute(
        "INSERT INTO machines (manufacturer_id, model_name, family, machine_type) "
        "VALUES (1, 'ICB Twin', 'Infusion Series', 'coffee brewer')"
    )

    # review_status='approved' explicitly: these fixtures simulate an
    # already-published, reviewed corpus, not the P0-6 review-queue workflow
    # itself (that's covered separately in test_review_status_gates_retrieval).
    conn.execute(
        "INSERT INTO documents (original_filename, storage_path, source_system, source_ref, "
        "file_type, sha256, byte_size, status, review_status) VALUES ('axiom.pdf', 'axiom.pdf', "
        "'local_directory', 'axiom.pdf', 'pdf', 'hash1', 100, 'indexed', 'approved')"
    )
    conn.execute(
        "INSERT INTO documents (original_filename, storage_path, source_system, source_ref, "
        "file_type, sha256, byte_size, status, review_status) VALUES ('icb.pdf', 'icb.pdf', "
        "'local_directory', 'icb.pdf', 'pdf', 'hash2', 100, 'indexed', 'approved')"
    )
    conn.execute("INSERT INTO document_machines (document_id, machine_id, review_status) VALUES (1, 1, 'approved')")
    conn.execute("INSERT INTO document_machines (document_id, machine_id, review_status) VALUES (2, 2, 'approved')")

    conn.execute(
        "INSERT INTO chunks (document_id, page_number, chunk_type, content, char_count, ordinal) "
        "VALUES (1, 4, 'text', 'Axiom brewer heating element error E4 means the thermistor circuit is open on the Axiom.', 90, 0)"
    )
    conn.execute(
        "INSERT INTO chunks (document_id, page_number, chunk_type, content, char_count, ordinal) "
        "VALUES (2, 7, 'text', 'ICB Twin brewer heating element error E4 means a different fault on the ICB Twin control board.', 95, 0)"
    )


def _embed_seeded_chunks():
    from app.retrieval.embeddings import embed_texts, embedding_fingerprint, vector_to_blob

    with get_conn() as conn:
        rows = conn.execute("SELECT id, content FROM chunks ORDER BY id").fetchall()
        vectors = embed_texts([r["content"] for r in rows])
        for row, vec in zip(rows, vectors):
            conn.execute(
                "INSERT INTO embeddings (chunk_id, model_name, dim, vector) VALUES (%s, %s, %s, %s)",
                (row["id"], embedding_fingerprint(), len(vec), vector_to_blob(vec)),
            )


# --- P1-5: a no-document query must never load the embedding model -------
# Deliberately NOT @pytest.mark.slow: the whole point is proving embed_query
# is never called, so these must never load the real model either.

def test_vector_search_never_calls_embed_query_when_no_eligible_chunks(test_env, monkeypatch):
    """Independent follow-up review P1-5: 'query eligible rows first and
    return [] before embed_query() when none exist.' An empty corpus (or one
    with nothing for the given machine) must resolve without ever touching
    the embedding model -- loading it just to discover there's nothing to
    compare against wastes time and makes an otherwise-instant 'nothing
    here' answer depend on model/network availability for no reason."""
    from app.retrieval import search as search_module

    def exploding_embed_query(text):
        raise AssertionError("embed_query() must not be called when there are no eligible chunks")

    monkeypatch.setattr(search_module, "embed_query", exploding_embed_query)

    assert search_module.vector_search("anything", machine_id=None) == []
    assert search_module.vector_search("anything", machine_id=999) == []


def test_vector_search_calls_embed_query_when_eligible_chunks_exist(test_env, monkeypatch):
    """The complement of the test above: once there IS something eligible to
    compare against, embed_query() must actually run -- proving the early
    return is scoped to the true no-document case, not disabling vector
    search generally."""
    import numpy as np

    from app.retrieval import search as search_module
    from app.retrieval.embeddings import embedding_fingerprint, vector_to_blob

    with get_conn() as conn:
        conn.execute("INSERT INTO manufacturers (name) VALUES ('Bunn-O-Matic Corporation')")
        conn.execute("INSERT INTO machines (manufacturer_id, model_name) VALUES (1, 'Axiom')")
        conn.execute(
            "INSERT INTO documents (original_filename, storage_path, source_system, source_ref, "
            "file_type, sha256, byte_size, status, review_status) VALUES ('axiom.pdf', 'axiom.pdf', "
            "'local_directory', 'axiom.pdf', 'pdf', 'hash1', 100, 'indexed', 'approved')"
        )
        conn.execute("INSERT INTO document_machines (document_id, machine_id, review_status) VALUES (1, 1, 'approved')")
        conn.execute(
            "INSERT INTO chunks (document_id, page_number, chunk_type, content, char_count, ordinal) "
            "VALUES (1, 1, 'text', 'Some manual content here.', 25, 0)"
        )
        conn.execute(
            "INSERT INTO embeddings (chunk_id, model_name, dim, vector) VALUES (1, %s, 2, %s)",
            (embedding_fingerprint(), vector_to_blob(np.array([1.0, 0.0], dtype=np.float32))),
        )

    calls = []

    def fake_embed_query(text):
        calls.append(text)
        return np.array([1.0, 0.0], dtype=np.float32)

    monkeypatch.setattr(search_module, "embed_query", fake_embed_query)

    result = search_module.vector_search("anything", machine_id=1)

    assert calls == ["anything"]
    assert len(result) == 1
    assert result[0][0] == 1
    # dim=2 here is arbitrary -- chosen for a trivial fake vector, not the real
    # model's 384. vector_search reads dim per-row, so this is not a bug.


def test_p1_15_vector_search_excludes_a_chunk_embedded_under_a_different_model_fingerprint(test_env, monkeypatch):
    """The actual bug: embeddings.model_name used to record only the model
    name, not the revision -- a stale row (from before a model/revision
    change) looked eligible and got compared against a fresh query vector
    from a DIFFERENT vector space, producing meaningless similarity scores.
    vector_search must now exclude it -- proven here by making it the ONLY
    embedding for the only eligible chunk, so an unfiltered query would
    return it and a correctly filtered one returns []."""
    import numpy as np

    from app.retrieval import search as search_module
    from app.retrieval.embeddings import vector_to_blob

    with get_conn() as conn:
        conn.execute("INSERT INTO manufacturers (name) VALUES ('Bunn-O-Matic Corporation')")
        conn.execute("INSERT INTO machines (manufacturer_id, model_name) VALUES (1, 'Axiom')")
        conn.execute(
            "INSERT INTO documents (original_filename, storage_path, source_system, source_ref, "
            "file_type, sha256, byte_size, status, review_status) VALUES ('axiom.pdf', 'axiom.pdf', "
            "'local_directory', 'axiom.pdf', 'pdf', 'hash1', 100, 'indexed', 'approved')"
        )
        conn.execute("INSERT INTO document_machines (document_id, machine_id, review_status) VALUES (1, 1, 'approved')")
        conn.execute(
            "INSERT INTO chunks (document_id, page_number, chunk_type, content, char_count, ordinal) "
            "VALUES (1, 1, 'text', 'Some manual content here.', 25, 0)"
        )
        conn.execute(
            "INSERT INTO embeddings (chunk_id, model_name, dim, vector) VALUES (1, %s, 2, %s)",
            ("some-old-model@old-revision", vector_to_blob(np.array([1.0, 0.0], dtype=np.float32))),
        )

    monkeypatch.setattr(search_module, "embed_query", lambda text: np.array([1.0, 0.0], dtype=np.float32))

    assert search_module.vector_search("anything", machine_id=1) == []


def test_vector_search_returns_empty_list_without_raising_when_embedding_model_fails(test_env, monkeypatch):
    """Advisor-caught gap in the first pass at P1-5: the review's ask was an
    honest not_found response when the model is unavailable, but throwing away
    a whole hybrid_search() call (including a perfectly working lexical result)
    over the *vector* half failing was stricter than necessary -- and the
    P1-2 fix already established the precedent of degrading to a labeled
    lexical-only mode rather than refusing outright. vector_search() must
    swallow an embed_query() failure and return [] so hybrid_search() (below)
    can still return real, citable lexical results."""
    import numpy as np

    from app.retrieval import search as search_module
    from app.retrieval.embeddings import embedding_fingerprint, vector_to_blob

    with get_conn() as conn:
        conn.execute("INSERT INTO manufacturers (name) VALUES ('Bunn-O-Matic Corporation')")
        conn.execute("INSERT INTO machines (manufacturer_id, model_name) VALUES (1, 'Axiom')")
        conn.execute(
            "INSERT INTO documents (original_filename, storage_path, source_system, source_ref, "
            "file_type, sha256, byte_size, status, review_status) VALUES ('axiom.pdf', 'axiom.pdf', "
            "'local_directory', 'axiom.pdf', 'pdf', 'hash1', 100, 'indexed', 'approved')"
        )
        conn.execute("INSERT INTO document_machines (document_id, machine_id, review_status) VALUES (1, 1, 'approved')")
        conn.execute(
            "INSERT INTO chunks (document_id, page_number, chunk_type, content, char_count, ordinal) "
            "VALUES (1, 1, 'text', 'Some manual content here.', 25, 0)"
        )
        conn.execute(
            "INSERT INTO embeddings (chunk_id, model_name, dim, vector) VALUES (1, %s, 2, %s)",
            (embedding_fingerprint(), vector_to_blob(np.array([1.0, 0.0], dtype=np.float32))),
        )

    def exploding_embed_query(text):
        raise RuntimeError("Could not load embedding model (simulated).")

    monkeypatch.setattr(search_module, "embed_query", exploding_embed_query)

    assert search_module.vector_search("anything", machine_id=1) == []


def test_hybrid_search_returns_lexical_only_results_when_embedding_model_fails(test_env, monkeypatch):
    """The end-to-end version of the test above: with the embedding model down
    but FTS-matchable content present, hybrid_search() must still return
    citable results (from lexical search alone) rather than degrading all the
    way to routes_chat's refusal message -- that refusal is meant for when
    retrieval genuinely has nothing, not for a single subsystem being down."""
    from app.retrieval import search as search_module

    with get_conn() as conn:
        conn.execute("INSERT INTO manufacturers (name) VALUES ('Bunn-O-Matic Corporation')")
        conn.execute("INSERT INTO machines (manufacturer_id, model_name) VALUES (1, 'Axiom')")
        conn.execute(
            "INSERT INTO documents (original_filename, storage_path, source_system, source_ref, "
            "file_type, sha256, byte_size, status, review_status) VALUES ('axiom.pdf', 'axiom.pdf', "
            "'local_directory', 'axiom.pdf', 'pdf', 'hash1', 100, 'indexed', 'approved')"
        )
        conn.execute("INSERT INTO document_machines (document_id, machine_id, review_status) VALUES (1, 1, 'approved')")
        conn.execute(
            "INSERT INTO chunks (document_id, page_number, chunk_type, content, char_count, ordinal) "
            "VALUES (1, 4, 'text', 'Axiom brewer heating element error E4 means the thermistor circuit is open.', 90, 0)"
        )

    def exploding_embed_query(text):
        raise RuntimeError("Could not load embedding model (simulated).")

    monkeypatch.setattr(search_module, "embed_query", exploding_embed_query)

    results = search_module.hybrid_search("error code E4", machine_id=1, top_k=6)

    assert len(results) == 1
    assert results[0].chunk_id == 1
    assert results[0].vector_score == 0.0
    assert results[0].lexical_score != 0.0


@pytest.mark.slow
def test_machine_filter_excludes_other_models_chunks(test_env):
    with get_conn() as conn:
        _seed_two_machines_with_similar_language(conn)
    _embed_seeded_chunks()

    from app.retrieval.search import hybrid_search

    results = hybrid_search("What does error code E4 mean?", machine_id=1, top_k=10)
    assert results, "expected at least one match for machine 1"
    assert all(r.document_id == 1 for r in results), (
        "a machine-scoped query must never return chunks belonging to a different model's document"
    )

    results_other = hybrid_search("What does error code E4 mean?", machine_id=2, top_k=10)
    assert all(r.document_id == 2 for r in results_other)


@pytest.mark.slow
def test_unfiltered_query_can_return_both_machines(test_env):
    with get_conn() as conn:
        _seed_two_machines_with_similar_language(conn)
    _embed_seeded_chunks()

    from app.retrieval.search import hybrid_search

    results = hybrid_search("brewer heating element error", machine_id=None, top_k=10)
    doc_ids = {r.document_id for r in results}
    assert doc_ids == {1, 2}


@pytest.mark.slow
def test_review_status_gates_retrieval(test_env):
    """Independent follow-up review P0-6: 'Confidence is stored but not
    enforced.' Same document, linked to two machines -- one link approved,
    one still pending. Retrieval must return results for the approved link
    and nothing for the pending one, proving both documents.review_status AND
    document_machines.review_status are enforced, not just one of them."""
    with get_conn() as conn:
        conn.execute("INSERT INTO manufacturers (name) VALUES ('Bunn-O-Matic Corporation')")
        conn.execute("INSERT INTO machines (manufacturer_id, model_name) VALUES (1, 'Axiom')")
        conn.execute("INSERT INTO machines (manufacturer_id, model_name) VALUES (1, 'ICB Twin')")
        conn.execute(
            "INSERT INTO documents (original_filename, storage_path, source_system, source_ref, "
            "file_type, sha256, byte_size, status, review_status) VALUES ('axiom.pdf', 'axiom.pdf', "
            "'google_drive', 'f1', 'pdf', 'hash1', 100, 'indexed', 'approved')"
        )
        conn.execute(
            "INSERT INTO document_machines (document_id, machine_id, review_status) VALUES (1, 1, 'approved')"
        )
        conn.execute(
            "INSERT INTO document_machines (document_id, machine_id, review_status) VALUES (1, 2, 'pending')"
        )
        conn.execute(
            "INSERT INTO chunks (document_id, page_number, chunk_type, content, char_count, ordinal) "
            "VALUES (1, 4, 'text', 'Axiom brewer heating element error E4 troubleshooting steps.', 60, 0)"
        )
    _embed_seeded_chunks()

    from app.retrieval.search import hybrid_search

    approved = hybrid_search("error E4", machine_id=1, top_k=10)
    assert approved and all(r.document_id == 1 for r in approved)

    pending = hybrid_search("error E4", machine_id=2, top_k=10)
    assert pending == [], "a document_machines link that hasn't been approved must never surface in retrieval"


@pytest.mark.slow
def test_unapproved_document_excluded_even_without_a_machine_filter(test_env):
    with get_conn() as conn:
        conn.execute("INSERT INTO manufacturers (name) VALUES ('Bunn-O-Matic Corporation')")
        conn.execute(
            "INSERT INTO documents (original_filename, storage_path, source_system, source_ref, "
            "file_type, sha256, byte_size, status, review_status) VALUES ('axiom.pdf', 'axiom.pdf', "
            "'google_drive', 'f1', 'pdf', 'hash1', 100, 'indexed', 'pending')"
        )
        conn.execute(
            "INSERT INTO chunks (document_id, page_number, chunk_type, content, char_count, ordinal) "
            "VALUES (1, 4, 'text', 'Freshly ingested content awaiting admin review.', 48, 0)"
        )
    _embed_seeded_chunks()

    from app.retrieval.search import hybrid_search

    results = hybrid_search("freshly ingested content", machine_id=None, top_k=10)
    assert results == []


def test_fts_query_sanitization_handles_special_characters(test_env):
    """FTS5 query syntax (quotes, parens, colons, hyphens) must not reach the
    engine unescaped — either as a crash risk or as unintended operators."""
    with get_conn() as conn:
        _seed_two_machines_with_similar_language(conn)

    from app.retrieval.search import lexical_search

    for weird_query in ['error "code" (E4)', "what's -this: code?", "***", ""]:
        results = lexical_search(weird_query, machine_id=None)
        assert isinstance(results, list)  # must not raise


@pytest.mark.slow
def test_near_empty_chunks_excluded_from_results(test_env):
    """Found via a live probe with bare code queries: a chunk whose entire
    content was the single letter "E" (OCR/extraction noise) scored a 0.75
    cosine similarity against the query "E4" -- above the relevance gate --
    which would surface a useless one-character 'answer'. hybrid_search must
    filter these out before a provider ever sees them."""
    with get_conn() as conn:
        _seed_two_machines_with_similar_language(conn)
        # This becomes chunk id=3: the shared fixture above already inserted
        # exactly two chunks (ids 1, 2) in this same test's clean-slate
        # transaction, so the next GENERATED ALWAYS AS IDENTITY value is 3,
        # matching the id this test asserts against below.
        conn.execute(
            "INSERT INTO chunks (document_id, page_number, chunk_type, content, char_count, ordinal) "
            "VALUES (1, 4, 'text', 'E', 1, 1)"
        )
    _embed_seeded_chunks()

    from app.retrieval.search import hybrid_search

    results = hybrid_search("E4", machine_id=1, top_k=10)
    assert 3 not in [r.chunk_id for r in results]


def test_deactivated_document_excluded_from_retrieval(test_env):
    with get_conn() as conn:
        _seed_two_machines_with_similar_language(conn)
        conn.execute("UPDATE documents SET deactivated_at = now() WHERE id = 1")

    from app.retrieval.search import lexical_search

    results = lexical_search("thermistor circuit open", machine_id=None)
    matched_ids = [cid for cid, _ in results]
    assert 1 not in matched_ids


def test_superseded_document_is_excluded_from_retrieval_not_merely_penalized(test_env):
    """P1-11 (independent follow-up review): is_current_revision previously
    only applied a -0.20 rerank boost, so a withdrawn revision could still
    surface and be cited. A superseded manual isn't a weaker answer, it's a
    wrong one -- an obsolete torque spec or wiring diagram is exactly the harm
    this system exists to prevent."""
    with get_conn() as conn:
        _seed_two_machines_with_similar_language(conn)
        conn.execute("UPDATE documents SET is_current_revision = false WHERE id = 1")

    from app.retrieval.search import lexical_search

    results = lexical_search("thermistor circuit open", machine_id=None)
    assert 1 not in [cid for cid, _ in results], "superseded document must not be retrievable"

    # ...but the admin/audit flow can still deliberately look at it.
    audit = lexical_search("thermistor circuit open", machine_id=None, include_superseded=True)
    assert 1 in [cid for cid, _ in audit], "admin query tester must still be able to inspect it"


def test_machine_picker_excludes_machines_whose_only_manual_is_superseded(test_env):
    """The picker's eligibility rules must match retrieval's exactly (P1-6),
    including the current-revision rule -- otherwise a technician selects a
    machine that then dead-ends into 'no manuals'."""
    from fastapi.testclient import TestClient

    from app.main import app
    from tests.conftest import register_test_user

    picker_client = TestClient(app)
    with get_conn() as conn:
        _seed_two_machines_with_similar_language(conn)
        conn.execute("UPDATE documents SET is_current_revision = false WHERE id = 1")

    register_test_user(picker_client, "supersededpicker@example.com")
    listing = picker_client.get("/api/machines").json()
    ids = [m["id"] for m in listing]
    assert 1 not in ids, "machine whose only manual is superseded must not be offered"
    assert 2 in ids, "the machine with a current manual must still be offered"

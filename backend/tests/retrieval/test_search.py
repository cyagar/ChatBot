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
    conn.execute("INSERT INTO manufacturers (id, name) VALUES (1, 'Bunn-O-Matic Corporation')")
    conn.execute(
        "INSERT INTO machines (id, manufacturer_id, model_name, family, machine_type) "
        "VALUES (1, 1, 'Axiom', 'Axiom Series', 'coffee brewer')"
    )
    conn.execute(
        "INSERT INTO machines (id, manufacturer_id, model_name, family, machine_type) "
        "VALUES (2, 1, 'ICB Twin', 'Infusion Series', 'coffee brewer')"
    )

    # review_status='approved' explicitly: these fixtures simulate an
    # already-published, reviewed corpus, not the P0-6 review-queue workflow
    # itself (that's covered separately in test_review_status_gates_retrieval).
    conn.execute(
        "INSERT INTO documents (id, original_filename, storage_path, source_system, source_ref, "
        "file_type, sha256, byte_size, status, review_status) VALUES (1, 'axiom.pdf', 'axiom.pdf', "
        "'local_directory', 'axiom.pdf', 'pdf', 'hash1', 100, 'indexed', 'approved')"
    )
    conn.execute(
        "INSERT INTO documents (id, original_filename, storage_path, source_system, source_ref, "
        "file_type, sha256, byte_size, status, review_status) VALUES (2, 'icb.pdf', 'icb.pdf', "
        "'local_directory', 'icb.pdf', 'pdf', 'hash2', 100, 'indexed', 'approved')"
    )
    conn.execute("INSERT INTO document_machines (document_id, machine_id, review_status) VALUES (1, 1, 'approved')")
    conn.execute("INSERT INTO document_machines (document_id, machine_id, review_status) VALUES (2, 2, 'approved')")

    conn.execute(
        "INSERT INTO chunks (id, document_id, page_number, chunk_type, content, char_count, ordinal) "
        "VALUES (1, 1, 4, 'text', 'Axiom brewer heating element error E4 means the thermistor circuit is open on the Axiom.', 90, 0)"
    )
    conn.execute(
        "INSERT INTO chunks (id, document_id, page_number, chunk_type, content, char_count, ordinal) "
        "VALUES (2, 2, 7, 'text', 'ICB Twin brewer heating element error E4 means a different fault on the ICB Twin control board.', 95, 0)"
    )
    conn.execute("INSERT INTO chunks_fts (rowid, content) SELECT id, content FROM chunks")


def _embed_seeded_chunks():
    from app.retrieval.embeddings import embed_texts, vector_to_blob

    with get_conn() as conn:
        rows = conn.execute("SELECT id, content FROM chunks ORDER BY id").fetchall()
        vectors = embed_texts([r["content"] for r in rows])
        for row, vec in zip(rows, vectors):
            conn.execute(
                "INSERT INTO embeddings (chunk_id, model_name, dim, vector) VALUES (?, ?, ?, ?)",
                (row["id"], "test-model", len(vec), vector_to_blob(vec)),
            )


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
        conn.execute("INSERT INTO manufacturers (id, name) VALUES (1, 'Bunn-O-Matic Corporation')")
        conn.execute("INSERT INTO machines (id, manufacturer_id, model_name) VALUES (1, 1, 'Axiom')")
        conn.execute("INSERT INTO machines (id, manufacturer_id, model_name) VALUES (2, 1, 'ICB Twin')")
        conn.execute(
            "INSERT INTO documents (id, original_filename, storage_path, source_system, source_ref, "
            "file_type, sha256, byte_size, status, review_status) VALUES (1, 'axiom.pdf', 'axiom.pdf', "
            "'google_drive', 'f1', 'pdf', 'hash1', 100, 'indexed', 'approved')"
        )
        conn.execute(
            "INSERT INTO document_machines (document_id, machine_id, review_status) VALUES (1, 1, 'approved')"
        )
        conn.execute(
            "INSERT INTO document_machines (document_id, machine_id, review_status) VALUES (1, 2, 'pending')"
        )
        conn.execute(
            "INSERT INTO chunks (id, document_id, page_number, chunk_type, content, char_count, ordinal) "
            "VALUES (1, 1, 4, 'text', 'Axiom brewer heating element error E4 troubleshooting steps.', 60, 0)"
        )
        conn.execute("INSERT INTO chunks_fts (rowid, content) SELECT id, content FROM chunks")
    _embed_seeded_chunks()

    from app.retrieval.search import hybrid_search

    approved = hybrid_search("error E4", machine_id=1, top_k=10)
    assert approved and all(r.document_id == 1 for r in approved)

    pending = hybrid_search("error E4", machine_id=2, top_k=10)
    assert pending == [], "a document_machines link that hasn't been approved must never surface in retrieval"


@pytest.mark.slow
def test_unapproved_document_excluded_even_without_a_machine_filter(test_env):
    with get_conn() as conn:
        conn.execute("INSERT INTO manufacturers (id, name) VALUES (1, 'Bunn-O-Matic Corporation')")
        conn.execute(
            "INSERT INTO documents (id, original_filename, storage_path, source_system, source_ref, "
            "file_type, sha256, byte_size, status, review_status) VALUES (1, 'axiom.pdf', 'axiom.pdf', "
            "'google_drive', 'f1', 'pdf', 'hash1', 100, 'indexed', 'pending')"
        )
        conn.execute(
            "INSERT INTO chunks (id, document_id, page_number, chunk_type, content, char_count, ordinal) "
            "VALUES (1, 1, 4, 'text', 'Freshly ingested content awaiting admin review.', 48, 0)"
        )
        conn.execute("INSERT INTO chunks_fts (rowid, content) SELECT id, content FROM chunks")
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
        conn.execute(
            "INSERT INTO chunks (id, document_id, page_number, chunk_type, content, char_count, ordinal) "
            "VALUES (3, 1, 4, 'text', 'E', 1, 1)"
        )
        conn.execute("INSERT INTO chunks_fts (rowid, content) SELECT id, content FROM chunks WHERE id = 3")
    _embed_seeded_chunks()

    from app.retrieval.search import hybrid_search

    results = hybrid_search("E4", machine_id=1, top_k=10)
    assert 3 not in [r.chunk_id for r in results]


def test_deactivated_document_excluded_from_retrieval(test_env):
    with get_conn() as conn:
        _seed_two_machines_with_similar_language(conn)
        conn.execute("UPDATE documents SET deactivated_at = datetime('now') WHERE id = 1")

    from app.retrieval.search import lexical_search

    results = lexical_search("thermistor circuit open", machine_id=None)
    matched_ids = [cid for cid, _ in results]
    assert 1 not in matched_ids

from fastapi.testclient import TestClient

from app.config import get_settings
from app.db import get_conn
from app.main import app
from tests.conftest import register_test_user

client = TestClient(app)


def _seed_pending_document(conn) -> int:
    conn.execute("INSERT INTO manufacturers (name) VALUES ('Bunn-O-Matic Corporation')")
    conn.execute("INSERT INTO machines (manufacturer_id, model_name) VALUES (1, 'Axiom')")
    cur = conn.execute(
        "INSERT INTO documents (original_filename, storage_path, source_system, source_ref, "
        "file_type, sha256, byte_size, status) VALUES ('axiom.pdf', 'axiom.pdf', 'local_directory', "
        "'axiom.pdf', 'pdf', 'hash1', 100, 'indexed') RETURNING id"
    )
    doc_id = cur.fetchone()["id"]
    conn.execute(
        "INSERT INTO chunks (document_id, page_number, chunk_type, content, char_count, ordinal) "
        "VALUES (%s, 1, 'text', 'Pending manual content awaiting review.', 40, 0)",
        (doc_id,),
    )
    storage_path = get_settings().local_storage_dir_resolved / "axiom.pdf"
    storage_path.write_bytes(b"%PDF-1.4 fake pdf bytes")
    return doc_id


def test_technician_cannot_fetch_a_pending_documents_raw_file(test_env):
    """The raw-file, page-image, and evidence endpoints are a second path to
    document content and must honor the same approval boundary as
    retrieval -- not be reachable via a guessed document id just because
    review_status hasn't caught up with a Drive listing yet."""
    with get_conn() as conn:
        doc_id = _seed_pending_document(conn)
    register_test_user(client, "manualtech@example.com")

    resp = client.get(f"/api/manuals/{doc_id}/file")
    assert resp.status_code == 404

    evidence = client.get(f"/api/manuals/{doc_id}/chunks/1/evidence")
    assert evidence.status_code == 404


def test_administrator_can_still_preview_a_pending_documents_raw_file(test_env):
    with get_conn() as conn:
        doc_id = _seed_pending_document(conn)
    register_test_user(client, "manualadmin@example.com", role="administrator",
                        admin_email="manualadmin@example.com")

    resp = client.get(f"/api/manuals/{doc_id}/file")
    assert resp.status_code == 200

    evidence = client.get(f"/api/manuals/{doc_id}/chunks/1/evidence")
    assert evidence.status_code == 200
    assert evidence.json()["content"] == "Pending manual content awaiting review."


def test_technician_can_fetch_an_approved_documents_raw_file(test_env):
    with get_conn() as conn:
        doc_id = _seed_pending_document(conn)
        conn.execute("UPDATE documents SET review_status = 'approved' WHERE id = %s", (doc_id,))
    register_test_user(client, "manualtech2@example.com")

    resp = client.get(f"/api/manuals/{doc_id}/file")
    assert resp.status_code == 200


def test_p2_03_a_page_over_the_render_pixel_budget_is_rejected_not_rendered(test_env, monkeypatch):
    import fitz

    monkeypatch.setenv("MAX_PAGE_RENDER_PIXELS", "100")
    get_settings.cache_clear()
    try:
        with get_conn() as conn:
            conn.execute("INSERT INTO manufacturers (name) VALUES ('Bunn-O-Matic Corporation')")
            conn.execute("INSERT INTO machines (manufacturer_id, model_name) VALUES (1, 'Axiom')")
            cur = conn.execute(
                "INSERT INTO documents (original_filename, storage_path, source_system, source_ref, "
                "file_type, sha256, byte_size, status, review_status) VALUES ('huge.pdf', 'huge.pdf', "
                "'local_directory', 'huge.pdf', 'pdf', 'hash-huge', 100, 'indexed', 'approved') RETURNING id"
            )
            doc_id = cur.fetchone()["id"]
            storage_path = get_settings().local_storage_dir_resolved / "huge.pdf"
            doc = fitz.open()
            doc.new_page(width=3000, height=3000)
            doc.save(storage_path)
            doc.close()
        register_test_user(client, "manualtech3@example.com")

        resp = client.get(f"/api/manuals/{doc_id}/pages/1/image")
        assert resp.status_code == 400
        assert "pixel budget" in resp.json()["detail"].lower()
    finally:
        get_settings.cache_clear()


def test_the_page_cache_is_bounded_by_bytes_and_evicts_least_recently_used():
    from app.api.routes_manuals import _ByteBoundedCache

    cache = _ByteBoundedCache(max_bytes=100)
    cache.put("a", b"x" * 40)
    cache.put("b", b"x" * 40)
    assert cache.get("a") is not None  # a is now the most recently used
    cache.put("c", b"x" * 40)  # 120 bytes total: evicts b, the least recently used
    assert cache.get("b") is None
    assert cache.get("a") is not None and cache.get("c") is not None
    cache.put("huge", b"x" * 101)
    assert cache.get("huge") is None, "an entry over the whole budget is not cached"

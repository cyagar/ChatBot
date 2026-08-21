from fastapi.testclient import TestClient

from app.config import get_settings
from app.db import get_conn
from app.main import app
from tests.conftest import register_test_user

client = TestClient(app)


def _seed_pending_document(conn) -> int:
    conn.execute("INSERT INTO manufacturers (id, name) VALUES (1, 'Bunn-O-Matic Corporation')")
    conn.execute("INSERT INTO machines (id, manufacturer_id, model_name) VALUES (1, 1, 'Axiom')")
    cur = conn.execute(
        "INSERT INTO documents (original_filename, storage_path, source_system, source_ref, "
        "file_type, sha256, byte_size, status) VALUES ('axiom.pdf', 'axiom.pdf', 'local_directory', "
        "'axiom.pdf', 'pdf', 'hash1', 100, 'indexed')"
    )
    doc_id = cur.lastrowid
    conn.execute(
        "INSERT INTO chunks (id, document_id, page_number, chunk_type, content, char_count, ordinal) "
        "VALUES (1, ?, 1, 'text', 'Pending manual content awaiting review.', 40, 0)",
        (doc_id,),
    )
    storage_path = get_settings().local_storage_dir_resolved / "axiom.pdf"
    storage_path.write_bytes(b"%PDF-1.4 fake pdf bytes")
    return doc_id


def test_technician_cannot_fetch_a_pending_documents_raw_file(test_env):
    """The raw-file, page-image, and evidence endpoints are a second path to
    document content and must honor the same P0-6 approval boundary as
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
        conn.execute("UPDATE documents SET review_status = 'approved' WHERE id = ?", (doc_id,))
    register_test_user(client, "manualtech2@example.com")

    resp = client.get(f"/api/manuals/{doc_id}/file")
    assert resp.status_code == 200

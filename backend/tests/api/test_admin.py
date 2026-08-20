from fastapi.testclient import TestClient

from app.db import get_conn
from app.main import app

client = TestClient(app)


def _register_admin(email="admin@example.com"):
    resp = client.post("/api/auth/register", json={"email": email, "password": "password123"})
    assert resp.status_code == 201
    assert resp.json()["role"] == "administrator"


def _seed_document(conn) -> int:
    conn.execute("INSERT INTO manufacturers (id, name) VALUES (1, 'Bunn-O-Matic Corporation')")
    conn.execute("INSERT INTO machines (id, manufacturer_id, model_name) VALUES (1, 1, 'Axiom')")
    cur = conn.execute(
        "INSERT INTO documents (original_filename, storage_path, source_system, source_ref, "
        "file_type, sha256, byte_size, status, manufacturer_id, doc_type, title, is_current_revision) "
        "VALUES ('axiom.pdf', 'axiom.pdf', 'local_directory', 'axiom.pdf', 'pdf', 'hash1', 100, "
        "'indexed', 1, 'service_repair', 'Axiom Service Manual', 1)"
    )
    doc_id = cur.lastrowid
    conn.execute("INSERT INTO document_machines (document_id, machine_id) VALUES (?, 1)", (doc_id,))
    return doc_id


def test_list_documents_returns_seeded_document(test_env):
    with get_conn() as conn:
        doc_id = _seed_document(conn)
    _register_admin()

    resp = client.get("/api/admin/documents")
    assert resp.status_code == 200
    body = resp.json()
    assert any(d["id"] == doc_id for d in body)


def test_metadata_correction_updates_and_logs_audit_trail(test_env):
    with get_conn() as conn:
        doc_id = _seed_document(conn)
    _register_admin()

    resp = client.patch(
        f"/api/admin/documents/{doc_id}",
        json={"title": "Corrected Title", "reason": "Original title was auto-detected incorrectly."},
    )
    assert resp.status_code == 200
    assert resp.json()["title"] == "Corrected Title"

    with get_conn() as conn:
        override = conn.execute(
            "SELECT * FROM metadata_overrides WHERE document_id = ? AND field = 'title'", (doc_id,)
        ).fetchone()
    assert override is not None
    assert override["corrected_value"] == "Corrected Title"
    assert override["previous_value"] == "Axiom Service Manual"
    assert override["corrected_by"] == "admin@example.com"


def test_deactivate_document_removes_it_from_default_listing(test_env):
    with get_conn() as conn:
        doc_id = _seed_document(conn)
    _register_admin()

    resp = client.post(f"/api/admin/documents/{doc_id}/deactivate")
    assert resp.status_code == 200

    listing = client.get("/api/admin/documents").json()
    assert all(d["id"] != doc_id for d in listing)

    listing_with_deactivated = client.get("/api/admin/documents?include_deactivated=true").json()
    assert any(d["id"] == doc_id for d in listing_with_deactivated)


def test_deactivating_already_deactivated_document_404s(test_env):
    with get_conn() as conn:
        doc_id = _seed_document(conn)
    _register_admin()

    client.post(f"/api/admin/documents/{doc_id}/deactivate")
    resp = client.post(f"/api/admin/documents/{doc_id}/deactivate")
    assert resp.status_code == 404


def test_upload_rejects_disallowed_extension(test_env):
    _register_admin()
    resp = client.post(
        "/api/admin/documents/upload",
        files={"file": ("malicious.exe", b"not a real exe", "application/octet-stream")},
    )
    assert resp.status_code == 400


def test_upload_sanitizes_path_traversal_filename(test_env):
    _register_admin()
    resp = client.post(
        "/api/admin/documents/upload",
        files={"file": ("../../evil.pdf", b"%PDF-1.4 fake pdf bytes", "application/pdf")},
    )
    assert resp.status_code == 202
    stored_as = resp.json()["stored_as"]
    assert ".." not in stored_as
    assert "/" not in stored_as and "\\" not in stored_as


def test_query_test_endpoint_requires_admin(test_env):
    resp = client.post("/api/auth/register", json={"email": "admin4@example.com", "password": "password123"})
    assert resp.json()["role"] == "administrator"
    client.post("/api/auth/logout")
    client.post("/api/auth/register", json={"email": "tech@example.com", "password": "password123"})

    resp = client.post("/api/admin/query-test", json={"question": "test question"})
    assert resp.status_code == 403


def test_empty_duplicates_list_is_empty_not_error(test_env):
    _register_admin()
    resp = client.get("/api/admin/duplicates")
    assert resp.status_code == 200
    assert resp.json() == []

from fastapi.testclient import TestClient

from app.db import get_conn
from app.main import app
from tests.conftest import register_test_user

client = TestClient(app)


def _register_admin(email="admin@example.com"):
    # admin_email=email makes this specific address the bootstrap administrator
    # (created directly, no invite needed) rather than a second admin invited
    # by some other bootstrap identity -- keeps a single, predictable admin
    # per test the way the old first-HTTP-registrant behavior used to.
    return register_test_user(client, email, role="administrator", admin_email=email)


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


def test_direct_upload_endpoint_removed(test_env):
    """Ingestion is Drive-only now -- manuals go in the shared Drive folder,
    not through a local upload endpoint that could drift out of sync with it.
    405, not 404: PATCH /documents/{document_id} structurally matches the same
    path shape ("upload" parses as the path param), so Starlette reports
    method-not-allowed for POST rather than falling through to a 404."""
    _register_admin()
    resp = client.post(
        "/api/admin/documents/upload",
        files={"file": ("manual.pdf", b"%PDF-1.4 fake pdf bytes", "application/pdf")},
    )
    assert resp.status_code == 405


def test_query_test_endpoint_requires_admin(test_env):
    _register_admin("admin4@example.com")
    register_test_user(client, "tech@example.com", admin_email="admin4@example.com")

    resp = client.post("/api/admin/query-test", json={"question": "test question"})
    assert resp.status_code == 403


def test_empty_duplicates_list_is_empty_not_error(test_env):
    _register_admin()
    resp = client.get("/api/admin/duplicates")
    assert resp.status_code == 200
    assert resp.json() == []


# --- Review queue (P0-6) ---

def test_new_document_appears_in_review_queue_pending(test_env):
    with get_conn() as conn:
        doc_id = _seed_document(conn)
    _register_admin()

    resp = client.get("/api/admin/review-queue")
    assert resp.status_code == 200
    body = resp.json()
    entry = next((d for d in body if d["id"] == doc_id), None)
    assert entry is not None
    assert entry["review_status"] == "pending"
    assert entry["links"][0]["review_status"] == "pending"


def test_approving_document_and_link_removes_it_from_the_queue(test_env):
    with get_conn() as conn:
        doc_id = _seed_document(conn)
    _register_admin()

    resp = client.post(f"/api/admin/documents/{doc_id}/review", json={"decision": "approved"})
    assert resp.status_code == 200
    resp = client.post(f"/api/admin/documents/{doc_id}/machines/1/review", json={"decision": "approved"})
    assert resp.status_code == 200

    queue = client.get("/api/admin/review-queue").json()
    assert all(d["id"] != doc_id for d in queue)

    with get_conn() as conn:
        row = conn.execute("SELECT review_status, reviewed_by FROM documents WHERE id = ?", (doc_id,)).fetchone()
        audit = conn.execute(
            "SELECT event_type FROM audit_events WHERE target_type = 'document' AND target_id = ?", (doc_id,)
        ).fetchall()
    assert row["review_status"] == "approved"
    assert row["reviewed_by"] is not None
    assert any(a["event_type"] == "document_reviewed" for a in audit)


def test_rejecting_link_keeps_document_out_of_retrieval_via_queue(test_env):
    with get_conn() as conn:
        doc_id = _seed_document(conn)
    _register_admin()

    client.post(f"/api/admin/documents/{doc_id}/review", json={"decision": "approved"})
    resp = client.post(f"/api/admin/documents/{doc_id}/machines/1/review", json={"decision": "rejected"})
    assert resp.status_code == 200

    with get_conn() as conn:
        link = conn.execute(
            "SELECT review_status FROM document_machines WHERE document_id = ? AND machine_id = 1", (doc_id,)
        ).fetchone()
    assert link["review_status"] == "rejected"


def test_metadata_correction_approves_the_links_it_sets(test_env):
    """An admin explicitly setting machine_ids via PATCH is itself the human
    review those links get -- they must land approved, not pending, or every
    metadata correction would silently pull the document out of retrieval."""
    with get_conn() as conn:
        doc_id = _seed_document(conn)
    _register_admin()

    resp = client.patch(
        f"/api/admin/documents/{doc_id}",
        json={"machine_ids": [1], "reason": "Confirmed correct machine association."},
    )
    assert resp.status_code == 200

    with get_conn() as conn:
        link = conn.execute(
            "SELECT review_status, reviewed_by FROM document_machines WHERE document_id = ? AND machine_id = 1",
            (doc_id,),
        ).fetchone()
    assert link["review_status"] == "approved"
    assert link["reviewed_by"] is not None


# --- Invitations, disable/enable (P0-5) ---

def test_invitation_create_and_use(test_env):
    _register_admin()
    resp = client.post("/api/admin/invitations", json={"email": "newtech@example.com", "role": "technician"})
    assert resp.status_code == 201
    body = resp.json()
    assert body["token"]

    listing = client.get("/api/admin/invitations").json()
    entry = next(i for i in listing if i["id"] == body["id"])
    assert "token" not in entry or entry["token"] is None, "the raw token must never be listable again"


def test_invitation_creation_rejects_existing_email(test_env):
    _register_admin()
    resp = client.post("/api/admin/invitations", json={"email": "admin@example.com", "role": "technician"})
    assert resp.status_code == 409


def test_revoked_invitation_cannot_be_used(test_env):
    _register_admin()
    invite = client.post("/api/admin/invitations", json={"email": "revokeme@example.com"}).json()
    resp = client.post(f"/api/admin/invitations/{invite['id']}/revoke")
    assert resp.status_code == 200
    client.post("/api/auth/logout")

    reg = client.post(
        "/api/auth/register",
        json={"email": "revokeme@example.com", "password": "password123", "invite_token": invite["token"]},
    )
    assert reg.status_code == 403


def test_admin_cannot_disable_own_account(test_env):
    _register_admin()
    with get_conn() as conn:
        admin_id = conn.execute("SELECT id FROM users WHERE email = 'admin@example.com'").fetchone()["id"]
    resp = client.post(f"/api/admin/users/{admin_id}/disable")
    assert resp.status_code == 400


def test_machine_picker_excludes_machines_with_only_pending_links(test_env):
    """A pending-only document_machines link must not surface the machine in
    the picker at all -- otherwise a technician selects a machine that then
    dead-ends into "no manuals" the moment retrieval applies its own approval
    filter (independent follow-up review P0-6)."""
    with get_conn() as conn:
        conn.execute("INSERT INTO manufacturers (id, name) VALUES (1, 'Bunn-O-Matic Corporation')")
        conn.execute("INSERT INTO machines (id, manufacturer_id, model_name) VALUES (1, 1, 'Axiom')")
        conn.execute(
            "INSERT INTO documents (id, original_filename, storage_path, source_system, source_ref, "
            "file_type, sha256, byte_size, status) VALUES (1, 'axiom.pdf', 'axiom.pdf', "
            "'local_directory', 'axiom.pdf', 'pdf', 'hash1', 100, 'indexed')"
        )
        conn.execute("INSERT INTO document_machines (document_id, machine_id) VALUES (1, 1)")

    _register_admin("machadmin@example.com")
    register_test_user(client, "machtech@example.com", admin_email="machadmin@example.com")

    resp = client.get("/api/machines", params={"q": "Axiom"})
    assert resp.status_code == 200
    assert resp.json() == []

    client.post("/api/auth/login", json={"email": "machadmin@example.com", "password": "password123"})
    assert client.post("/api/admin/documents/1/review", json={"decision": "approved"}).status_code == 200
    assert client.post("/api/admin/documents/1/machines/1/review", json={"decision": "approved"}).status_code == 200

    client.post("/api/auth/login", json={"email": "machtech@example.com", "password": "password123"})
    resp2 = client.get("/api/machines", params={"q": "Axiom"})
    assert resp2.status_code == 200
    assert len(resp2.json()) == 1
    assert resp2.json()[0]["document_count"] == 1


def test_disable_and_enable_user_round_trip(test_env):
    _register_admin()
    register_test_user(client, "roundtrip@example.com", admin_email="admin@example.com")
    with get_conn() as conn:
        user_id = conn.execute("SELECT id FROM users WHERE email = 'roundtrip@example.com'").fetchone()["id"]
    _register_admin()

    assert client.post(f"/api/admin/users/{user_id}/disable").status_code == 200
    with get_conn() as conn:
        row = conn.execute("SELECT is_disabled, token_version FROM users WHERE id = ?", (user_id,)).fetchone()
    assert row["is_disabled"] == 1
    assert row["token_version"] == 1

    assert client.post(f"/api/admin/users/{user_id}/enable").status_code == 200
    with get_conn() as conn:
        row = conn.execute("SELECT is_disabled FROM users WHERE id = ?", (user_id,)).fetchone()
    assert row["is_disabled"] == 0

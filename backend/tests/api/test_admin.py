import threading

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
    conn.execute("INSERT INTO manufacturers (name) VALUES ('Bunn-O-Matic Corporation')")
    conn.execute("INSERT INTO machines (manufacturer_id, model_name) VALUES (1, 'Axiom')")
    cur = conn.execute(
        "INSERT INTO documents (original_filename, storage_path, source_system, source_ref, "
        "file_type, sha256, byte_size, status, manufacturer_id, doc_type, title, is_current_revision) "
        "VALUES ('axiom.pdf', 'axiom.pdf', 'local_directory', 'axiom.pdf', 'pdf', 'hash1', 100, "
        "'indexed', 1, 'service_repair', 'Axiom Service Manual', true) RETURNING id"
    )
    doc_id = cur.fetchone()["id"]
    conn.execute("INSERT INTO document_machines (document_id, machine_id) VALUES (%s, 1)", (doc_id,))
    # P0-02: review_document() now requires nonempty chunks before a document
    # can be approved -- every real 'indexed' document has at least one.
    conn.execute(
        "INSERT INTO chunks (document_id, chunk_type, content, char_count, ordinal) "
        "VALUES (%s, 'text', 'seeded chunk content', 21, 0)",
        (doc_id,),
    )
    return doc_id


def test_list_documents_returns_seeded_document(test_env):
    with get_conn() as conn:
        doc_id = _seed_document(conn)
    _register_admin()

    resp = client.get("/api/admin/documents")
    assert resp.status_code == 200
    body = resp.json()
    assert any(d["id"] == doc_id for d in body)


def test_document_at_current_pipeline_version_does_not_need_reprocessing(test_env):
    """Migration 0008 defaults extraction_version/chunking_version to 1,
    matching CURRENT_EXTRACTION_VERSION/CURRENT_CHUNKING_VERSION today -- the
    existing corpus must not be retroactively flagged as stale on upgrade."""
    with get_conn() as conn:
        doc_id = _seed_document(conn)
    _register_admin()

    doc = next(d for d in client.get("/api/admin/documents").json() if d["id"] == doc_id)
    assert doc["needs_reprocessing"] is False


def test_document_at_a_stale_pipeline_version_needs_reprocessing(test_env):
    """Independent follow-up review 2026-08-24 P0-7 (bounded slice): a
    document's pipeline version is now visible via the same listing an admin
    already uses to review documents, not buried only in ingestion_events."""
    with get_conn() as conn:
        doc_id = _seed_document(conn)
        conn.execute("UPDATE documents SET chunking_version = 0 WHERE id = %s", (doc_id,))
    _register_admin()

    doc = next(d for d in client.get("/api/admin/documents").json() if d["id"] == doc_id)
    assert doc["needs_reprocessing"] is True


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
            "SELECT * FROM metadata_overrides WHERE document_id = %s AND field = 'title'", (doc_id,)
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

    # P0-02: a document can only be approved once it has an approved machine
    # link -- link review must happen first.
    resp = client.post(f"/api/admin/documents/{doc_id}/machines/1/review", json={"decision": "approved"})
    assert resp.status_code == 200
    resp = client.post(f"/api/admin/documents/{doc_id}/review", json={"decision": "approved"})
    assert resp.status_code == 200

    queue = client.get("/api/admin/review-queue").json()
    assert all(d["id"] != doc_id for d in queue)

    with get_conn() as conn:
        row = conn.execute("SELECT review_status, reviewed_by FROM documents WHERE id = %s", (doc_id,)).fetchone()
        audit = conn.execute(
            "SELECT event_type FROM audit_events WHERE target_type = 'document' AND target_id = %s", (doc_id,)
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
            "SELECT review_status FROM document_machines WHERE document_id = %s AND machine_id = 1", (doc_id,)
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
            "SELECT review_status, reviewed_by FROM document_machines WHERE document_id = %s AND machine_id = 1",
            (doc_id,),
        ).fetchone()
    assert link["review_status"] == "approved"
    assert link["reviewed_by"] is not None


# --- Replacement cutover (independent follow-up review P0-2, 2026-08-24
# follow-up): approving a replacement is the moment it retires whatever it's
# superseding, not ingestion. ---

def _seed_document_at_source_ref(conn, source_ref, *, sha256, review_status="pending",
                                  status="indexed", title="Axiom Service Manual") -> int:
    # ON CONFLICT DO NOTHING (SQLite's INSERT OR IGNORE, ported) against
    # manufacturers.name's/machines' own UNIQUE constraints -- this helper is
    # called twice per test (old + new document at the same source_ref), and
    # unlike the explicit id=1 the old version forced, letting the first call
    # create manufacturer/machine id=1 and the second no-op is what actually
    # needs the conflict guard now.
    conn.execute(
        "INSERT INTO manufacturers (name) VALUES ('Bunn-O-Matic Corporation') ON CONFLICT (name) DO NOTHING"
    )
    conn.execute(
        "INSERT INTO machines (manufacturer_id, model_name) VALUES (1, 'Axiom') "
        "ON CONFLICT (manufacturer_id, model_name) DO NOTHING"
    )
    cur = conn.execute(
        "INSERT INTO documents (original_filename, storage_path, source_system, source_ref, "
        "file_type, sha256, byte_size, status, review_status, manufacturer_id, doc_type, title, "
        "is_current_revision, ingested_at) "
        "VALUES ('axiom.pdf', %s, 'google_drive', %s, 'pdf', %s, 100, %s, %s, 1, 'service_repair', %s, true, "
        "now()) RETURNING id",
        (sha256, source_ref, sha256, status, review_status, title),
    )
    doc_id = cur.fetchone()["id"]
    conn.execute("INSERT INTO document_machines (document_id, machine_id) VALUES (%s, 1)", (doc_id,))
    return doc_id


def test_approving_replacement_deactivates_old_document_at_same_source_ref(test_env):
    """Mirrors what _ingest_one actually leaves behind: the old approved
    document and the new pending replacement both active at the same
    source_ref. Approving the replacement must be the atomic cutover --
    the old one retires in the same request, not on some later admin
    pass, so there's never a moment with two approved documents (or,
    before this fix, zero) live at this source_ref."""
    source_ref = "google_drive:file123"
    with get_conn() as conn:
        old_id = _seed_document_at_source_ref(conn, source_ref, sha256="old-hash", review_status="approved")
        new_id = _seed_document_at_source_ref(conn, source_ref, sha256="new-hash", review_status="pending")
        # P0-02: review_document() now requires nonempty chunks and an
        # approved machine link before a document can be approved/promoted.
        conn.execute(
            "INSERT INTO chunks (document_id, chunk_type, content, char_count, ordinal) "
            "VALUES (%s, 'text', 'seeded chunk content', 21, 0)",
            (new_id,),
        )
    _register_admin()
    assert client.post(
        f"/api/admin/documents/{new_id}/machines/1/review", json={"decision": "approved"}
    ).status_code == 200

    resp = client.post(f"/api/admin/documents/{new_id}/review", json={"decision": "approved"})
    assert resp.status_code == 200

    with get_conn() as conn:
        old_row = conn.execute("SELECT deactivated_at, status_reason FROM documents WHERE id = %s", (old_id,)).fetchone()
        new_row = conn.execute("SELECT deactivated_at, review_status FROM documents WHERE id = %s", (new_id,)).fetchone()
        audit = conn.execute(
            "SELECT event_type FROM audit_events WHERE target_type = 'document' AND target_id = %s", (new_id,)
        ).fetchall()
    assert old_row["deactivated_at"] is not None, "the old document must be retired once its replacement is approved"
    assert "Superseded" in (old_row["status_reason"] or "")
    assert new_row["deactivated_at"] is None
    assert new_row["review_status"] == "approved"
    assert any(a["event_type"] == "document_superseded" for a in audit)


def test_rejecting_replacement_leaves_old_document_active(test_env):
    """The failure mode this whole fix exists to prevent: a replacement that
    turns out to be wrong must never take the working manual down with it."""
    source_ref = "google_drive:file456"
    with get_conn() as conn:
        old_id = _seed_document_at_source_ref(conn, source_ref, sha256="old-hash", review_status="approved")
        new_id = _seed_document_at_source_ref(conn, source_ref, sha256="new-hash", review_status="pending")
    _register_admin()

    resp = client.post(f"/api/admin/documents/{new_id}/review", json={"decision": "rejected"})
    assert resp.status_code == 200

    with get_conn() as conn:
        old_row = conn.execute("SELECT deactivated_at FROM documents WHERE id = %s", (old_id,)).fetchone()
        new_row = conn.execute("SELECT deactivated_at, review_status FROM documents WHERE id = %s", (new_id,)).fetchone()
    assert old_row["deactivated_at"] is None, "rejecting a replacement must not touch the document it targeted"
    assert new_row["deactivated_at"] is None, "a rejected document is kept (not deactivated) for the review record"
    assert new_row["review_status"] == "rejected"


def test_concurrent_approval_of_two_replacement_candidates_leaves_exactly_one_active(test_env, monkeypatch):
    """P1-6 (2026-08-24 independent follow-up review, "concurrent ...
    approval ... and promotion tests"): two admins approving two DIFFERENT
    pending replacement candidates at the SAME source_ref at nearly the same
    moment. Before the fix, the review_status UPDATE had no re-check against
    a concurrent supersession -- the loser's approval could still succeed
    after its document was already deactivated by the winner, then that
    loser's own supersede step would deactivate the winner's document too,
    leaving ZERO active approved documents at this source_ref (the exact
    failure mode P0-2 fixed for ingestion, reintroduced here). Exactly one
    of the two concurrent requests must succeed; the other must see a clean
    404, and exactly one document must end up active and approved.

    The vulnerable window is narrow: candidate_b's request must read
    "not yet deactivated" BEFORE candidate_a's request commits, then write
    AFTER it commits. Two bare threads rarely land there on their own -- the
    whole request (SELECT + UPDATE + commit) completes well within one GIL
    switch interval against this fast, local, WAL-mode DB, so an
    uninstrumented version of this test passed even against the unguarded
    code it's meant to catch. sqlite3.Connection.execute is patched for the
    duration of this test only (not the app's own code) so candidate_b's
    initial SELECT deterministically blocks until candidate_a's request has
    fully committed, before candidate_b's own UPDATE runs -- reproducing
    exactly the interleaving the original bug depended on."""
    source_ref = "google_drive:file789"
    with get_conn() as conn:
        old_id = _seed_document_at_source_ref(conn, source_ref, sha256="old-hash", review_status="approved")
        candidate_a = _seed_document_at_source_ref(conn, source_ref, sha256="candidate-a", review_status="pending")
        candidate_b = _seed_document_at_source_ref(conn, source_ref, sha256="candidate-b", review_status="pending")
        # P0-02: review_document() now requires nonempty chunks and an
        # approved machine link before a document can be approved/promoted --
        # both candidates need that BEFORE the race below, or every approval
        # attempt would 409 for a reason unrelated to what this test covers.
        for candidate_id in (candidate_a, candidate_b):
            conn.execute(
                "INSERT INTO chunks (document_id, chunk_type, content, char_count, ordinal) "
                "VALUES (%s, 'text', 'seeded chunk content', 21, 0)",
                (candidate_id,),
            )
    _register_admin()
    for candidate_id in (candidate_a, candidate_b):
        assert client.post(
            f"/api/admin/documents/{candidate_id}/machines/1/review", json={"decision": "approved"}
        ).status_code == 200

    from contextlib import contextmanager

    import app.api.routes_admin as routes_admin_module
    from app.db import get_conn as real_get_conn

    # Must match routes_admin.py's review_document() SQL text exactly --
    # %s placeholders (psycopg), not SQLite's ?.
    # Must match review_document()'s SQL text exactly -- P0-02 added `status`
    # to this SELECT's column list, which silently broke this comparison
    # (the barrier below never fired, so the two threads raced with no
    # synchronization at all instead of the deliberate interleaving this
    # test depends on).
    select_sql = "SELECT id, source_ref, status FROM documents WHERE id = %s AND deactivated_at IS NULL"
    # Both requests' reads must land before either commits (a Barrier makes
    # that deterministic instead of hoping thread scheduling cooperates);
    # only THEN does candidate_b additionally wait for candidate_a's full
    # commit before candidate_b's own write proceeds -- reproducing "read
    # stale, write late" exactly. psycopg.Connection itself can't be
    # monkeypatched (it's a C-backed type, like sqlite3.Connection was), so
    # the connection this route sees is wrapped in a thin Python proxy instead.
    both_read = threading.Barrier(2, timeout=5)
    a_committed = threading.Event()

    class _InstrumentedConn:
        def __init__(self, real_conn):
            self._real_conn = real_conn

        def execute(self, sql, params=()):
            result = self._real_conn.execute(sql, params)
            if sql == select_sql and params and params[0] in (candidate_a, candidate_b):
                both_read.wait(timeout=5)
                if params[0] == candidate_b:
                    a_committed.wait(timeout=5)
            return result

        def __getattr__(self, name):
            return getattr(self._real_conn, name)

    @contextmanager
    def patched_get_conn():
        with real_get_conn() as conn:
            yield _InstrumentedConn(conn)

    monkeypatch.setattr(routes_admin_module, "get_conn", patched_get_conn)

    responses = {}

    def approve_a():
        responses["a"] = client.post(f"/api/admin/documents/{candidate_a}/review", json={"decision": "approved"})
        a_committed.set()

    def approve_b():
        responses["b"] = client.post(f"/api/admin/documents/{candidate_b}/review", json={"decision": "approved"})

    threads = [threading.Thread(target=approve_a), threading.Thread(target=approve_b)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    statuses = sorted(r.status_code for r in responses.values())
    assert statuses == [200, 404], "exactly one concurrent approval succeeds, the other is rejected as already-superseded"

    with get_conn() as conn:
        active_approved = conn.execute(
            "SELECT id FROM documents WHERE source_ref = %s AND deactivated_at IS NULL "
            "AND review_status = 'approved'",
            (source_ref,),
        ).fetchall()
        all_rows = {
            r["id"]: (r["review_status"], r["deactivated_at"])
            for r in conn.execute(
                "SELECT id, review_status, deactivated_at FROM documents WHERE source_ref = %s", (source_ref,)
            ).fetchall()
        }
    assert len(active_approved) == 1, (
        f"expected exactly one active approved document at this source_ref, got {len(active_approved)}: {all_rows}"
    )
    assert old_id not in {r["id"] for r in active_approved}, "the old document must have been superseded"
    # old_id legitimately ends up 'approved' (its real historical review
    # decision, never revoked) AND deactivated (retired once a newer
    # replacement was approved) -- that combination is expected for a
    # superseded document. The property that must never hold is a LOSING
    # CANDIDATE ending up simultaneously 'approved' and deactivated in the
    # same race -- that ghost state (its own approval "succeeding" into a
    # row that was already retired) is exactly what the unguarded race used
    # to produce.
    for doc_id in (candidate_a, candidate_b):
        review_status, deactivated_at = all_rows[doc_id]
        assert not (review_status == "approved" and deactivated_at is not None), (
            f"candidate document {doc_id} is both approved and deactivated -- ghost state from the race"
        )


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


def test_concurrent_registration_with_the_same_invite_token_only_succeeds_once(test_env):
    """P1-6 (2026-08-24 independent follow-up review, "concurrent ...
    invitation ... tests"): two requests racing to register with the SAME
    single-use invite token. Before this fix, the invite was consumed with
    a check-then-act read followed by an unconditional UPDATE at the end --
    both requests could read used_at=NULL and both proceed to INSERT a
    user, with users.email's UNIQUE constraint as the only thing stopping a
    duplicate account; the loser then raised an unhandled
    sqlite3.IntegrityError (a 500, not the same clean 403 every other
    invitation-rejection path returns) instead of failing cleanly. The
    invite is now claimed atomically via the same claim-UPDATE pattern used
    everywhere else in this codebase, which is race-safe under any
    interleaving -- unlike the P0-2 approval race above, this needs no
    forced-interleaving instrumentation to demonstrate."""
    _register_admin()
    invite = client.post("/api/admin/invitations", json={"email": "racer@example.com"}).json()
    token = invite["token"]
    client.post("/api/auth/logout")

    responses = []

    def register():
        responses.append(client.post(
            "/api/auth/register",
            json={"email": "racer@example.com", "password": "password123", "invite_token": token},
        ))

    threads = [threading.Thread(target=register) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    statuses = sorted(r.status_code for r in responses)
    assert statuses == [201, 403], "exactly one registration succeeds, the other is cleanly rejected as already-used"

    with get_conn() as conn:
        count = conn.execute("SELECT COUNT(*) AS n FROM users WHERE email = 'racer@example.com'").fetchone()["n"]
    assert count == 1, "the losing request must never create a second account"


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
        conn.execute("INSERT INTO manufacturers (name) VALUES ('Bunn-O-Matic Corporation')")
        conn.execute("INSERT INTO machines (manufacturer_id, model_name) VALUES (1, 'Axiom')")
        conn.execute(
            "INSERT INTO documents (original_filename, storage_path, source_system, source_ref, "
            "file_type, sha256, byte_size, status) VALUES ('axiom.pdf', 'axiom.pdf', "
            "'local_directory', 'axiom.pdf', 'pdf', 'hash1', 100, 'indexed')"
        )
        conn.execute("INSERT INTO document_machines (document_id, machine_id) VALUES (1, 1)")
        # P0-02: review_document() now requires nonempty chunks and an
        # approved machine link before a document can be approved.
        conn.execute(
            "INSERT INTO chunks (document_id, chunk_type, content, char_count, ordinal) "
            "VALUES (1, 'text', 'seeded chunk content', 21, 0)"
        )

    _register_admin("machadmin@example.com")
    register_test_user(client, "machtech@example.com", admin_email="machadmin@example.com")

    resp = client.get("/api/machines", params={"q": "Axiom"})
    assert resp.status_code == 200
    assert resp.json() == []

    client.post("/api/auth/login", json={"email": "machadmin@example.com", "password": "password123"})
    assert client.post("/api/admin/documents/1/machines/1/review", json={"decision": "approved"}).status_code == 200
    assert client.post("/api/admin/documents/1/review", json={"decision": "approved"}).status_code == 200

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
        row = conn.execute("SELECT is_disabled, token_version FROM users WHERE id = %s", (user_id,)).fetchone()
    assert row["is_disabled"] == 1
    assert row["token_version"] == 1

    assert client.post(f"/api/admin/users/{user_id}/enable").status_code == 200
    with get_conn() as conn:
        row = conn.execute("SELECT is_disabled FROM users WHERE id = %s", (user_id,)).fetchone()
    assert row["is_disabled"] == 0

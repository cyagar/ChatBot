"""Regressions from the 2026-09-21 external release review (FinalChanges.txt),
tracked one test per finding ID so each fix stays independently verifiable.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_p1_24_login_rate_limit_cannot_be_bypassed_by_rotating_cookies(test_env, monkeypatch):
    """P1-24: the default rate-limit key function keys on whatever tma_session
    cookie value a client happens to send, without verifying it's a real
    signed session -- login/register are exactly the routes an unauthenticated
    attacker controls that cookie completely for. Reproduced before the fix:
    12 login attempts, each with a different unsigned/garbage cookie, all
    returned 401 with no 429 at all, while the same 12 attempts with NO cookie
    correctly hit 429 after AUTH_RATE_LIMIT (10/minute) was exceeded. Fixed by
    routing /api/auth/login and /api/auth/register through a dedicated
    key_func (app/rate_limit.py's auth_key_func) that always keys on IP,
    applied per-route via @limiter.limit(..., key_func=...), leaving the
    session-aware default key_func for other rate-limited routes unchanged."""
    monkeypatch.setenv("RATE_LIMIT_PER_MINUTE", "60")  # irrelevant here; AUTH_RATE_LIMIT is fixed at 10/minute
    from app.config import get_settings
    get_settings.cache_clear()

    statuses = []
    for i in range(11):
        resp = client.post(
            "/api/auth/login",
            json={"email": "nobody@hmwagner.com", "password": "wrong-password"},
            cookies={"tma_session": f"garbage-token-{i}"},
        )
        statuses.append(resp.status_code)

    assert statuses[:10] == [401] * 10, f"expected 10 failed-credentials responses, got {statuses[:10]}"
    assert statuses[10] == 429, (
        f"rotating a different fake cookie on every request must NOT reset the rate-limit bucket "
        f"-- expected the 11th attempt to be throttled, got status sequence {statuses}"
    )


def test_p0_03_a_metadata_only_correction_does_not_touch_machine_links(test_env):
    """P0-03: the admin editor's machine picker sends machine_ids on every
    Save, even for a pure title/revision correction, because the checkbox
    group is pre-checked from ALL existing links (approved, pending, AND
    rejected) with no way to tell them apart -- so a title-only fix silently
    re-approved a previously-rejected link (PATCH /documents/{id} treats a
    present machine_ids as the admin's deliberate human review, inserting
    every sent id as review_status='approved', confidence=1.0, after
    deleting every existing document_machines row first). Reproduced before
    the fix: PATCH-ing only `title` with `machine_ids` unconditionally
    included (the old frontend behavior, reproduced directly against the
    API here since the JS itself isn't exercised by pytest) flipped a
    rejected link back to approved. Fixed on the frontend (admin.js) by
    omitting machine_ids from the payload entirely unless the admin actually
    interacted with the picker -- this test proves the API-level contract
    that fix relies on: omitting machine_ids (sending it as JSON null, which
    Pydantic treats identically to the field being absent) must leave
    existing links, rejected ones included, completely untouched."""
    from app.auth.security import hash_password
    from app.db import get_conn
    from app.main import app as fastapi_app
    from fastapi.testclient import TestClient
    from tests.conftest import register_test_user

    local_client = TestClient(fastapi_app)
    register_test_user(local_client, "admin-p003@example.com", role="administrator", admin_email="admin-p003@example.com")

    with get_conn() as conn:
        conn.execute("INSERT INTO manufacturers (name) VALUES ('Bunn-O-Matic Corporation')")
        conn.execute("INSERT INTO machines (manufacturer_id, model_name) VALUES (1, 'Axiom')")
        cur = conn.execute(
            "INSERT INTO documents (original_filename, storage_path, source_system, source_ref, "
            "file_type, sha256, byte_size, status, manufacturer_id, doc_type, title, is_current_revision) "
            "VALUES ('axiom.pdf', 'axiom.pdf', 'local_directory', 'axiom.pdf', 'pdf', 'hash1', 100, "
            "'indexed', 1, 'service_repair', 'Old Title', true) RETURNING id"
        )
        doc_id = cur.fetchone()["id"]
        conn.execute("INSERT INTO document_machines (document_id, machine_id) VALUES (%s, 1)", (doc_id,))

    reject = local_client.post(f"/api/admin/documents/{doc_id}/machines/1/review", json={"decision": "rejected"})
    assert reject.status_code == 200

    # A title-only correction -- machine_ids explicitly omitted (JSON null),
    # exactly what the fixed frontend now sends when the picker was never
    # touched.
    patch = local_client.patch(
        f"/api/admin/documents/{doc_id}",
        json={"title": "New Title", "machine_ids": None, "reason": "Fixing a typo in the title"},
    )
    assert patch.status_code == 200
    assert patch.json()["title"] == "New Title"

    with get_conn() as conn:
        row = conn.execute(
            "SELECT review_status FROM document_machines WHERE document_id = %s AND machine_id = 1", (doc_id,)
        ).fetchone()
    assert row["review_status"] == "rejected", (
        "a title-only correction must not touch machine link review state -- "
        f"got {row['review_status']!r}"
    )

    # Control case, proving this is a real danger and not just an unused code
    # path: this is exactly what the OLD frontend sent on every save
    # (machine_ids always present, pre-checked from every existing link
    # including rejected ones) -- confirms the backend really does silently
    # re-approve on a present machine_ids, which is why the frontend fix
    # (omitting it) is the correct place to have fixed this.
    patch2 = local_client.patch(
        f"/api/admin/documents/{doc_id}",
        json={"title": "New Title 2", "machine_ids": [1], "reason": "Old frontend behavior, for contrast"},
    )
    assert patch2.status_code == 200
    with get_conn() as conn:
        row2 = conn.execute(
            "SELECT review_status FROM document_machines WHERE document_id = %s AND machine_id = 1", (doc_id,)
        ).fetchone()
    assert row2["review_status"] == "approved", "sanity check: a present machine_ids really does re-approve"


def test_p1_04_stale_corpus_status_accepts_a_real_psycopg_datetime(test_env, monkeypatch):
    """P1-04: _corpus_status's stale-corpus check called
    datetime.fromisoformat() on ingestion_runs.finished_at, a TIMESTAMPTZ
    column psycopg already returns as a real, aware datetime (not a string)
    -- fromisoformat(datetime_obj) raised TypeError every time, and the
    function's own blanket `except Exception: return "ok", ""` silently
    swallowed it, so a corpus that hadn't synced in days (or ever) always
    reported healthy. Reproduced directly against this test before the fix
    (asserted "ok" for a 2020 run, which passed -- the bug). Fixed by using
    the datetime value directly instead of re-parsing it as a string."""
    from app.api.routes_config import _corpus_status
    from app.config import get_settings
    from app.db import get_conn

    monkeypatch.setenv("GOOGLE_DRIVE_FOLDER_ID", "test-folder-id")
    monkeypatch.setenv("INGESTION_STALENESS_THRESHOLD_HOURS", "48")
    get_settings.cache_clear()
    settings = get_settings()

    with get_conn() as conn:
        conn.execute(
            "INSERT INTO ingestion_runs (started_at, finished_at, status) "
            "VALUES (now() - interval '200 hours', now() - interval '199 hours', 'completed')"
        )
    status_, message = _corpus_status(settings)
    assert status_ == "degraded", f"a sync from 199 hours ago must not report healthy, got ({status_!r}, {message!r})"

    with get_conn() as conn:
        conn.execute(
            "INSERT INTO ingestion_runs (started_at, finished_at, status) "
            "VALUES (now() - interval '1 hour', now(), 'completed')"
        )
    status_, message = _corpus_status(settings)
    assert status_ == "ok", f"a just-completed sync must report healthy, got ({status_!r}, {message!r})"

    get_settings.cache_clear()


def test_p0_02_approving_an_unready_replacement_does_not_retire_the_working_manual(test_env):
    """P0-02: "Approve document" and "Approve link" are two independent
    buttons on the same review-queue card (admin.js renderReviewQueue) --
    nothing stops an admin clicking the former first. Before the fix,
    approving the document record alone was enough for review_document() to
    immediately deactivate every other active document at the same
    source_ref, even when the newly-approved document had failed ingestion,
    extracted zero chunks, or had no approved machine link of its own yet --
    leaving technicians with nothing retrievable at that source_ref (the old
    revision retired, the new one not actually servable) until the admin
    came back and separately approved a link. Fixed by making promotion a
    single atomic transition: review_document() now verifies status is
    indexed/partial, chunk_count > 0, and at least one approved
    document_machines link BEFORE retiring the prior revision, and rejects
    with 409 (touching nothing) if any condition isn't met yet."""
    from app.db import get_conn
    from app.main import app as fastapi_app
    from fastapi.testclient import TestClient
    from tests.conftest import register_test_user

    local_client = TestClient(fastapi_app)
    register_test_user(local_client, "admin-p002@example.com", role="administrator", admin_email="admin-p002@example.com")

    with get_conn() as conn:
        conn.execute("INSERT INTO manufacturers (name) VALUES ('Bunn-O-Matic Corporation')")
        conn.execute("INSERT INTO machines (manufacturer_id, model_name) VALUES (1, 'Axiom')")

        # The old, working revision: indexed, approved, with an approved
        # machine link -- this is what's actually serving technicians today.
        old_cur = conn.execute(
            "INSERT INTO documents (original_filename, storage_path, source_system, source_ref, "
            "file_type, sha256, byte_size, status, manufacturer_id, doc_type, title, is_current_revision, "
            "review_status) VALUES ('axiom.pdf', 'axiom.pdf', 'local_directory', 'axiom.pdf', 'pdf', "
            "'hash-old', 100, 'indexed', 1, 'service_repair', 'Axiom Manual', true, 'approved') RETURNING id"
        )
        old_doc_id = old_cur.fetchone()["id"]
        conn.execute(
            "INSERT INTO document_machines (document_id, machine_id, review_status) VALUES (%s, 1, 'approved')",
            (old_doc_id,),
        )
        conn.execute(
            "INSERT INTO chunks (document_id, chunk_type, content, char_count, ordinal) "
            "VALUES (%s, 'text', 'brew temperature is 200F', 25, 0)",
            (old_doc_id,),
        )

        # The replacement candidate at the SAME source_ref: ingested, but
        # its machine link hasn't been reviewed yet (still 'pending') --
        # exactly the state right after a fresh ingestion run, before any
        # admin review has happened at all.
        new_cur = conn.execute(
            "INSERT INTO documents (original_filename, storage_path, source_system, source_ref, "
            "file_type, sha256, byte_size, status, manufacturer_id, doc_type, title, is_current_revision) "
            "VALUES ('axiom.pdf', 'axiom.pdf', 'local_directory', 'axiom.pdf', 'pdf', 'hash-new', 100, "
            "'indexed', 1, 'service_repair', 'Axiom Manual v2', false) RETURNING id"
        )
        new_doc_id = new_cur.fetchone()["id"]
        conn.execute(
            "INSERT INTO document_machines (document_id, machine_id, review_status) VALUES (%s, 1, 'pending')",
            (new_doc_id,),
        )
        conn.execute(
            "INSERT INTO chunks (document_id, chunk_type, content, char_count, ordinal) "
            "VALUES (%s, 'text', 'brew temperature is 205F', 25, 0)",
            (new_doc_id,),
        )

    # The admin clicks "Approve document" on the replacement before ever
    # looking at its machine link.
    resp = local_client.post(f"/api/admin/documents/{new_doc_id}/review", json={"decision": "approved"})
    assert resp.status_code == 409, (
        f"approving a document with no approved machine link must be rejected, not promoted -- got "
        f"{resp.status_code}: {resp.text}"
    )

    with get_conn() as conn:
        old_row = conn.execute(
            "SELECT deactivated_at, review_status FROM documents WHERE id = %s", (old_doc_id,)
        ).fetchone()
        new_row = conn.execute("SELECT review_status FROM documents WHERE id = %s", (new_doc_id,)).fetchone()
    assert old_row["deactivated_at"] is None, "the still-working old revision must not have been retired"
    assert old_row["review_status"] == "approved", "the old revision's own approval must be untouched"
    assert new_row["review_status"] == "pending", (
        "a rejected promotion must leave the candidate's review_status alone so it still shows up "
        f"in the review queue for the admin to fix -- got {new_row['review_status']!r}"
    )

    # Now the admin does it in the right order: approve the machine link
    # first, then the document. Promotion should proceed normally.
    link_resp = local_client.post(
        f"/api/admin/documents/{new_doc_id}/machines/1/review", json={"decision": "approved"}
    )
    assert link_resp.status_code == 200

    resp2 = local_client.post(f"/api/admin/documents/{new_doc_id}/review", json={"decision": "approved"})
    assert resp2.status_code == 200, f"a ready replacement (indexed, chunked, approved link) must promote -- got {resp2.status_code}: {resp2.text}"

    with get_conn() as conn:
        old_row2 = conn.execute("SELECT deactivated_at FROM documents WHERE id = %s", (old_doc_id,)).fetchone()
    assert old_row2["deactivated_at"] is not None, "once the replacement is actually ready, the old revision should be retired"

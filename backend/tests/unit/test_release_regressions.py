"""Regressions from the 2026-09-21 external release review (FinalChanges.txt),
tracked one test per finding ID so each fix stays independently verifiable.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.db import get_conn
from app.main import app
from tests.conftest import register_test_user

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


def test_p0_05_a_switching_machine_while_an_answer_is_in_flight_is_rejected(test_env):
    """P0-05 (part A): set_conversation_machine used to update
    conversations.machine_id unconditionally, even with no pending
    clarification to resume -- an answer still generating for the OLD
    machine would finish and persist into a conversation now pointed at a
    DIFFERENT machine, producing cross-machine context on the next question.
    Fixed by rejecting the switch with 409 whenever is_processing is true
    and there is no pending clarification (the one case that's supposed to
    change the machine while processing).

    processing_claimed_at is set to now() (a fresh, unexpired lease) --
    under P0-04's lease model, is_processing=true with NO claimed_at is
    treated as an abandoned pre-lease-migration row and is immediately
    reclaimable (see test_p0_04_a), so a genuinely in-flight claim must
    look like a real one to exercise this specific 409 path."""
    from app.db import get_conn
    from app.main import app as fastapi_app
    from fastapi.testclient import TestClient
    from tests.conftest import register_test_user

    local_client = TestClient(fastapi_app)
    register_test_user(local_client, "tech-p005a@example.com")

    with get_conn() as conn:
        conn.execute("INSERT INTO manufacturers (name) VALUES ('Bunn-O-Matic Corporation')")
        conn.execute("INSERT INTO machines (manufacturer_id, model_name) VALUES (1, 'Axiom')")
        conn.execute("INSERT INTO machines (manufacturer_id, model_name) VALUES (1, 'Imix')")
        user_id = conn.execute("SELECT id FROM users WHERE email = 'tech-p005a@example.com'").fetchone()["id"]
        conv_cur = conn.execute(
            "INSERT INTO conversations (user_id, machine_id, is_processing, processing_attempt_id, "
            "processing_claimed_at) VALUES (%s, 1, true, 'live-attempt', now()) RETURNING id",
            (user_id,),
        )
        conv_id = conv_cur.fetchone()["id"]

    resp = local_client.post(f"/api/conversations/{conv_id}/machine", json={"machine_id": 2})
    assert resp.status_code == 409, (
        f"switching machine mid-answer with no pending clarification must be rejected -- "
        f"got {resp.status_code}: {resp.text}"
    )

    with get_conn() as conn:
        row = conn.execute("SELECT machine_id FROM conversations WHERE id = %s", (conv_id,)).fetchone()
    assert row["machine_id"] == 1, "a rejected switch must not have changed the conversation's machine"


def test_p0_05_b_retry_uses_the_failed_answers_own_machine_not_the_conversations_current_one(monkeypatch, test_env):
    """P0-05 (part B): retry_failed_answer used to read conv["machine_id"]
    -- the conversation's CURRENT machine -- rather than the machine this
    particular failed answer was actually generated against. If the
    technician legally switches machines afterward (allowed once
    is_processing is back to false) and then retries this OLDER failed
    answer, it must still regenerate against the ORIGINAL machine, not
    silently apply a different machine's advice to it. Fixed by reading
    machine_id off the failed message row itself instead of the
    conversation."""
    import app.api.routes_chat as routes_chat
    from app.db import get_conn
    from app.main import app as fastapi_app
    from app.providers.base import AIProvider, GeneratedAnswer
    from fastapi.testclient import TestClient
    from tests.conftest import register_test_user

    calls = []

    def _fake_hybrid_search(query, machine_id, top_k=6):
        calls.append(machine_id)
        return []

    class _NoAnswerProvider(AIProvider):
        name = "test_no_answer"

        def generate(self, question, machine_label, passages, history=None):
            return GeneratedAnswer(answer="No answer.", citations=[], provider=self.name, is_no_answer=True)

    monkeypatch.setattr(routes_chat, "hybrid_search", _fake_hybrid_search)
    monkeypatch.setattr(routes_chat, "get_provider", lambda: _NoAnswerProvider())

    local_client = TestClient(fastapi_app)
    register_test_user(local_client, "tech-p005b@example.com")

    with get_conn() as conn:
        conn.execute("INSERT INTO manufacturers (name) VALUES ('Bunn-O-Matic Corporation')")
        conn.execute("INSERT INTO machines (manufacturer_id, model_name) VALUES (1, 'Axiom')")
        conn.execute("INSERT INTO machines (manufacturer_id, model_name) VALUES (1, 'Imix')")
        user_id = conn.execute("SELECT id FROM users WHERE email = 'tech-p005b@example.com'").fetchone()["id"]
        # machine_id=1 originally -- the failed answer below was generated
        # against machine 1.
        conv_cur = conn.execute(
            "INSERT INTO conversations (user_id, machine_id, is_processing) VALUES (%s, 1, false) RETURNING id",
            (user_id,),
        )
        conv_id = conv_cur.fetchone()["id"]
        conn.execute(
            "INSERT INTO messages (conversation_id, role, content, machine_id) VALUES (%s, 'user', 'brew temp?', 1)",
            (conv_id,),
        )
        msg_cur = conn.execute(
            "INSERT INTO messages (conversation_id, role, content, machine_id, answer_status) "
            "VALUES (%s, 'assistant', '', 1, 'failed') RETURNING id",
            (conv_id,),
        )
        failed_msg_id = msg_cur.fetchone()["id"]
        # Technician legally switches machines afterward (processing already
        # finished) -- exactly the state that used to fool retry.
        conn.execute("UPDATE conversations SET machine_id = 2 WHERE id = %s", (conv_id,))

    resp = local_client.post(f"/api/conversations/{conv_id}/messages/{failed_msg_id}/retry")
    assert resp.status_code == 200, resp.text
    assert calls == [1], (
        f"retry must regenerate against the failed answer's OWN machine (1), not the conversation's "
        f"current one (2) -- hybrid_search was called with machine_id={calls}"
    )


def test_p0_13_withdrawing_a_source_document_retroactively_flags_history_and_saved_answers(test_env):
    """P0-13: message hydration used to return historical answer text and
    citations with no indication that the cited document had since been
    withdrawn (emergency deactivation) or lost approval (re-rejected) --
    both the live conversation history and the saved-answers list kept
    showing withdrawn content with no warning at all. Fixed by computing
    each citation's source_withdrawn (and the message-level
    has_withdrawn_source) fresh on every hydration, from the document's
    CURRENT state, not what it was when the answer was generated."""
    from app.auth.security import hash_password
    from app.db import get_conn
    from app.main import app as fastapi_app
    from fastapi.testclient import TestClient
    from tests.conftest import register_test_user

    local_client = TestClient(fastapi_app)
    register_test_user(local_client, "tech-p013@example.com")

    with get_conn() as conn:
        conn.execute("INSERT INTO manufacturers (name) VALUES ('Bunn-O-Matic Corporation')")
        conn.execute("INSERT INTO machines (manufacturer_id, model_name) VALUES (1, 'Axiom')")
        doc_cur = conn.execute(
            "INSERT INTO documents (original_filename, storage_path, source_system, source_ref, "
            "file_type, sha256, byte_size, status, manufacturer_id, doc_type, title, is_current_revision, "
            "review_status) VALUES ('axiom.pdf', 'axiom.pdf', 'local_directory', 'axiom.pdf', 'pdf', "
            "'hash-p013', 100, 'indexed', 1, 'service_repair', 'Axiom Manual', true, 'approved') RETURNING id"
        )
        doc_id = doc_cur.fetchone()["id"]
        chunk_cur = conn.execute(
            "INSERT INTO chunks (document_id, chunk_type, content, char_count, ordinal) "
            "VALUES (%s, 'text', 'brew temperature is 200F', 25, 0) RETURNING id",
            (doc_id,),
        )
        chunk_id = chunk_cur.fetchone()["id"]

        user_id = conn.execute("SELECT id FROM users WHERE email = 'tech-p013@example.com'").fetchone()["id"]
        conv_cur = conn.execute(
            "INSERT INTO conversations (user_id, machine_id) VALUES (%s, 1) RETURNING id", (user_id,)
        )
        conv_id = conv_cur.fetchone()["id"]
        msg_cur = conn.execute(
            "INSERT INTO messages (conversation_id, role, content, machine_id) "
            "VALUES (%s, 'assistant', 'Brew at 200F.', 1) RETURNING id",
            (conv_id,),
        )
        msg_id = msg_cur.fetchone()["id"]
        conn.execute(
            "INSERT INTO message_sources (message_id, chunk_id, rank, is_citation, citation_ordinal, excerpt) "
            "VALUES (%s, %s, 1, true, 1, 'brew temperature is 200F')",
            (msg_id, chunk_id),
        )
        conn.execute(
            "INSERT INTO saved_answers (user_id, message_id) VALUES (%s, %s)", (user_id, msg_id)
        )

    # Before withdrawal: neither the live history nor the saved-answers list
    # should flag anything.
    before = local_client.get(f"/api/conversations/{conv_id}/messages")
    assert before.status_code == 200
    before_msg = next(m for m in before.json() if m["id"] == msg_id)
    assert before_msg["has_withdrawn_source"] is False
    assert before_msg["citations"][0]["source_withdrawn"] is False

    saved_before = local_client.get("/api/saved-answers")
    assert saved_before.status_code == 200
    assert saved_before.json()[0]["answer"]["has_withdrawn_source"] is False

    # Emergency withdrawal: deactivate the document.
    with get_conn() as conn:
        conn.execute("UPDATE documents SET deactivated_at = now() WHERE id = %s", (doc_id,))

    after = local_client.get(f"/api/conversations/{conv_id}/messages")
    assert after.status_code == 200
    after_msg = next(m for m in after.json() if m["id"] == msg_id)
    assert after_msg["has_withdrawn_source"] is True, (
        "a deactivated source must retroactively flag every historical answer that cited it"
    )
    assert after_msg["citations"][0]["source_withdrawn"] is True

    saved_after = local_client.get("/api/saved-answers")
    assert saved_after.status_code == 200
    assert saved_after.json()[0]["answer"]["has_withdrawn_source"] is True, (
        "a saved answer (technician bookmark) must also be retroactively flagged, not just live history"
    )


def test_p0_04_a_an_expired_processing_lease_can_be_reclaimed_instead_of_blocking_forever(monkeypatch, test_env):
    """P0-04 (part A): the old plain is_processing boolean was cleared only
    in a Python `finally` -- a killed worker, a lost DB connection during
    release, or a shutdown between claim and `finally` left it true forever,
    rejecting every future question/retry with 409 with no way out. Fixed by
    turning the claim into a lease: a claim older than
    PROCESSING_LEASE_SECONDS is now reclaimable by a later request instead
    of blocking indefinitely. Reproduces the stuck state directly (no actual
    process kill needed -- the lease's age is what matters, not how it got
    old)."""
    import app.api.routes_chat as routes_chat
    from app.db import get_conn
    from app.main import app as fastapi_app
    from app.providers.base import AIProvider, GeneratedAnswer
    from fastapi.testclient import TestClient
    from tests.conftest import register_test_user

    monkeypatch.setattr(routes_chat, "hybrid_search", lambda *a, **k: [])

    class _NoAnswerProvider(AIProvider):
        name = "test_no_answer"

        def generate(self, question, machine_label, passages, history=None):
            return GeneratedAnswer(answer="No answer.", citations=[], provider=self.name, is_no_answer=True)

    monkeypatch.setattr(routes_chat, "get_provider", lambda: _NoAnswerProvider())

    local_client = TestClient(fastapi_app)
    register_test_user(local_client, "tech-p004a@example.com")

    with get_conn() as conn:
        conn.execute("INSERT INTO manufacturers (name) VALUES ('Bunn-O-Matic Corporation')")
        conn.execute("INSERT INTO machines (manufacturer_id, model_name) VALUES (1, 'Axiom')")
        user_id = conn.execute("SELECT id FROM users WHERE email = 'tech-p004a@example.com'").fetchone()["id"]
        conv_cur = conn.execute(
            "INSERT INTO conversations (user_id, machine_id, is_processing, processing_attempt_id, "
            "processing_claimed_at) VALUES (%s, 1, true, 'dead-attempt', now() - interval '10 minutes') "
            "RETURNING id",
            (user_id,),
        )
        conv_id = conv_cur.fetchone()["id"]

    resp = local_client.post(
        f"/api/conversations/{conv_id}/messages", json={"content": "what is the brew temperature?"}
    )
    assert resp.status_code != 409, (
        f"a processing claim older than the lease duration must be reclaimable, not block every future "
        f"question forever -- got {resp.status_code}: {resp.text}"
    )


def test_p0_04_b_a_zombie_attempts_late_write_never_overwrites_the_reclaiming_attempts_answer(monkeypatch, test_env):
    """P0-04 (part B): the property fencing actually exists for. Attempt A
    claims the lease; its lease then expires (its provider call is still
    running -- slow, not dead) and attempt B reclaims the SAME conversation
    and successfully persists a real answer; THEN A's slow provider call
    finally returns and tries to persist too. Without a fencing token on the
    write itself, A's late write would silently overwrite or duplicate B's
    already-persisted answer -- exactly the hazard a naive
    reclaim-without-fencing implementation would pass by accident (only one
    attempt ever runs in most tests) but fail for real."""
    import app.api.routes_chat as routes_chat
    from app.db import get_conn
    from app.main import app as fastapi_app
    from app.providers.base import AIProvider, GeneratedAnswer
    from fastapi.testclient import TestClient
    from tests.conftest import register_test_user

    local_client = TestClient(fastapi_app)
    register_test_user(local_client, "tech-p004b@example.com")

    with get_conn() as conn:
        conn.execute("INSERT INTO manufacturers (name) VALUES ('Bunn-O-Matic Corporation')")
        conn.execute("INSERT INTO machines (manufacturer_id, model_name) VALUES (1, 'Axiom')")
        user_id = conn.execute("SELECT id FROM users WHERE email = 'tech-p004b@example.com'").fetchone()["id"]
        conv_cur = conn.execute(
            "INSERT INTO conversations (user_id, machine_id) VALUES (%s, 1) RETURNING id", (user_id,)
        )
        conv_id = conv_cur.fetchone()["id"]
        user_msg_cur = conn.execute(
            "INSERT INTO messages (conversation_id, role, content) VALUES (%s, 'user', 'brew temp?') "
            "RETURNING id",
            (conv_id,),
        )
        user_message_id = user_msg_cur.fetchone()["id"]

    with get_conn() as conn:
        attempt_a = routes_chat._claim_conversation_processing(conn, conv_id)
    assert attempt_a is not None

    # A's lease expires while its provider call is still running (slow, not
    # dead) -- B reclaims the same conversation with a fresh attempt_id.
    with get_conn() as conn:
        conn.execute(
            "UPDATE conversations SET processing_claimed_at = now() - interval '10 minutes' WHERE id = %s",
            (conv_id,),
        )
        attempt_b = routes_chat._claim_conversation_processing(conn, conv_id)
    assert attempt_b is not None
    assert attempt_b != attempt_a

    class _FixedAnswerProvider(AIProvider):
        name = "test_fixed"

        def __init__(self, text):
            self.text = text

        def generate(self, question, machine_label, passages, history=None):
            return GeneratedAnswer(answer=self.text, citations=[], provider=self.name, is_no_answer=True)

    monkeypatch.setattr(routes_chat, "hybrid_search", lambda *a, **k: [])

    # B's attempt actually completes and writes the real answer.
    monkeypatch.setattr(routes_chat, "get_provider", lambda: _FixedAnswerProvider("B's real answer"))
    routes_chat._generate_and_persist_answer(
        conv_id, user_message_id, "brew temp?", 1, [], user_id=user_id, attempt_id=attempt_b,
    )

    # A's slow provider call finally returns AFTER B already won.
    monkeypatch.setattr(routes_chat, "get_provider", lambda: _FixedAnswerProvider("A's stale answer"))
    routes_chat._generate_and_persist_answer(
        conv_id, user_message_id, "brew temp?", 1, [], user_id=user_id, attempt_id=attempt_a,
    )

    with get_conn() as conn:
        rows = conn.execute(
            "SELECT content FROM messages WHERE conversation_id = %s AND role = 'assistant' ORDER BY id",
            (conv_id,),
        ).fetchall()
    contents = [r["content"] for r in rows]
    assert contents == ["B's real answer"], (
        f"a zombie attempt's late write must never overwrite or duplicate the reclaiming attempt's "
        f"real answer -- got {contents}"
    )


def test_p0_10_test_fixture_refuses_a_database_url_identical_to_production(tmp_path, monkeypatch):
    """P0-10: the test_env fixture runs migrations and an unconditional
    TRUNCATE ... CASCADE against whatever DATABASE_URL/DATABASE_URL_UNPOOLED
    it finds in backend/.env.test -- pointing that file at the real
    production connection string, even by accident (a copy-pasted .env, a
    misconfigured secret), would destroy live data. _refuse_if_production_database
    compares each test DB URL against backend/.env's own value and raises
    before any migration or TRUNCATE runs if they're identical; this test
    exercises that comparison directly, against temporary fake .env files,
    without touching the real backend/.env or running any actual query."""
    import tests.conftest as conftest_module

    prod_env = tmp_path / "prod.env"
    prod_env.write_text("DATABASE_URL=postgresql://prod-host/prod_db\n")
    monkeypatch.setattr(conftest_module, "PROD_ENV_FILE", prod_env)

    # Identical to the "production" value -- must raise, and must name which
    # variable collided.
    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        conftest_module._refuse_if_production_database(
            "postgresql://prod-host/prod_db", "DATABASE_URL"
        )

    # A genuinely different test database -- must not raise.
    conftest_module._refuse_if_production_database(
        "postgresql://test-host/test_db", "DATABASE_URL"
    )

    # No backend/.env at all (every CI run) -- nothing to compare against,
    # must not raise regardless of the test URL's value.
    monkeypatch.setattr(conftest_module, "PROD_ENV_FILE", tmp_path / "does-not-exist.env")
    conftest_module._refuse_if_production_database(
        "postgresql://prod-host/prod_db", "DATABASE_URL"
    )


def test_p0_11_eval_script_refuses_without_a_disposable_clone(tmp_path):
    """P0-11: the eval script used to crash immediately with AttributeError
    (Settings.db_path_resolved no longer exists post-Postgres-migration), and
    even patched, it could touch the live configured PostgreSQL database
    directly. Rewritten to require EVAL_DATABASE_URL/EVAL_DATABASE_URL_UNPOOLED
    pointing at a disposable clone, refusing hard (before any migration or
    query) if either is missing or identical to the real production value.
    Invoked as a real subprocess (not imported) since the script's guard
    runs as a module-level side effect at import time -- these three cases
    never need a real database, since the guard raises before any connection
    is attempted."""
    import subprocess
    import sys as _sys

    script = Path(__file__).resolve().parent.parent.parent / "scripts" / "eval_retrieval.py"
    fake_prod_env = tmp_path / "fake_prod.env"
    fake_prod_env.write_text("DATABASE_URL=postgresql://prod-host/prod_db\n")

    base_env = {**os.environ, "TMA_EVAL_PROD_ENV_FILE_FOR_TESTS": str(fake_prod_env)}

    # Missing entirely.
    result = subprocess.run(
        [_sys.executable, str(script)], env={k: v for k, v in base_env.items() if k != "EVAL_DATABASE_URL"},
        capture_output=True, text=True, timeout=30, cwd=script.parent.parent,
    )
    assert result.returncode != 0
    assert "must both be set" in result.stderr

    # Set, but identical to the fake "production" value.
    result = subprocess.run(
        [_sys.executable, str(script)],
        env={**base_env, "EVAL_DATABASE_URL": "postgresql://prod-host/prod_db",
             "EVAL_DATABASE_URL_UNPOOLED": "postgresql://prod-host/prod_db"},
        capture_output=True, text=True, timeout=30, cwd=script.parent.parent,
    )
    assert result.returncode != 0
    assert "IDENTICAL to backend/.env's production" in result.stderr

    # A genuinely different (if unreachable) URL -- must pass the guard and
    # fail later, on an actual connection attempt, not on the guard itself.
    result = subprocess.run(
        [_sys.executable, str(script)],
        env={**base_env, "EVAL_DATABASE_URL": "postgresql://fake-eval-clone-host/evaldb",
             "EVAL_DATABASE_URL_UNPOOLED": "postgresql://fake-eval-clone-host/evaldb"},
        capture_output=True, text=True, timeout=30, cwd=script.parent.parent,
    )
    assert result.returncode != 0
    assert "must both be set" not in result.stderr
    assert "IDENTICAL to backend/.env's production" not in result.stderr


def test_p1_01_invitation_link_resolves_to_a_real_redemption_page(test_env):
    """P1-01: admin.js generated invitation links pointing at "/?invite=..."
    -- leftover from a removed technician PWA that used to handle that query
    param client-side. There was no route at "/" at all (reproduced by the
    review: GET /?invite=synthetic returned 404), so an admin could create a
    token and the JSON API could redeem it, but a recipient had no supported
    way to actually do that. Fixed with a real /invite route serving a
    minimal HTML redemption page that calls POST /api/auth/register
    directly (already covered end-to-end by test_admin.py's invitation
    tests) -- this proves the route itself exists and that admin.js was
    updated to link there instead of the dead "/?invite=" path."""
    resp = client.get("/invite")
    assert resp.status_code == 200, f"GET /invite must serve the redemption page, not 404 -- got {resp.status_code}"
    assert "text/html" in resp.headers["content-type"]

    resp_with_params = client.get("/invite", params={"token": "abc123", "email": "tech@example.com"})
    assert resp_with_params.status_code == 200

    admin_js = Path(__file__).resolve().parent.parent.parent / "app" / "web" / "static" / "js" / "admin.js"
    source = admin_js.read_text(encoding="utf-8")
    assert "/invite?token=" in source, "admin.js must link invitations to the real /invite route"
    assert "/?invite=$" not in source, "admin.js must not still generate the dead /?invite= link"


def _seed_p1_02_answerable_machine():
    """Mirrors test_auth_and_chat.py's _seed_answerable_machine: a real chunk
    plus a code-token question (app/providers/extractive.py's
    _code_token_rescue) gets a genuine, citation-bearing completed answer
    with no embeddings needed."""
    with get_conn() as conn:
        conn.execute("INSERT INTO manufacturers (name) VALUES ('Bunn-O-Matic Corporation')")
        conn.execute("INSERT INTO machines (manufacturer_id, model_name) VALUES (1, 'Axiom')")
        cur = conn.execute(
            "INSERT INTO documents (original_filename, storage_path, source_system, source_ref, "
            "file_type, sha256, byte_size, status, review_status) VALUES "
            "('axiom.pdf', 'axiom.pdf', 'local_directory', 'axiom.pdf', 'pdf', 'hash1', 100, "
            "'indexed', 'approved') RETURNING id"
        )
        doc_id = cur.fetchone()["id"]
        conn.execute(
            "INSERT INTO document_machines (document_id, machine_id, review_status) VALUES (%s, 1, 'approved')",
            (doc_id,),
        )
        conn.execute(
            "INSERT INTO chunks (document_id, page_number, chunk_type, content, char_count, ordinal) "
            "VALUES (%s, 4, 'text', "
            "'ERROR CODE E9: indicates a tank heater fault. Check the thermistor circuit.', 90, 0)",
            (doc_id,),
        )


def test_p1_02_admin_feedback_listing_includes_machine_label_message_id_and_citations(test_env):
    """P1-02: GET /api/admin/feedback used to return only
    rating/comment/user/conversation_id -- an admin triaging an "incorrect"
    report had no machine, model, or citation context and no way to jump to
    the specific answer (only a conversation_id) without separately opening
    the conversation. Now also reports message_id, the machine the answer
    was generated for, and every citation the answer actually used."""
    _seed_p1_02_answerable_machine()
    register_test_user(client, "tech-p102@example.com", admin_email="admin-p102@example.com")
    conv = client.post("/api/conversations", json={"machine_id": 1}).json()
    msg = client.post(
        f"/api/conversations/{conv['id']}/messages", json={"content": "what does error E9 mean"}
    ).json()
    feedback_resp = client.post(
        f"/api/messages/{msg['id']}/feedback", json={"rating": "incorrect", "comment": "wrong fault cause"}
    )
    assert feedback_resp.status_code == 201, feedback_resp.text

    register_test_user(client, "admin-p102@example.com", role="administrator", admin_email="admin-p102@example.com")
    resp = client.get("/api/admin/feedback")
    assert resp.status_code == 200, resp.text
    row = next(r for r in resp.json() if r["message_id"] == msg["id"])

    assert row["rating"] == "incorrect"
    assert row["comment"] == "wrong fault cause"
    assert row["machine_label"] == "Bunn-O-Matic Corporation Axiom", (
        f"admin feedback listing must report which machine the answer was generated for, got {row}"
    )
    assert "axiom.pdf" in row["citations"], (
        f"admin feedback listing must report the citations the answer actually used, got {row}"
    )


def _admin_js_source() -> str:
    return (Path(__file__).resolve().parent.parent.parent / "app" / "web" / "static" / "js" / "admin.js").read_text(
        encoding="utf-8"
    )


def test_p1_06_admin_js_has_no_inline_style_or_event_handler_attributes(test_env):
    """P1-06: app/main.py's CSP is style-src 'self'/script-src 'self' with no
    unsafe-inline (P1-18), but admin.js built up markup with dozens of
    inline style="..." attributes and one onclick="..." handler -- a
    CSP-enforcing browser silently drops every one of them. Worst case: the
    edit-row/report-row toggle rows relied on an inline style="display:none"
    ever having applied at all, so under real CSP enforcement they rendered
    visible on first load instead of hidden until toggled. Fixed by moving
    every declaration into admin.css classes (toggled via classList, not
    .style.display/.style.cssText) and wiring the one handler via
    addEventListener. This is a static source check, matching the review's
    own verification depth for this finding ("Status: OPEN; static")."""
    source = _admin_js_source()
    assert 'style="' not in source, "admin.js must not build any inline style=\"...\" attribute"
    assert "onclick=" not in source, "admin.js must not build any inline onclick=\"...\" attribute"

    # The two rows that actually depend on starting hidden must use the CSS
    # class (toggled via classList), not a runtime .style.display check --
    # proves the fix isn't just cosmetic (removing the attribute) but that
    # the show/hide logic was actually ported to classList too.
    assert '"edit-row hidden"' in source
    assert '"report-row hidden"' in source
    assert "row.style.display" not in source
    assert 'classList.toggle("hidden")' in source or 'classList.add("hidden")' in source

    admin_css = (
        Path(__file__).resolve().parent.parent.parent / "app" / "web" / "static" / "css" / "admin.css"
    ).read_text(encoding="utf-8")
    assert ".hidden" in admin_css and "display: none" in admin_css


def test_p1_07_admin_js_attribute_interpolation_uses_a_quote_safe_encoder(test_env):
    """P1-07: esc() escapes text-node content (&, <, >) but not quote
    characters -- a text node never needs them escaped, but an HTML
    ATTRIBUTE value does. esc()'s result was interpolated directly inside
    value="..." and data-search="..." for admin/PDF-derived data (document
    title, revision, manufacturer, machine family) -- a value containing a
    double quote truncates the attribute and lets the rest of the string
    inject new attributes onto that element. Fixed with escAttr() (esc() ->
    &quot;/&#39; on top), used at every such site. Static source check,
    matching the review's own verification depth for this finding."""
    source = _admin_js_source()
    assert "function escAttr(" in source, "admin.js must define a quote-safe attribute encoder"

    # The vulnerable OLD pattern (esc()'s result placed directly inside a
    # quoted attribute) must be gone from every site the review named --
    # and escAttr must be the thing that replaced it, not just some other
    # attribute going untouched.
    assert 'value="${esc(' not in source, "esc() (not quote-safe) must not feed a value=\"...\" attribute"
    assert 'data-search="${esc(' not in source, "esc() (not quote-safe) must not feed a data-search=\"...\" attribute"
    assert source.count("escAttr(") >= 5, (
        "expected escAttr() at every attribute-interpolation site the review named "
        "(invite link, manufacturer/title/revision values, machine data-search)"
    )

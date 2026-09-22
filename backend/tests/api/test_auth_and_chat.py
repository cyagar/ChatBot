import threading

import pytest
from fastapi.testclient import TestClient

from app.db import get_conn
from app.main import app
from tests.conftest import register_test_user

client = TestClient(app)


def _register(email="tech1@example.com", password="password123"):
    return register_test_user(client, email, role="technician", password=password)


_ANSWERABLE_QUESTION = "what does error E9 mean"


def _seed_answerable_machine():
    """P1-11 (independent follow-up review, applied 2026-09-14): feedback/save
    are now restricted to a completed, substantive assistant answer -- an
    unanswerable question (no machine/chunks seeded) produces a no-answer or
    clarifying message, which is no longer a valid feedback/save target. This
    seeds a real chunk plus relies on a code-token question (see
    app/providers/extractive.py's _code_token_rescue -- no embeddings are
    seeded here, so the vector-similarity gate alone would otherwise reject
    every answer) so a conversation on machine_id=1 gets a genuine, eligible
    completed answer."""
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


def test_registration_without_invite_is_rejected(test_env):
    """Independent follow-up review P0-5: public self-registration used to
    always succeed (the first registrant even became administrator). Now a
    request with no invite_token at all must fail validation, and critically
    must not create a user row -- a 4xx alone doesn't prove that."""
    resp = client.post("/api/auth/register", json={"email": "uninvited@example.com", "password": "password123"})
    assert resp.status_code == 422

    with get_conn() as conn:
        row = conn.execute("SELECT id FROM users WHERE email = %s", ("uninvited@example.com",)).fetchone()
    assert row is None


def test_registration_with_bogus_invite_token_is_rejected(test_env):
    resp = client.post(
        "/api/auth/register",
        json={"email": "nope@example.com", "password": "password123", "invite_token": "not-a-real-token"},
    )
    assert resp.status_code == 403
    with get_conn() as conn:
        row = conn.execute("SELECT id FROM users WHERE email = %s", ("nope@example.com",)).fetchone()
    assert row is None


def test_invite_is_bound_to_its_email_and_single_use(test_env):
    register_test_user(client, "bootstrap-admin@example.com", role="administrator")
    invite = client.post(
        "/api/admin/invitations", json={"email": "invited@example.com", "role": "technician"}
    ).json()
    token = invite["token"]
    client.post("/api/auth/logout")

    # Wrong email for this token.
    wrong_email = client.post(
        "/api/auth/register",
        json={"email": "someone-else@example.com", "password": "password123", "invite_token": token},
    )
    assert wrong_email.status_code == 403

    # Correct email consumes it.
    ok = client.post(
        "/api/auth/register",
        json={"email": "invited@example.com", "password": "password123", "invite_token": token},
    )
    assert ok.status_code == 201
    assert ok.json()["role"] == "technician"
    client.post("/api/auth/logout")

    # Same token again, even with the right email, must fail -- single-use.
    reuse = client.post(
        "/api/auth/register",
        json={"email": "invited@example.com", "password": "password123", "invite_token": token},
    )
    assert reuse.status_code == 403


def test_p1_20_registering_with_mixed_case_email_can_log_in_with_any_casing(test_env):
    """P1-20 (external review, 2026-09-21): users.email is TEXT with
    case-sensitive uniqueness, and registration stored whatever case was
    typed -- a technician who registered as "Tech.User@Example.com" could
    not log in with "tech.user@example.com" (or any other casing), since
    login's SELECT ... WHERE email = %s compared exactly. Fixed by
    normalizing every write/read through normalize_email()."""
    register_test_user(client, "bootstrap-admin@example.com", role="administrator")
    invite = client.post(
        "/api/admin/invitations", json={"email": "Mixed.Case@Example.com", "role": "technician"}
    ).json()
    client.post("/api/auth/logout")

    reg = client.post(
        "/api/auth/register",
        json={"email": "Mixed.Case@Example.com", "password": "password123", "invite_token": invite["token"]},
    )
    assert reg.status_code == 201
    assert reg.json()["email"] == "mixed.case@example.com", "the stored/returned email must be normalized to lowercase"
    client.post("/api/auth/logout")

    login_different_case = client.post(
        "/api/auth/login", json={"email": "MIXED.CASE@EXAMPLE.COM", "password": "password123"}
    )
    assert login_different_case.status_code == 200, (
        f"a technician who registered with mixed case must be able to log in with any casing, "
        f"got {login_different_case.status_code}: {login_different_case.text}"
    )


def test_p1_20_invitation_for_an_email_differing_only_in_case_from_an_existing_account_is_rejected(test_env):
    """Companion to the test above: without normalizing the existing-account
    check too, "Tech@Example.com" and "tech@example.com" could become two
    separate accounts sharing what a human considers the same address."""
    register_test_user(client, "bootstrap-admin2@example.com", role="administrator", admin_email="bootstrap-admin2@example.com")
    invite = client.post(
        "/api/admin/invitations", json={"email": "existing.tech@example.com", "role": "technician"}
    ).json()
    client.post("/api/auth/logout")
    client.post(
        "/api/auth/register",
        json={"email": "existing.tech@example.com", "password": "password123", "invite_token": invite["token"]},
    )
    client.post("/api/auth/logout")

    register_test_user(client, "bootstrap-admin2@example.com", role="administrator", admin_email="bootstrap-admin2@example.com")
    resp = client.post(
        "/api/admin/invitations", json={"email": "Existing.Tech@Example.com", "role": "technician"}
    )
    assert resp.status_code == 409, (
        f"inviting a case-variant of an already-registered email must be rejected, got {resp.status_code}"
    )


def test_p1_20_bootstrap_admin_stores_a_lowercase_email(test_env):
    from app.auth.bootstrap import bootstrap_admin

    bootstrap_admin("Admin.User@Example.com", "password123")
    with get_conn() as conn:
        row = conn.execute("SELECT email FROM users").fetchone()
    assert row["email"] == "admin.user@example.com"

    login = client.post("/api/auth/login", json={"email": "admin.user@example.com", "password": "password123"})
    assert login.status_code == 200


def test_p1_20_database_level_backstop_rejects_a_case_variant_duplicate_email(test_env):
    """Defense-in-depth (matches test_duplicate_email_registration_rejected's
    reasoning): normalize_email() is the primary enforcement, applied at
    every write in Python -- migrations/0004_lowercase_emails.sql's
    unique(lower(email)) index is the database-level backstop for a future
    write that bypasses it (a script, a bug). Proven directly at the SQL
    level, independent of any application code path."""
    import psycopg
    import pytest

    with get_conn() as conn:
        conn.execute(
            "INSERT INTO users (email, password_hash, role) VALUES (%s, 'x', 'technician')",
            ("dupe.check@example.com",),
        )

    # A separate connection/transaction -- Postgres aborts the whole
    # transaction on a constraint violation until rollback, so the raise
    # must propagate out through get_conn()'s own except/rollback, not be
    # swallowed by pytest.raises while still inside the `with` block (which
    # would leave get_conn() trying to commit an already-aborted transaction).
    with pytest.raises(psycopg.errors.UniqueViolation):
        with get_conn() as conn:
            conn.execute(
                "INSERT INTO users (email, password_hash, role) VALUES (%s, 'x', 'technician')",
                ("Dupe.Check@Example.com",),
            )


def test_bootstrap_admin_refuses_once_a_user_exists(test_env):
    from app.auth.bootstrap import bootstrap_admin

    bootstrap_admin("first-admin@example.com", "password123")
    with pytest.raises(RuntimeError, match="Refusing to bootstrap"):
        bootstrap_admin("second-admin@example.com", "password123")


def test_duplicate_email_registration_rejected(test_env):
    """Defense-in-depth check at the register endpoint itself, independent of
    invite creation already refusing to issue an invite for an email that has
    an account (covered by test_invite_creation_rejects_existing_email)."""
    register_test_user(client, "bootstrap-admin@example.com", role="administrator")
    invite = client.post(
        "/api/admin/invitations", json={"email": "raceduplicate@example.com", "role": "technician"}
    ).json()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO users (email, password_hash, role) VALUES (%s, 'x', 'technician')",
            ("raceduplicate@example.com",),
        )
    client.post("/api/auth/logout")
    resp = client.post(
        "/api/auth/register",
        json={"email": "raceduplicate@example.com", "password": "password123", "invite_token": invite["token"]},
    )
    assert resp.status_code == 409


def test_disabled_user_cannot_log_in_or_use_an_existing_session(test_env):
    register_test_user(client, "bootstrap-admin@example.com", role="administrator")
    reg = register_test_user(client, "todisable@example.com")
    user_id = reg.json()["id"]

    # A second client holds the disabled-to-be user's own session cookie,
    # captured before the admin disables them, so we can prove an ALREADY
    # ISSUED token stops working -- not just that a fresh login is blocked
    # (independent follow-up review P0-5: "session revocation").
    tech_client = TestClient(app)
    tech_client.cookies.set("tma_session", client.cookies.get("tma_session"))
    assert tech_client.get("/api/auth/me").status_code == 200

    register_test_user(client, "bootstrap-admin@example.com", role="administrator")
    disable_resp = client.post(f"/api/admin/users/{user_id}/disable")
    assert disable_resp.status_code == 200

    assert tech_client.get("/api/auth/me").status_code == 401, (
        "a session token issued before disable must stop working immediately, not just at its natural expiry"
    )

    login_resp = client.post("/api/auth/login", json={"email": "todisable@example.com", "password": "password123"})
    assert login_resp.status_code == 401


def test_login_wrong_password_rejected(test_env):
    _register("user@example.com", "correct-password")
    resp = client.post("/api/auth/login", json={"email": "user@example.com", "password": "wrong-password"})
    assert resp.status_code == 401


def test_unauthenticated_request_rejected(test_env):
    fresh_client = TestClient(app)  # no session cookie
    resp = fresh_client.post("/api/conversations", json={"machine_id": None})
    assert resp.status_code == 401


def test_conversation_without_machine_asks_clarifying_question(test_env):
    _register("tech3@example.com")
    conv = client.post("/api/conversations", json={"machine_id": None}).json()
    resp = client.post(f"/api/conversations/{conv['id']}/messages", json={"content": "Why won't it heat up?"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["is_clarifying_question"] is True
    assert body["is_no_answer"] is False


def test_conversation_and_message_timestamps_carry_an_explicit_utc_offset(test_env):
    """Phase 1 (narrowed scope): "Return UTC ISO-8601 timestamps with
    offsets." SQLite's datetime('now') (what conversations.started_at/
    updated_at and messages.created_at are actually stored as) returns
    'YYYY-MM-DD HH:MM:SS' with no timezone marker at all -- not valid
    ISO-8601. This proves the real HTTP response, not just app.api.common.
    iso_utc() in isolation (see tests/unit/test_iso_utc.py for that)."""
    _register("tech-ts@example.com")
    conv = client.post("/api/conversations", json={"machine_id": None}).json()
    assert conv["started_at"].endswith("+00:00")
    assert conv["updated_at"].endswith("+00:00")
    assert "T" in conv["started_at"]  # not the bare 'YYYY-MM-DD HH:MM:SS' form

    msg = client.post(
        f"/api/conversations/{conv['id']}/messages", json={"content": "Why won't it heat up?"}
    ).json()
    assert msg["created_at"].endswith("+00:00")
    assert "T" in msg["created_at"]

    listed = client.get("/api/conversations").json()[0]
    assert listed["started_at"].endswith("+00:00")
    assert listed["updated_at"].endswith("+00:00")


def test_question_on_machine_with_no_manuals_is_honest_no_answer(test_env):
    with get_conn() as conn:
        # RESTART IDENTITY (tests/conftest.py's test_env fixture) guarantees
        # these come out as id=1 without needing to force an explicit value
        # into the GENERATED ALWAYS AS IDENTITY column.
        conn.execute("INSERT INTO manufacturers (name) VALUES ('Bunn-O-Matic Corporation')")
        conn.execute(
            "INSERT INTO machines (manufacturer_id, model_name) VALUES (1, 'Axiom')"
        )

    _register("tech4@example.com")
    conv = client.post("/api/conversations", json={"machine_id": 1}).json()
    resp = client.post(f"/api/conversations/{conv['id']}/messages", json={"content": "What does error E4 mean?"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["is_no_answer"] is True
    assert "verify" in body["content"].lower() or "not" in body["content"].lower()


def test_empty_question_rejected(test_env):
    _register("tech5@example.com")
    conv = client.post("/api/conversations", json={"machine_id": None}).json()
    resp = client.post(f"/api/conversations/{conv['id']}/messages", json={"content": "   "})
    assert resp.status_code == 422


def test_cannot_access_another_users_conversation(test_env):
    _register("owner@example.com", "password123")
    conv = client.post("/api/conversations", json={"machine_id": None}).json()
    client.post("/api/auth/logout")

    _register("intruder@example.com", "password123")
    resp = client.get(f"/api/conversations/{conv['id']}/messages")
    assert resp.status_code == 404


def test_admin_endpoint_forbidden_for_technician(test_env):
    _register("plaintech@example.com")
    resp = client.get("/api/admin/documents")
    assert resp.status_code == 403


def test_admin_endpoint_allowed_for_administrator(test_env):
    register_test_user(client, "bootstrap-admin@example.com", role="administrator")
    resp = client.get("/api/admin/documents")
    assert resp.status_code == 200


def test_feedback_rejects_invalid_rating(test_env):
    _register("tech6@example.com")
    conv = client.post("/api/conversations", json={"machine_id": None}).json()
    msg = client.post(f"/api/conversations/{conv['id']}/messages", json={"content": "test"}).json()
    resp = client.post(f"/api/messages/{msg['id']}/feedback", json={"rating": "not_a_real_rating"})
    assert resp.status_code == 422


def test_concurrent_feedback_submission_does_not_crash_or_corrupt(test_env):
    """P1-6 (2026-08-24 independent follow-up review, "concurrent chat,
    feedback, ... tests"): unlike retry/machine-confirmation/invitation,
    feedback has no idempotency mechanism and none was added here -- there
    is no expensive or duplicative side effect a double-tap could trigger
    (no provider call, no second conversation turn), so multiple feedback
    rows per message are allowed by design (a technician can submit
    "helpful" and later reconsider "incorrect"; the schema has no
    UNIQUE(message_id, user_id)). This is a characterization test proving
    concurrent submission is merely safe -- no crash, no lost/merged row --
    not a test of deduplication, which was never the ask here."""
    _seed_answerable_machine()
    _register("feedbackracer@example.com")
    conv = client.post("/api/conversations", json={"machine_id": 1}).json()
    msg = client.post(f"/api/conversations/{conv['id']}/messages", json={"content": _ANSWERABLE_QUESTION}).json()

    responses = []

    def submit(rating):
        responses.append(client.post(f"/api/messages/{msg['id']}/feedback", json={"rating": rating}))

    threads = [
        threading.Thread(target=submit, args=("helpful",)),
        threading.Thread(target=submit, args=("incorrect",)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert all(r.status_code == 201 for r in responses), [r.status_code for r in responses]

    with get_conn() as conn:
        rows = conn.execute("SELECT rating FROM feedback WHERE message_id = %s", (msg["id"],)).fetchall()
    assert sorted(r["rating"] for r in rows) == ["helpful", "incorrect"]


def test_get_messages_reports_the_current_users_feedback_and_saved_state(test_env):
    """Found via live tablet testing (2026-08-25): a client that reloads a
    conversation (app restart, rotation recreating a ViewModel, navigating
    away and back) had no way to know a message was already rated/saved, so
    the buttons reset to unmarked and a re-tap silently duplicated the row.
    MessageOut.feedback_rating/is_saved is how a client rehydrates that
    state instead of re-deriving it -- this pins the contract."""
    _seed_answerable_machine()
    _register("tech10@example.com")
    conv = client.post("/api/conversations", json={"machine_id": 1}).json()
    msg = client.post(f"/api/conversations/{conv['id']}/messages", json={"content": _ANSWERABLE_QUESTION}).json()

    fresh = next(m for m in client.get(f"/api/conversations/{conv['id']}/messages").json() if m["id"] == msg["id"])
    assert fresh["feedback_rating"] is None
    assert fresh["is_saved"] is False

    client.post(f"/api/messages/{msg['id']}/feedback", json={"rating": "helpful"})
    client.post(f"/api/messages/{msg['id']}/save")

    updated = next(m for m in client.get(f"/api/conversations/{conv['id']}/messages").json() if m["id"] == msg["id"])
    assert updated["feedback_rating"] == "helpful"
    assert updated["is_saved"] is True


def test_get_messages_reports_the_most_recent_feedback_rating(test_env):
    """Feedback rows are intentionally not deduplicated -- a technician
    reconsidering (helpful, then later incorrect) is an allowed, real case
    (see test_concurrent_feedback_submission_does_not_crash_or_corrupt).
    feedback_rating must report the latest judgment, not the first."""
    _seed_answerable_machine()
    _register("tech11@example.com")
    conv = client.post("/api/conversations", json={"machine_id": 1}).json()
    msg = client.post(f"/api/conversations/{conv['id']}/messages", json={"content": _ANSWERABLE_QUESTION}).json()

    client.post(f"/api/messages/{msg['id']}/feedback", json={"rating": "helpful"})
    client.post(f"/api/messages/{msg['id']}/feedback", json={"rating": "incorrect"})

    updated = next(m for m in client.get(f"/api/conversations/{conv['id']}/messages").json() if m["id"] == msg["id"])
    assert updated["feedback_rating"] == "incorrect"


def test_save_answer_twice_is_idempotent(test_env):
    """The bug this session found live: a client with stale/unknown saved
    state re-tapping Save inserted a second saved_answers row for the same
    (user_id, message_id). Unlike feedback, a duplicate save carries no new
    information, so this is enforced as a real UNIQUE constraint + INSERT OR
    IGNORE (migration 0011), not an append-only log."""
    _seed_answerable_machine()
    _register("tech12@example.com")
    conv = client.post("/api/conversations", json={"machine_id": 1}).json()
    msg = client.post(f"/api/conversations/{conv['id']}/messages", json={"content": _ANSWERABLE_QUESTION}).json()

    first = client.post(f"/api/messages/{msg['id']}/save")
    second = client.post(f"/api/messages/{msg['id']}/save")
    assert first.status_code == 201
    assert second.status_code == 201

    with get_conn() as conn:
        rows = conn.execute("SELECT id FROM saved_answers WHERE message_id = %s", (msg["id"],)).fetchall()
    assert len(rows) == 1


def test_duplicate_idempotency_key_returns_the_original_reply_not_a_new_turn(test_env):
    """Android Rewrite Plan sec 9/16/17: a client retry after an ambiguous
    dropped connection must return the original attempt's result, never
    insert a second user turn or trigger a second provider call."""
    _register("idempotent1@example.com")
    conv = client.post("/api/conversations", json={"machine_id": None}).json()
    key = "client-generated-uuid-1"

    first = client.post(
        f"/api/conversations/{conv['id']}/messages",
        json={"content": "Why won't it heat up?"},
        headers={"Idempotency-Key": key},
    )
    second = client.post(
        f"/api/conversations/{conv['id']}/messages",
        json={"content": "Why won't it heat up?"},
        headers={"Idempotency-Key": key},
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["id"] == second.json()["id"]

    with get_conn() as conn:
        user_rows = conn.execute(
            "SELECT id FROM messages WHERE conversation_id = %s AND role = 'user'", (conv["id"],)
        ).fetchall()
    assert len(user_rows) == 1


def test_different_idempotency_keys_create_separate_turns(test_env):
    """Sanity guard against over-aggressive dedup: two distinct keys (two
    genuinely different questions) must not collapse into one turn."""
    _register("idempotent2@example.com")
    conv = client.post("/api/conversations", json={"machine_id": None}).json()

    first = client.post(
        f"/api/conversations/{conv['id']}/messages",
        json={"content": "Why won't it heat up?"},
        headers={"Idempotency-Key": "key-a"},
    )
    second = client.post(
        f"/api/conversations/{conv['id']}/messages",
        json={"content": "Why is it leaking?"},
        headers={"Idempotency-Key": "key-b"},
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["id"] != second.json()["id"]

    with get_conn() as conn:
        user_rows = conn.execute(
            "SELECT id FROM messages WHERE conversation_id = %s AND role = 'user'", (conv["id"],)
        ).fetchall()
    assert len(user_rows) == 2


def test_missing_idempotency_key_behaves_exactly_as_before(test_env):
    """Backward compatibility: existing callers (the PWA JS) that never send
    the header must be completely unaffected -- every question is a new turn,
    same as pre-idempotency behavior."""
    _register("idempotent3@example.com")
    conv = client.post("/api/conversations", json={"machine_id": None}).json()

    first = client.post(f"/api/conversations/{conv['id']}/messages", json={"content": "test one"})
    second = client.post(f"/api/conversations/{conv['id']}/messages", json={"content": "test two"})

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["id"] != second.json()["id"]

    with get_conn() as conn:
        user_rows = conn.execute(
            "SELECT id FROM messages WHERE conversation_id = %s AND role = 'user'", (conv["id"],)
        ).fetchall()
    assert len(user_rows) == 2


def test_concurrent_duplicate_idempotency_key_never_creates_two_user_turns(test_env):
    """The pre-check (SELECT then INSERT) is a fast path, not the safety
    mechanism -- this proves the UNIQUE index actually holds under a genuine
    race, not just under sequential duplicate requests (see
    test_duplicate_idempotency_key_returns_the_original_reply_not_a_new_turn
    above, which doesn't exercise concurrent timing at all)."""
    _register("idempotentracer@example.com")
    conv = client.post("/api/conversations", json={"machine_id": None}).json()
    key = "racing-key"

    responses = []
    barrier = threading.Barrier(2)

    def submit():
        barrier.wait(timeout=5)
        responses.append(
            client.post(
                f"/api/conversations/{conv['id']}/messages",
                json={"content": "Why won't it heat up?"},
                headers={"Idempotency-Key": key},
            )
        )

    threads = [threading.Thread(target=submit) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Either both requests see the same completed reply (200/200, same id),
    # or the loser arrives before the winner's reply is persisted and gets a
    # 409 ("already being processed") instead -- both are correct outcomes.
    # What must never happen is a second user turn or two different
    # assistant replies.
    statuses = sorted(r.status_code for r in responses)
    assert statuses in ([200, 200], [200, 409]), statuses
    ok_ids = {r.json()["id"] for r in responses if r.status_code == 200}
    assert len(ok_ids) == 1

    with get_conn() as conn:
        user_rows = conn.execute(
            "SELECT id FROM messages WHERE conversation_id = %s AND role = 'user'", (conv["id"],)
        ).fetchall()
    assert len(user_rows) == 1


def test_save_and_list_saved_answer_roundtrip(test_env):
    _seed_answerable_machine()
    _register("tech7@example.com")
    conv = client.post("/api/conversations", json={"machine_id": 1}).json()
    msg = client.post(f"/api/conversations/{conv['id']}/messages", json={"content": _ANSWERABLE_QUESTION}).json()

    save_resp = client.post(f"/api/messages/{msg['id']}/save")
    assert save_resp.status_code == 201

    list_resp = client.get("/api/saved-answers")
    assert list_resp.status_code == 200
    saved = list_resp.json()
    entry = next((s for s in saved if s["answer"]["id"] == msg["id"]), None)
    assert entry is not None
    # P1-3: a saved answer must carry enough context to resume from -- which
    # conversation it belongs to and the question that produced it, not just
    # the bare answer text.
    assert entry["conversation_id"] == conv["id"]
    assert entry["question"] == _ANSWERABLE_QUESTION


def test_list_conversations_derives_a_title_from_the_first_user_message(test_env):
    """P1-3: conversations.title is never written anywhere in the codebase --
    without a derived fallback, every row in a history list would render
    blank."""
    _register("tech8@example.com")
    conv = client.post("/api/conversations", json={"machine_id": None}).json()
    long_question = "Why does the brewer keep tripping the breaker " + ("x" * 80)
    client.post(f"/api/conversations/{conv['id']}/messages", json={"content": long_question})

    listed = {c["id"]: c for c in client.get("/api/conversations").json()}
    assert listed[conv["id"]]["title"].startswith("Why does the brewer keep tripping the breaker")
    assert len(listed[conv["id"]]["title"]) <= 80


def test_list_conversations_omits_conversations_with_no_questions_asked(test_env):
    """Reported live on the tablet (2026-08-25): tapping a machine (or "Not
    sure which machine?") creates the conversation row immediately, before
    any question is typed -- backing out without asking anything left a
    blank, useless entry in History. list_conversations must only surface
    conversations where a question was actually sent."""
    _register("tech8b@example.com")
    asked = client.post("/api/conversations", json={"machine_id": None}).json()
    abandoned = client.post("/api/conversations", json={"machine_id": None}).json()
    client.post(f"/api/conversations/{asked['id']}/messages", json={"content": "What does error E4 mean?"})

    listed_ids = {c["id"] for c in client.get("/api/conversations").json()}
    assert asked["id"] in listed_ids
    assert abandoned["id"] not in listed_ids


def test_confirm_machine_endpoint_sets_and_persists_machine(test_env):
    with get_conn() as conn:
        # RESTART IDENTITY (tests/conftest.py's test_env fixture) guarantees
        # these come out as id=1 without needing to force an explicit value
        # into the GENERATED ALWAYS AS IDENTITY column.
        conn.execute("INSERT INTO manufacturers (name) VALUES ('Bunn-O-Matic Corporation')")
        conn.execute("INSERT INTO machines (manufacturer_id, model_name) VALUES (1, 'Axiom')")

    _register("tech8@example.com")
    conv = client.post("/api/conversations", json={"machine_id": None}).json()
    assert conv["machine_id"] is None

    resp = client.post(f"/api/conversations/{conv['id']}/machine", json={"machine_id": 1})
    assert resp.status_code == 200
    body = resp.json()
    assert body["machine_id"] == 1
    assert "Axiom" in body["machine_label"]

    # A question now resolves straight through -- no clarifying question,
    # since the machine was set via the explicit endpoint, not inferred.
    ask = client.post(f"/api/conversations/{conv['id']}/messages", json={"content": "What does error E4 mean?"})
    assert ask.status_code == 200
    assert ask.json()["is_clarifying_question"] is False


def test_confirm_machine_rejects_unknown_machine(test_env):
    _register("tech9@example.com")
    conv = client.post("/api/conversations", json={"machine_id": None}).json()
    resp = client.post(f"/api/conversations/{conv['id']}/machine", json={"machine_id": 999})
    assert resp.status_code == 404


def test_rate_limit_enforced(test_env, monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_PER_MINUTE", "2")
    from app.config import get_settings
    get_settings.cache_clear()

    _register("ratelimited@example.com")
    conv = client.post("/api/conversations", json={"machine_id": None}).json()

    statuses = [
        client.post(f"/api/conversations/{conv['id']}/messages", json={"content": f"question {i}"}).status_code
        for i in range(4)
    ]
    assert 429 in statuses, f"expected a 429 among {statuses} after exceeding the 2/minute limit"

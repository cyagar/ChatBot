"""The live POST /messages response for an ambiguous machine mention includes
the specific candidate machines found (`clarifying_options`) -- this must be
persisted, not just `is_clarifying_question` and the prompt text, or reload
(GET /messages) would reproduce the clarifying bubble's TEXT but not its
tappable candidate buttons, silently downgrading to the frontend's generic
"choose a machine" fallback, which routes through the ordinary picker flow
and starts a brand-new conversation instead of resuming the one with the
pending question, abandoning it rather than answering it.

These tests cover the backend half: migration 0007 adds
messages.clarifying_options, and this file proves the persisted value is
byte-for-byte the same list the live response returned, for both the
multi-candidate and zero-candidate cases.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.db import get_conn
from app.main import app
from tests.conftest import register_test_user

client = TestClient(app)


def _seed_eligible_document(conn, machine_id: int, source_ref: str) -> int:
    """_resolve_machine_mention (routes_chat.py) only matches a machine that
    has at least one approved, indexed, current-revision document with an
    approved link -- the same eligibility rule search_machines/recent_machines
    (routes_machines.py) already enforce. Both review_status columns default
    to 'pending' (migration 0001), so both are set explicitly here rather
    than relying on the admin review endpoints these tests have no admin
    session to call."""
    cur = conn.execute(
        "INSERT INTO documents (original_filename, storage_path, source_system, source_ref, "
        "file_type, sha256, byte_size, status, review_status, manufacturer_id, doc_type, title, "
        "is_current_revision) "
        "VALUES (%s, %s, 'local_directory', %s, 'pdf', %s, 100, 'indexed', 'approved', 1, "
        "'service_repair', 'Manual', true) RETURNING id",
        (f"{source_ref}.pdf", f"{source_ref}.pdf", source_ref, source_ref),
    )
    doc_id = cur.fetchone()["id"]
    conn.execute(
        "INSERT INTO document_machines (document_id, machine_id, review_status) VALUES (%s, %s, 'approved')",
        (doc_id, machine_id),
    )
    return doc_id


def _seed_two_ambiguous_machines(conn):
    """'Axiom' and 'Axiom Pro' both exact-word-match a question mentioning
    'Axiom Pro' -- \\bAxiom\\b matches inside 'Axiom Pro' too -- giving a
    real two-candidate clarification without relying on fuzzy matching.
    Both need an eligible document of their own (see _seed_eligible_document)
    or neither would be a real candidate to begin with."""
    conn.execute("INSERT INTO manufacturers (name) VALUES ('Bunn-O-Matic Corporation')")
    conn.execute("INSERT INTO machines (manufacturer_id, model_name) VALUES (1, 'Axiom')")
    conn.execute("INSERT INTO machines (manufacturer_id, model_name) VALUES (1, 'Axiom Pro')")
    machine_ids = [r["id"] for r in conn.execute("SELECT id FROM machines ORDER BY id").fetchall()]
    _seed_eligible_document(conn, machine_ids[0], "axiom.pdf")
    _seed_eligible_document(conn, machine_ids[1], "axiom-pro.pdf")


def test_clarifying_options_persist_and_survive_reload_with_exact_equality(test_env):
    with get_conn() as conn:
        _seed_two_ambiguous_machines(conn)

    register_test_user(client, "clarify1@example.com", role="technician")
    conv = client.post("/api/conversations", json={"machine_id": None}).json()

    live = client.post(
        f"/api/conversations/{conv['id']}/messages", json={"content": "Is the Axiom Pro leaking?"}
    ).json()

    assert live["is_clarifying_question"] is True
    assert len(live["clarifying_options"]) == 2
    assert {c["id"] for c in live["clarifying_options"]} == {1, 2}

    reloaded = client.get(f"/api/conversations/{conv['id']}/messages").json()
    reloaded_clarify = [m for m in reloaded if m["is_clarifying_question"]]
    assert len(reloaded_clarify) == 1

    # Exact live-versus-reload equality, not just "some candidates showed
    # up" -- same ids, same labels, same order.
    assert reloaded_clarify[0]["clarifying_options"] == live["clarifying_options"]


def test_clarifying_message_with_no_matched_machine_persists_an_empty_list(test_env):
    """The zero-candidate case ('please pick a manufacturer and model') must
    also round-trip consistently -- an empty list live, an empty list on
    reload, not a NULL-vs-[] mismatch that would trip up the frontend."""
    with get_conn() as conn:
        conn.execute("INSERT INTO manufacturers (name) VALUES ('Bunn-O-Matic Corporation')")
        conn.execute("INSERT INTO machines (manufacturer_id, model_name) VALUES (1, 'Axiom')")

    register_test_user(client, "clarify2@example.com", role="technician")
    conv = client.post("/api/conversations", json={"machine_id": None}).json()

    live = client.post(
        f"/api/conversations/{conv['id']}/messages", json={"content": "It won't stop leaking"}
    ).json()

    assert live["is_clarifying_question"] is True
    assert live["clarifying_options"] == []

    reloaded = client.get(f"/api/conversations/{conv['id']}/messages").json()
    reloaded_clarify = [m for m in reloaded if m["is_clarifying_question"]]
    assert len(reloaded_clarify) == 1
    assert reloaded_clarify[0]["clarifying_options"] == []


def test_confirming_a_machine_from_multiple_candidates_still_resumes_the_pending_question(test_env):
    """The specific-candidate resume path (unchanged by this fix, but the
    control this file's persistence guarantee exists to keep working) --
    picking one of the persisted candidates via POST /machine must resume
    the original question, the same guarantee test_multiturn.py already
    covers for the zero-candidate case."""
    with get_conn() as conn:
        _seed_two_ambiguous_machines(conn)

    register_test_user(client, "clarify3@example.com", role="technician")
    conv = client.post("/api/conversations", json={"machine_id": None}).json()

    live = client.post(
        f"/api/conversations/{conv['id']}/messages", json={"content": "Is the Axiom Pro leaking?"}
    ).json()
    chosen_id = live["clarifying_options"][0]["id"]

    resp = client.post(f"/api/conversations/{conv['id']}/machine", json={"machine_id": chosen_id})
    assert resp.status_code == 200
    assert resp.json()["machine_id"] == chosen_id

    messages = client.get(f"/api/conversations/{conv['id']}/messages").json()
    user_messages = [m for m in messages if m["role"] == "user"]
    assert len(user_messages) == 1, "the original question must not be duplicated as a new user turn"


def test_a_name_matched_machine_with_nothing_retrievable_is_not_offered_as_a_candidate(test_env):
    """_resolve_machine_mention must apply the same eligibility rule
    search_machines/recent_machines already enforce --
    otherwise a free-text mention can resolve straight to a machine the
    explicit picker would never even show, dead-ending into "no manuals"
    right after the technician just confirmed which one they meant. 'Axiom'
    here exact-word-matches the question but has no approved/indexed
    document at all, so it must be treated exactly like a machine that was
    never mentioned -- the zero-candidate clarifying path, not a confident
    (and useless) single-candidate resolution."""
    with get_conn() as conn:
        conn.execute("INSERT INTO manufacturers (name) VALUES ('Bunn-O-Matic Corporation')")
        conn.execute("INSERT INTO machines (manufacturer_id, model_name) VALUES (1, 'Axiom')")

    register_test_user(client, "clarify4@example.com", role="technician")
    conv = client.post("/api/conversations", json={"machine_id": None}).json()

    live = client.post(
        f"/api/conversations/{conv['id']}/messages", json={"content": "Is the Axiom leaking?"}
    ).json()

    assert live["is_clarifying_question"] is True
    assert live["clarifying_options"] == [], (
        "a name match on a machine with nothing retrievable must not be offered as a candidate"
    )

    with get_conn() as conn:
        conv_row = conn.execute("SELECT machine_id FROM conversations WHERE id = %s", (conv["id"],)).fetchone()
    assert conv_row["machine_id"] is None, "the ineligible machine must never be silently set on the conversation"

"""P1-7 (independent follow-up review): "Persist clarification candidates/
pending question... Require exact live-versus-reload equality."

The live POST /messages response for an ambiguous machine mention includes
the specific candidate machines found (`clarifying_options`), but that list
was never persisted -- only `is_clarifying_question` and the prompt text
were. Reload (GET /messages) therefore reproduced the clarifying bubble's
TEXT but not its tappable candidate buttons, silently downgrading to the
frontend's generic "choose a machine" fallback -- which (a separate, related
bug an advisor review caught while fixing this) routed through the ordinary
picker flow and started a brand-new conversation instead of resuming the one
with the pending question, abandoning it rather than answering it.

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


def _seed_two_ambiguous_machines(conn):
    """'Axiom' and 'Axiom Pro' both exact-word-match a question mentioning
    'Axiom Pro' -- \\bAxiom\\b matches inside 'Axiom Pro' too -- giving a
    real two-candidate clarification without relying on fuzzy matching."""
    conn.execute("INSERT INTO manufacturers (name) VALUES ('Bunn-O-Matic Corporation')")
    conn.execute("INSERT INTO machines (manufacturer_id, model_name) VALUES (1, 'Axiom')")
    conn.execute("INSERT INTO machines (manufacturer_id, model_name) VALUES (1, 'Axiom Pro')")


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

    # Exact live-versus-reload equality (P1-7's own wording), not just "some
    # candidates showed up" -- same ids, same labels, same order.
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

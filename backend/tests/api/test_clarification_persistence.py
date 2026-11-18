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


def test_a_sibling_model_mention_with_a_machine_already_selected_offers_a_switch(test_env):
    """'Axiom' and 'Axiom Pro' are a sibling-name-overlap case (one
    machine's name is a substring of another's, e.g. 'ICB' inside
    'ICB-DV'): \\b treats a space the same as any other boundary, so a
    bare substring check on the
    CURRENT machine's own name ('Axiom') would wrongly read as present
    inside a question that's actually naming the sibling ('Axiom Pro'),
    suppressing the mismatch alert entirely. With Axiom already selected,
    a question naming Axiom Pro specifically (and not plain 'Axiom' on its
    own) must offer a one-tap switch instead of silently answering against
    the wrong sibling's manual."""
    with get_conn() as conn:
        _seed_two_ambiguous_machines(conn)

    register_test_user(client, "clarify-mismatch1@example.com", role="technician")
    conv = client.post("/api/conversations", json={"machine_id": 1}).json()
    assert conv["machine_id"] == 1

    live = client.post(
        f"/api/conversations/{conv['id']}/messages", json={"content": "Is the Axiom Pro leaking?"}
    ).json()
    assert live["is_clarifying_question"] is True
    assert {c["id"] for c in live["clarifying_options"]} == {1, 2}


def test_mentioning_the_current_machine_too_does_not_trigger_a_mismatch_alert(test_env):
    """A question that names BOTH the current machine and a sibling (e.g.
    comparing the two, or just using the base name generically) must not
    be treated as an unambiguous switch -- only an EXACT single other
    match with no mention of the current one triggers the alert."""
    with get_conn() as conn:
        _seed_two_ambiguous_machines(conn)

    register_test_user(client, "clarify-mismatch2@example.com", role="technician")
    conv = client.post("/api/conversations", json={"machine_id": 1}).json()

    live = client.post(
        f"/api/conversations/{conv['id']}/messages",
        json={"content": "Does the Axiom heat faster than the Axiom Pro?"},
    ).json()
    assert live["is_clarifying_question"] is False


def test_a_bare_mention_of_the_shorter_sibling_name_is_shorthand_not_a_switch(test_env):
    """The reverse direction of the sibling-overlap case: with the LONGER,
    more specific model selected (Axiom Pro), a bare mention of the
    shorter name it contains ('Axiom') is far more likely shorthand for
    the same machine than a deliberate switch to a different, less
    specific sibling -- this must not trigger a mismatch alert."""
    with get_conn() as conn:
        _seed_two_ambiguous_machines(conn)

    register_test_user(client, "clarify-mismatch5@example.com", role="technician")
    conv = client.post("/api/conversations", json={"machine_id": 2}).json()
    assert conv["machine_id"] == 2

    live = client.post(
        f"/api/conversations/{conv['id']}/messages", json={"content": "Is the Axiom leaking?"}
    ).json()
    assert live["is_clarifying_question"] is False


def test_a_mid_word_substring_is_not_mistaken_for_shorthand(test_env):
    """The shorthand suppression is word-bounded, not a bare substring
    check: "AJ" sits inside "Majestic" as letters, but not as its own
    word, so a mention of the real, different "AJ" machine while
    "Majestic" is selected must still prompt a switch rather than being
    wrongly read as shorthand for the current one."""
    with get_conn() as conn:
        conn.execute("INSERT INTO manufacturers (name) VALUES ('Bunn-O-Matic Corporation')")
        conn.execute("INSERT INTO machines (manufacturer_id, model_name) VALUES (1, 'Majestic')")
        conn.execute("INSERT INTO machines (manufacturer_id, model_name) VALUES (1, 'AJ')")
        _seed_eligible_document(conn, 1, "majestic")
        _seed_eligible_document(conn, 2, "aj")

    register_test_user(client, "clarify-mismatch6@example.com", role="technician")
    conv = client.post("/api/conversations", json={"machine_id": 1}).json()

    live = client.post(
        f"/api/conversations/{conv['id']}/messages", json={"content": "Is the AJ leaking?"}
    ).json()
    assert live["is_clarifying_question"] is True
    assert {c["id"] for c in live["clarifying_options"]} == {1, 2}


def _capturing_provider_setup(monkeypatch):
    """Returns `captured`, a list that gets one entry (the history list
    passed to generate()) per provider call -- lets a test see exactly
    what history a resumed answer actually received, not just that some
    answer was produced."""
    import app.api.routes_chat as routes_chat
    from app.providers.base import AIProvider, GeneratedAnswer

    captured: list[list] = []

    class _CapturingProvider(AIProvider):
        name = "test_capturing"

        def generate(self, question, machine_label, passages, history=None):
            captured.append(list(history or []))
            return GeneratedAnswer(
                answer=f"Answer about {machine_label}.", citations=[], provider=self.name, is_no_answer=False,
            )

    monkeypatch.setattr(routes_chat, "get_provider", lambda: _CapturingProvider())
    return captured


def test_confirming_a_switch_from_a_mismatch_alert_resumes_against_the_new_machine(test_env, monkeypatch):
    with get_conn() as conn:
        _seed_two_ambiguous_machines(conn)
    captured = _capturing_provider_setup(monkeypatch)

    register_test_user(client, "clarify-mismatch3@example.com", role="technician")
    conv = client.post("/api/conversations", json={"machine_id": 1}).json()
    first = client.post(f"/api/conversations/{conv['id']}/messages", json={"content": "Why is it not heating?"})
    assert first.status_code == 200
    assert captured[-1] == []

    live = client.post(
        f"/api/conversations/{conv['id']}/messages", json={"content": "Is the Axiom Pro leaking?"}
    ).json()
    other_option = next(c for c in live["clarifying_options"] if c["id"] != 1)
    assert other_option["label"].startswith("Switch to "), (
        "distinguishes this from the no-machine-selected ambiguity path's own candidate labels"
    )

    resp = client.post(f"/api/conversations/{conv['id']}/machine", json={"machine_id": other_option["id"]})
    assert resp.status_code == 200
    assert resp.json()["machine_id"] == other_option["id"]

    messages = client.get(f"/api/conversations/{conv['id']}/messages").json()
    user_messages = [m for m in messages if m["role"] == "user"]
    assert len(user_messages) == 2, "the first question plus the resumed mismatch question, no duplicates"

    history_after_switch = captured[-1]
    assert history_after_switch == [], (
        f"machine 1's prior turn must not leak into the resumed answer for machine {other_option['id']}, "
        f"got {history_after_switch}"
    )
    with get_conn() as conn:
        resumed_machine_id = conn.execute(
            "SELECT machine_id FROM messages WHERE conversation_id = %s AND role = 'assistant' "
            "AND is_clarifying_question = false ORDER BY id DESC LIMIT 1",
            (conv["id"],),
        ).fetchone()["machine_id"]
    assert resumed_machine_id == other_option["id"]


def test_keeping_the_current_machine_from_a_mismatch_alert_resumes_on_it(test_env, monkeypatch):
    with get_conn() as conn:
        _seed_two_ambiguous_machines(conn)
    captured = _capturing_provider_setup(monkeypatch)

    register_test_user(client, "clarify-mismatch4@example.com", role="technician")
    conv = client.post("/api/conversations", json={"machine_id": 1}).json()
    first = client.post(f"/api/conversations/{conv['id']}/messages", json={"content": "Why is it not heating?"})
    assert first.status_code == 200

    live = client.post(
        f"/api/conversations/{conv['id']}/messages", json={"content": "Is the Axiom Pro leaking?"}
    ).json()
    keep_option = next(c for c in live["clarifying_options"] if c["id"] == 1)
    assert keep_option["label"].startswith("Keep ")

    resp = client.post(f"/api/conversations/{conv['id']}/machine", json={"machine_id": keep_option["id"]})
    assert resp.status_code == 200
    assert resp.json()["machine_id"] == 1

    history_after_keep = captured[-1]
    assert any("heating" in h.content.lower() for h in history_after_keep), (
        "staying on machine 1 must keep machine 1's own prior turn as real follow-up context, "
        f"got {history_after_keep}"
    )
    with get_conn() as conn:
        resumed_machine_id = conn.execute(
            "SELECT machine_id FROM messages WHERE conversation_id = %s AND role = 'assistant' "
            "AND is_clarifying_question = false ORDER BY id DESC LIMIT 1",
            (conv["id"],),
        ).fetchone()["machine_id"]
    assert resumed_machine_id == 1


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

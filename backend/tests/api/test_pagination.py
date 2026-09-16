"""Phase 1 (narrowed scope, 2026-08-26): "Add stable cursor pagination for
machines, history, messages, and saved answers." Response bodies stay
plain arrays (no Android/web-UI change -- see docs/OWNER_DECISION_GATE.md
section 9); pagination rides on X-Next-Cursor/X-Has-More response headers
instead. Each test here seeds more rows than one page holds and proves the
two pages together cover every row exactly once -- not just that a header
exists, but that paging through actually works.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.db import get_conn
from app.main import app
from tests.conftest import register_test_user

client = TestClient(app)


def test_conversations_history_pagination_covers_every_row_once(test_env):
    register_test_user(client, "page-history@example.com", role="technician")
    ids = []
    for i in range(5):
        conv = client.post("/api/conversations", json={"machine_id": None}).json()
        client.post(f"/api/conversations/{conv['id']}/messages", json={"content": f"question {i}"})
        ids.append(conv["id"])

    page1 = client.get("/api/conversations", params={"limit": 3})
    assert page1.headers["X-Has-More"] == "true"
    cursor = page1.headers["X-Next-Cursor"]
    page1_ids = [c["id"] for c in page1.json()]
    assert len(page1_ids) == 3

    page2 = client.get("/api/conversations", params={"limit": 3, "cursor": cursor})
    assert page2.headers["X-Has-More"] == "false"
    assert "X-Next-Cursor" not in page2.headers
    page2_ids = [c["id"] for c in page2.json()]
    assert len(page2_ids) == 2

    assert set(page1_ids) | set(page2_ids) == set(ids)
    assert set(page1_ids).isdisjoint(page2_ids)


def test_messages_pagination_covers_every_row_once_and_default_limit_is_unaffected(test_env):
    register_test_user(client, "page-messages@example.com", role="technician")
    conv = client.post("/api/conversations", json={"machine_id": None}).json()
    for i in range(5):
        client.post(f"/api/conversations/{conv['id']}/messages", json={"content": f"question {i}"})

    # No limit/cursor passed -- an existing caller must still get everything
    # in one response, exactly like before this change.
    unpaginated = client.get(f"/api/conversations/{conv['id']}/messages")
    assert unpaginated.headers["X-Has-More"] == "false"
    all_ids = [m["id"] for m in unpaginated.json()]
    assert len(all_ids) == 10  # 5 user + 5 assistant/clarifying replies

    page1 = client.get(f"/api/conversations/{conv['id']}/messages", params={"limit": 4})
    assert page1.headers["X-Has-More"] == "true"
    cursor = page1.headers["X-Next-Cursor"]
    page1_ids = [m["id"] for m in page1.json()]
    assert page1_ids == all_ids[:4]  # oldest-first order preserved

    page2 = client.get(
        f"/api/conversations/{conv['id']}/messages", params={"limit": 4, "cursor": cursor}
    )
    page2_ids = [m["id"] for m in page2.json()]
    assert page1_ids + page2_ids == all_ids[:8]


def test_saved_answers_pagination_covers_every_row_once(test_env):
    """P1-11 (independent follow-up review, applied here 2026-09-14): save
    is now restricted to a completed, substantive assistant answer -- an
    unanswerable question (no machine/chunks seeded) produces a no-answer
    message, which is no longer a valid save target. A real chunk plus a
    code-token question (see app/providers/extractive.py's
    _code_token_rescue -- no embeddings are seeded here, so the vector gate
    itself would otherwise reject every answer) is seeded so all 5 questions
    get genuine, saveable completed answers."""
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

    register_test_user(client, "page-saved@example.com", role="technician")
    conv = client.post("/api/conversations", json={"machine_id": 1}).json()
    saved_message_ids = []
    for i in range(5):
        msg = client.post(
            f"/api/conversations/{conv['id']}/messages", json={"content": f"what does error E9 mean, case {i}"}
        ).json()
        assert msg["answer_status"] == "completed" and not msg["is_no_answer"], msg
        save_resp = client.post(f"/api/messages/{msg['id']}/save")
        assert save_resp.status_code == 201
        saved_message_ids.append(msg["id"])

    page1 = client.get("/api/saved-answers", params={"limit": 3})
    assert page1.headers["X-Has-More"] == "true"
    cursor = page1.headers["X-Next-Cursor"]
    page1_ids = [a["answer"]["id"] for a in page1.json()]

    page2 = client.get("/api/saved-answers", params={"limit": 3, "cursor": cursor})
    assert page2.headers["X-Has-More"] == "false"
    page2_ids = [a["answer"]["id"] for a in page2.json()]

    assert set(page1_ids) | set(page2_ids) == set(saved_message_ids)
    assert set(page1_ids).isdisjoint(page2_ids)


def _seed_machine(conn, machine_id, manufacturer, model_name):
    """`machine_id` is used only to derive unique per-row names/hashes below,
    not as an explicit id -- RESTART IDENTITY (tests/conftest.py's test_env
    fixture) guarantees a clean slate, and each of these three tables gets
    exactly one row per call in the same order here, so the generated
    manufacturers/machines/documents ids naturally come out in lockstep
    (1, 2, 3, ...) across all three tables, matching what callers assume."""
    conn.execute("INSERT INTO manufacturers (name) VALUES (%s)", (manufacturer,))
    conn.execute(
        "INSERT INTO machines (manufacturer_id, model_name) VALUES (%s, %s)",
        (machine_id, model_name),
    )
    conn.execute(
        "INSERT INTO documents (original_filename, storage_path, source_system, source_ref, "
        "file_type, sha256, byte_size, status, review_status, is_current_revision) "
        "VALUES (%s, %s, 'local_directory', %s, 'pdf', %s, 100, 'indexed', 'approved', true)",
        (f"doc{machine_id}.pdf", f"doc{machine_id}.pdf", f"doc{machine_id}.pdf", f"hash{machine_id}"),
    )
    conn.execute(
        "INSERT INTO document_machines (document_id, machine_id, review_status) VALUES (%s, %s, 'approved')",
        (machine_id, machine_id),
    )


def test_machine_search_pagination_covers_every_row_once(test_env):
    with get_conn() as conn:
        for i in range(5):
            _seed_machine(conn, i + 1, f"Manufacturer{i}", f"Model{i}")

    register_test_user(client, "page-machines@example.com", role="technician")

    page1 = client.get("/api/machines", params={"limit": 3})
    assert page1.headers["X-Has-More"] == "true"
    cursor = page1.headers["X-Next-Cursor"]
    page1_ids = [m["id"] for m in page1.json()]
    assert len(page1_ids) == 3

    page2 = client.get("/api/machines", params={"limit": 3, "cursor": cursor})
    assert page2.headers["X-Has-More"] == "false"
    page2_ids = [m["id"] for m in page2.json()]
    assert len(page2_ids) == 2

    assert set(page1_ids) | set(page2_ids) == {1, 2, 3, 4, 5}
    assert set(page1_ids).isdisjoint(page2_ids)


def test_recent_machines_pagination_covers_every_row_once(test_env):
    with get_conn() as conn:
        for i in range(5):
            _seed_machine(conn, i + 1, f"RecentMfr{i}", f"RecentModel{i}")

    register_test_user(client, "page-recent@example.com", role="technician")
    for i in range(5):
        client.post(f"/api/machines/{i + 1}/touch")

    page1 = client.get("/api/machines/recent", params={"limit": 3})
    assert page1.headers["X-Has-More"] == "true"
    cursor = page1.headers["X-Next-Cursor"]
    page1_ids = [m["id"] for m in page1.json()]
    assert len(page1_ids) == 3

    page2 = client.get("/api/machines/recent", params={"limit": 3, "cursor": cursor})
    assert page2.headers["X-Has-More"] == "false"
    page2_ids = [m["id"] for m in page2.json()]
    assert len(page2_ids) == 2

    assert set(page1_ids) | set(page2_ids) == {1, 2, 3, 4, 5}
    assert set(page1_ids).isdisjoint(page2_ids)


def test_invalid_cursor_returns_a_400_with_the_standard_error_envelope(test_env):
    register_test_user(client, "page-badcursor@example.com", role="technician")
    resp = client.get("/api/conversations", params={"cursor": "not-valid-base64-json!!"})
    assert resp.status_code == 400
    assert resp.json()["code"] == "BAD_REQUEST"

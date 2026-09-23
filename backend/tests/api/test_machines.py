"""Both /api/machines and /api/machines/recent must count DISTINCT
eligible document rows, not document_machines rows, and apply
active/current/approved/indexed rules consistently.

`COUNT(DISTINCT dm.document_id)` counts a column from the document_machines
side of a LEFT JOIN, which stays non-NULL even when the paired `documents`
row fails the eligibility ON-clause (wrong status, deactivated, unapproved,
superseded) -- so an inactive link would inflate the count. `COUNT(DISTINCT
d.id)` (the documents side) is NULL whenever the join's eligibility
conditions aren't met, so it only counts documents that are actually
retrievable. These tests pin that /api/machines and /api/machines/recent
can never disagree about the same machine's count, since a picker offering
a machine that then dead-ends into "no manuals" is exactly the failure this
guards against.

Also covers: `recent_machines()` must have the same `HAVING document_count
> 0` clause `search_machines()` has, or a favorited/recently-used machine
whose only manual went away (deactivated, unapproved, superseded) would
surface in `/api/machines/recent` with `document_count: 0` even though
`/api/machines` correctly hides it -- the same "dead-ends into no manuals"
failure, just reached through the other endpoint.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.db import get_conn
from app.main import app
from tests.conftest import register_test_user

client = TestClient(app)


def _insert_document(conn, doc_id, *, status="indexed", deactivated_at=None,
                      review_status="approved", is_current_revision=True):
    # doc_id is not written as an explicit id (Postgres's GENERATED ALWAYS AS
    # IDENTITY on documents.id would reject that) -- it's only used to build
    # unique per-call filenames/hashes below. RESTART IDENTITY (tests/
    # conftest.py's test_env fixture) plus every caller inserting documents
    # in the same 1, 2, ... order this argument already implies means the
    # generated id naturally comes out equal to doc_id anyway.
    conn.execute(
        "INSERT INTO documents (original_filename, storage_path, source_system, source_ref, "
        "file_type, sha256, byte_size, status, deactivated_at, review_status, is_current_revision) "
        "VALUES (%s, %s, 'local_directory', %s, 'pdf', %s, 100, %s, %s, %s, %s)",
        (f"doc{doc_id}.pdf", f"doc{doc_id}.pdf", f"doc{doc_id}.pdf", f"hash{doc_id}",
         status, deactivated_at, review_status, is_current_revision),
    )


def _link(conn, doc_id, machine_id, *, review_status="approved"):
    conn.execute(
        "INSERT INTO document_machines (document_id, machine_id, review_status) VALUES (%s, %s, %s)",
        (doc_id, machine_id, review_status),
    )


def test_document_count_excludes_deactivated_documents(test_env):
    with get_conn() as conn:
        conn.execute("INSERT INTO manufacturers (name) VALUES ('Bunn-O-Matic Corporation')")
        conn.execute("INSERT INTO machines (manufacturer_id, model_name) VALUES (1, 'Axiom')")
        _insert_document(conn, 1)
        _insert_document(conn, 2, deactivated_at="2026-01-01T00:00:00")
        _link(conn, 1, 1)
        _link(conn, 2, 1)

    register_test_user(client, "picker1@example.com", role="technician")
    results = client.get("/api/machines", params={"q": "Axiom"}).json()

    assert len(results) == 1
    assert results[0]["document_count"] == 1, (
        "a deactivated document's document_machines row is still a real row -- "
        "the old COUNT(DISTINCT dm.document_id) query counted it anyway"
    )


def test_document_count_excludes_unapproved_document_review_status(test_env):
    with get_conn() as conn:
        conn.execute("INSERT INTO manufacturers (name) VALUES ('Bunn-O-Matic Corporation')")
        conn.execute("INSERT INTO machines (manufacturer_id, model_name) VALUES (1, 'Axiom')")
        _insert_document(conn, 1)
        _insert_document(conn, 2, review_status="pending")
        _link(conn, 1, 1)
        _link(conn, 2, 1)

    register_test_user(client, "picker2@example.com", role="technician")
    results = client.get("/api/machines", params={"q": "Axiom"}).json()

    assert len(results) == 1
    assert results[0]["document_count"] == 1


def test_document_count_excludes_unapproved_link_review_status(test_env):
    """Unlike the other tests in this file, this guarantee is enforced by the
    `dm.review_status = 'approved'` condition on the LEFT JOIN's ON clause,
    not by the DISTINCT column fix -- it passes under the old
    `COUNT(DISTINCT dm.document_id)` query too. Kept here anyway since it's
    still part of "apply the rules consistently" and losing it silently
    would be worse than one test not discriminating the specific bug."""
    with get_conn() as conn:
        conn.execute("INSERT INTO manufacturers (name) VALUES ('Bunn-O-Matic Corporation')")
        conn.execute("INSERT INTO machines (manufacturer_id, model_name) VALUES (1, 'Axiom')")
        _insert_document(conn, 1)
        _insert_document(conn, 2)
        _link(conn, 1, 1, review_status="approved")
        _link(conn, 2, 1, review_status="pending")

    register_test_user(client, "picker3@example.com", role="technician")
    results = client.get("/api/machines", params={"q": "Axiom"}).json()

    assert len(results) == 1
    assert results[0]["document_count"] == 1, (
        "a pending document_machines link is not yet human-approved -- it "
        "must not count toward, or unlock, the machine appearing at all"
    )


def test_document_count_excludes_superseded_revisions(test_env):
    with get_conn() as conn:
        conn.execute("INSERT INTO manufacturers (name) VALUES ('Bunn-O-Matic Corporation')")
        conn.execute("INSERT INTO machines (manufacturer_id, model_name) VALUES (1, 'Axiom')")
        _insert_document(conn, 1)
        _insert_document(conn, 2, is_current_revision=False)
        _link(conn, 1, 1)
        _link(conn, 2, 1)

    register_test_user(client, "picker4@example.com", role="technician")
    results = client.get("/api/machines", params={"q": "Axiom"}).json()

    assert len(results) == 1
    assert results[0]["document_count"] == 1, (
        "a superseded revision has nothing retrievable -- the picker's "
        "count must match retrieval's own eligibility, not just document status"
    )


def test_machine_with_only_ineligible_documents_does_not_appear(test_env):
    """The complement of the above: not just an undercount, but the machine
    must vanish from the picker entirely once nothing it links to is
    eligible -- otherwise a technician selects a machine that dead-ends
    into "no manuals"."""
    with get_conn() as conn:
        conn.execute("INSERT INTO manufacturers (name) VALUES ('Bunn-O-Matic Corporation')")
        conn.execute("INSERT INTO machines (manufacturer_id, model_name) VALUES (1, 'Axiom')")
        _insert_document(conn, 1, deactivated_at="2026-01-01T00:00:00")
        _link(conn, 1, 1)

    register_test_user(client, "picker5@example.com", role="technician")
    results = client.get("/api/machines", params={"q": "Axiom"}).json()

    assert results == []


def test_search_and_recent_machines_report_the_same_document_count(test_env):
    """/api/machines and /api/machines/recent must never disagree about the
    same machine's count -- the review's "apply the rules consistently"
    half. Seeds one eligible and one ineligible (deactivated) link so a
    query that only fixed one of the two endpoints would be caught here."""
    with get_conn() as conn:
        conn.execute("INSERT INTO manufacturers (name) VALUES ('Bunn-O-Matic Corporation')")
        conn.execute("INSERT INTO machines (manufacturer_id, model_name) VALUES (1, 'Axiom')")
        _insert_document(conn, 1)
        _insert_document(conn, 2, deactivated_at="2026-01-01T00:00:00")
        _link(conn, 1, 1)
        _link(conn, 2, 1)

    register_test_user(client, "picker6@example.com", role="technician")
    client.post("/api/machines/1/touch")

    search_result = client.get("/api/machines", params={"q": "Axiom"}).json()
    recent_result = client.get("/api/machines/recent").json()

    assert len(search_result) == 1
    assert len(recent_result) == 1
    assert search_result[0]["document_count"] == recent_result[0]["document_count"] == 1


def test_recent_machines_drops_a_favorite_whose_only_manual_went_away(test_env):
    """recent_machines() must have the same HAVING clause search_machines()
    has, or a machine a technician favorited or recently viewed could still
    show up in /api/machines/recent with document_count: 0 after its only
    manual was deactivated -- a dead-end reachable through the recents list
    even though /api/machines correctly hides the same machine."""
    with get_conn() as conn:
        conn.execute("INSERT INTO manufacturers (name) VALUES ('Bunn-O-Matic Corporation')")
        conn.execute("INSERT INTO machines (manufacturer_id, model_name) VALUES (1, 'Axiom')")
        _insert_document(conn, 1)
        _link(conn, 1, 1)

    register_test_user(client, "picker7@example.com", role="technician")
    client.post("/api/machines/1/touch")
    client.post("/api/machines/1/favorite", params={"favorite": True})

    with get_conn() as conn:
        conn.execute("UPDATE documents SET deactivated_at = now() WHERE id = 1")

    recent_result = client.get("/api/machines/recent").json()

    assert recent_result == [], (
        "a favorited machine whose only manual is now deactivated has "
        "nothing retrievable -- it must not surface in recents at all, not "
        "with document_count: 0"
    )


def test_p1_12_search_reports_is_favorite_for_a_favorited_machine(test_env):
    """search_machines()'s SQL must join to recent_machines so
    _row_to_machine's "is_favorite" key reports the real value -- otherwise
    a favorited machine would draw an empty star in search results (only
    /api/machines/recent, which does join recent_machines, would report the
    real value). The Android client's optimistic toggle flips
    !machine.is_favorite, so a wrong initial state would send the wrong
    direction on first tap."""
    with get_conn() as conn:
        conn.execute("INSERT INTO manufacturers (name) VALUES ('Bunn-O-Matic Corporation')")
        conn.execute("INSERT INTO machines (manufacturer_id, model_name) VALUES (1, 'Axiom')")
        _insert_document(conn, 1)
        _link(conn, 1, 1)

    register_test_user(client, "picker8@example.com", role="technician")
    client.post("/api/machines/1/favorite", params={"favorite": True})

    search_result = client.get("/api/machines", params={"q": "Axiom"}).json()

    assert len(search_result) == 1
    assert search_result[0]["is_favorite"] is True, (
        f"a favorited machine must report is_favorite: true from search too, got {search_result[0]}"
    )


def test_p1_12_search_does_not_leak_another_users_favorite(test_env):
    """Companion to the test above: is_favorite in search results must be
    scoped to the requesting user, not any technician who favorited it."""
    with get_conn() as conn:
        conn.execute("INSERT INTO manufacturers (name) VALUES ('Bunn-O-Matic Corporation')")
        conn.execute("INSERT INTO machines (manufacturer_id, model_name) VALUES (1, 'Axiom')")
        _insert_document(conn, 1)
        _link(conn, 1, 1)

    register_test_user(client, "picker9-favoriter@example.com", role="technician")
    client.post("/api/machines/1/favorite", params={"favorite": True})

    register_test_user(client, "picker9-other@example.com", role="technician")
    search_result = client.get("/api/machines", params={"q": "Axiom"}).json()

    assert search_result[0]["is_favorite"] is False

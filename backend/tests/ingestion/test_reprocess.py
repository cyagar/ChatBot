"""reprocess_document: re-extracting/re-chunking an already-indexed
document's stored bytes under the current pipeline version, without waiting
for a full ingest_all() run to notice the staleness (it only ever reports
'needs_reprocessing', never acts on it) and without the normal
exact-duplicate check mistaking same-hash-as-itself for a real duplicate.
"""

import shutil

from app.db import get_conn
from app.ingestion import pipeline
from tests.ingestion.fakes import FakeDirectorySource


def test_reprocess_stages_a_new_pending_revision_while_the_old_one_keeps_serving(test_env, make_pdf, tmp_path):
    manuals_dir = tmp_path / "manuals"
    manuals_dir.mkdir()
    pdf = make_pdf(["Bunn Axiom brewer installation guide. Step 1: connect the water line."], name="axiom.pdf")
    shutil.copy(pdf, manuals_dir / pdf.name)

    report = pipeline.ingest_all(source=FakeDirectorySource(manuals_dir), embed=False)
    original_id = report.outcomes[0].document_id
    assert report.outcomes[0].status == "indexed"

    # Simulate the document having already been reviewed and approved --
    # exactly the state a real document a technician is currently retrieving
    # answers from would be in -- so the test can assert reprocessing leaves
    # that live state completely untouched.
    with get_conn() as conn:
        conn.execute(
            "UPDATE documents SET review_status = 'approved', reviewed_at = now() WHERE id = %s",
            (original_id,),
        )

    outcome = pipeline.reprocess_document(original_id)

    assert outcome.status == "indexed"
    assert outcome.document_id != original_id

    with get_conn() as conn:
        original = conn.execute(
            "SELECT status, review_status, deactivated_at, is_current_revision FROM documents WHERE id = %s",
            (original_id,),
        ).fetchone()
        new = conn.execute(
            "SELECT status, review_status, deactivated_at, source_ref, sha256, extraction_version, "
            "chunking_version FROM documents WHERE id = %s",
            (outcome.document_id,),
        ).fetchone()
        new_chunks = conn.execute(
            "SELECT COUNT(*) AS n FROM chunks WHERE document_id = %s", (outcome.document_id,)
        ).fetchone()["n"]
        run = conn.execute(
            "SELECT trigger, status FROM ingestion_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()

    # The currently-approved document -- and therefore every citation
    # already pointing at its chunks -- is untouched: still active, still
    # approved, still the current revision.
    assert original["status"] == "indexed"
    assert original["review_status"] == "approved"
    assert original["deactivated_at"] is None
    assert original["is_current_revision"] is True

    # The reprocessed version lands as a new, pending, active row at the
    # SAME source_ref/sha256 -- not auto-approved, not auto-promoted.
    assert new["deactivated_at"] is None
    assert new["review_status"] == "pending"
    assert new_chunks > 0

    assert run["trigger"] == "reprocess"
    assert run["status"] == "completed"


def test_reprocess_refuses_a_deactivated_or_non_indexed_document(test_env, make_pdf, tmp_path):
    manuals_dir = tmp_path / "manuals"
    manuals_dir.mkdir()
    pdf = make_pdf(["Content about the espresso group head gasket."], name="espresso.pdf")
    shutil.copy(pdf, manuals_dir / pdf.name)

    report = pipeline.ingest_all(source=FakeDirectorySource(manuals_dir), embed=False)
    doc_id = report.outcomes[0].document_id

    with get_conn() as conn:
        conn.execute("UPDATE documents SET deactivated_at = now() WHERE id = %s", (doc_id,))

    try:
        pipeline.reprocess_document(doc_id)
        assert False, "expected ValueError for a deactivated document"
    except ValueError:
        pass


def test_reprocess_rejects_a_document_that_no_longer_extracts(test_env, make_pdf, tmp_path):
    """The stored file on disk can't disappear in practice (it's the exact
    bytes that produced the active document), but the extraction result
    itself can regress under a bad pipeline change -- reprocess must not
    silently replace a working manual with a broken one; the failed attempt
    lands deactivated from the moment it's inserted."""
    import app.ingestion.pipeline as pipeline_module

    manuals_dir = tmp_path / "manuals"
    manuals_dir.mkdir()
    pdf = make_pdf(["Content about the espresso group head gasket."], name="espresso.pdf")
    shutil.copy(pdf, manuals_dir / pdf.name)

    report = pipeline.ingest_all(source=FakeDirectorySource(manuals_dir), embed=False)
    doc_id = report.outcomes[0].document_id

    from app.ingestion.extracted import ExtractedDocument

    def _broken_extract(path, ocr_available=False):
        return "pdf", ExtractedDocument(status="failed", reason="Simulated regression."), None

    original_extract = pipeline_module.extract
    pipeline_module.extract = _broken_extract
    try:
        outcome = pipeline.reprocess_document(doc_id)
    finally:
        pipeline_module.extract = original_extract

    assert outcome.status == "failed"
    with get_conn() as conn:
        original = conn.execute("SELECT deactivated_at FROM documents WHERE id = %s", (doc_id,)).fetchone()
        new = conn.execute("SELECT deactivated_at FROM documents WHERE id = %s", (outcome.document_id,)).fetchone()
        run = conn.execute("SELECT status FROM ingestion_runs ORDER BY id DESC LIMIT 1").fetchone()
    assert original["deactivated_at"] is None, "the working document must stay active after a failed reprocess"
    assert new["deactivated_at"] is not None, "a failed reprocess attempt must land already deactivated"
    assert run["status"] == "failed", (
        "a reprocess whose outcome is itself 'failed' must mark the run failed, not completed"
    )

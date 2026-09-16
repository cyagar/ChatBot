"""Integration tests for the ingestion pipeline against a real temp SQLite DB
and real (synthetic) PDF files — no mocking of extraction or storage, since
idempotency bugs live exactly in that plumbing.

Exercised via FakeDirectorySource (tests/ingestion/fakes.py), not the real
GoogleDriveSource -- these tests are about pipeline.py's idempotency/dedup
logic, which is source-agnostic, not about Drive connectivity."""

import shutil

import pytest

from app.db import get_conn
from app.ingestion.pipeline import ingest_all
from tests.ingestion.fakes import FakeDirectorySource


@pytest.fixture
def manuals_dir(tmp_path):
    d = tmp_path / "manuals"
    d.mkdir()
    return d


def test_full_run_indexes_every_file_exactly_once(test_env, make_pdf, manuals_dir):
    axiom = make_pdf(["Bunn Axiom brewer installation guide. Step 1: connect water line."], name="axiom.pdf")
    cma = make_pdf(["CMA 180UC dishmachine owner's manual. Rinse arm cleaning steps follow."], name="cma.pdf")
    shutil.copy(axiom, manuals_dir / axiom.name)
    shutil.copy(cma, manuals_dir / cma.name)

    report = ingest_all(source=FakeDirectorySource(manuals_dir), embed=False)
    counts = report.counts()
    assert sum(counts.values()) == len(list(manuals_dir.glob("*.pdf")))
    assert counts.get("indexed", 0) == len(list(manuals_dir.glob("*.pdf")))


def test_ingest_all_records_which_trigger_started_the_run(test_env, make_pdf, manuals_dir):
    """P1-4: distinguishes a scheduler-started run from an admin's manual
    'Run re-index now' click, so the admin UI can show the scheduler is
    actually running rather than taking it on faith. Default stays 'manual'
    -- callers that don't pass trigger explicitly (e.g. the reindex endpoint)
    must not be silently miscategorized as 'scheduled'."""
    pdf = make_pdf(["Content about the espresso group head gasket."], name="espresso.pdf")
    shutil.copy(pdf, manuals_dir / pdf.name)
    source = FakeDirectorySource(manuals_dir)

    manual_report = ingest_all(source=source, embed=False)
    scheduled_report = ingest_all(source=source, embed=False, trigger="scheduled")

    from app.db import get_conn
    with get_conn() as conn:
        triggers = {
            r["id"]: r["trigger"]
            for r in conn.execute("SELECT id, trigger FROM ingestion_runs ORDER BY id").fetchall()
        }
    assert triggers[manual_report.run_id] == "manual"
    assert triggers[scheduled_report.run_id] == "scheduled"


def test_second_run_skips_unchanged_files(test_env, make_pdf, manuals_dir):
    pdf = make_pdf(["Some manual content about replacing the inlet valve."])
    shutil.copy(pdf, manuals_dir / pdf.name)

    source = FakeDirectorySource(manuals_dir)
    first = ingest_all(source=source, embed=False)
    assert first.counts().get("indexed") == 1

    second = ingest_all(source=source, embed=False)
    assert second.counts() == {"skipped_unchanged": 1}


def test_unchanged_file_at_a_stale_pipeline_version_is_flagged_not_silently_skipped(
    test_env, make_pdf, manuals_dir, monkeypatch
):
    """Independent follow-up review 2026-08-24 P0-7: 'unchanged manuals don't
    receive new pipeline logic.' A document whose bytes never change used to
    be skipped forever regardless of whether extraction/chunking logic
    changed since it was last processed. Simulates a pipeline version bump
    (CURRENT_CHUNKING_VERSION going from 1 to 2) and confirms an otherwise-
    unchanged file is reported as needs_reprocessing, not silently absorbed
    into skipped_unchanged -- and that it is NOT auto-reprocessed (the
    document's chunking_version in the DB stays at what actually produced its
    current chunks, not the new code's version, since nothing re-chunked it)."""
    pdf = make_pdf(["Content about the ice cream machine compressor cycle."])
    shutil.copy(pdf, manuals_dir / pdf.name)
    source = FakeDirectorySource(manuals_dir)

    first = ingest_all(source=source, embed=False)
    assert first.counts().get("indexed") == 1

    import app.ingestion.pipeline as pipeline_module
    monkeypatch.setattr(pipeline_module, "CURRENT_CHUNKING_VERSION", 2)

    second = ingest_all(source=source, embed=False)
    assert second.counts() == {"needs_reprocessing": 1}

    with get_conn() as conn:
        doc = conn.execute(
            "SELECT chunking_version, status FROM documents WHERE status = 'indexed'"
        ).fetchone()
    assert doc["chunking_version"] == 1, "not auto-reprocessed -- version stays at what actually produced its chunks"
    assert doc["status"] == "indexed", "the document itself is untouched, only flagged"


def test_third_consecutive_run_still_stable(test_env, make_pdf, manuals_dir):
    """The real acceptance criterion: indexing must be idempotent across
    repeated runs, not just the second one."""
    pdf = make_pdf(["Content about the coffee brewer thermostat calibration procedure."])
    shutil.copy(pdf, manuals_dir / pdf.name)

    source = FakeDirectorySource(manuals_dir)
    ingest_all(source=source, embed=False)
    ingest_all(source=source, embed=False)
    third = ingest_all(source=source, embed=False)
    assert third.counts() == {"skipped_unchanged": 1}

    from app.db import get_conn
    with get_conn() as conn:
        active_count = conn.execute(
            "SELECT COUNT(*) c FROM documents WHERE deactivated_at IS NULL"
        ).fetchone()["c"]
    assert active_count == 1, "repeated runs must not accumulate duplicate rows for an unchanged file"


def test_exact_duplicate_bytes_are_flagged_and_excluded_from_retrieval(test_env, make_pdf, manuals_dir):
    pdf = make_pdf(["Identical content appearing under two different filenames in the source."])
    shutil.copy(pdf, manuals_dir / "original_name.pdf")
    shutil.copy(pdf, manuals_dir / "renamed_copy.pdf")

    report = ingest_all(source=FakeDirectorySource(manuals_dir), embed=False)
    counts = report.counts()
    assert counts.get("indexed") == 1
    assert counts.get("duplicate") == 1

    from app.db import get_conn
    with get_conn() as conn:
        dup_count = conn.execute("SELECT COUNT(*) c FROM duplicate_matches WHERE match_type='exact_hash'").fetchone()["c"]
        chunk_count_for_dup = conn.execute(
            "SELECT COUNT(*) c FROM chunks c JOIN documents d ON d.id=c.document_id WHERE d.status='duplicate'"
        ).fetchone()["c"]
    assert dup_count == 1
    assert chunk_count_for_dup == 0, "a duplicate document must not contribute its own chunks to retrieval"


def test_content_change_at_same_path_creates_new_pending_row_without_deactivating_old(
    test_env, make_pdf, manuals_dir
):
    """Independent follow-up review P0-2 (2026-08-24 follow-up): a replacement
    that merely extracts/chunks successfully must NOT retire the manual it's
    replacing -- the new row's review_status defaults to 'pending' (migration
    0003), so deactivating the old one here would leave technicians with zero
    approved documents at this source_ref until a human happens to review it.
    Both rows must stay active until an admin approves the replacement (see
    tests/api/test_admin.py for the approval-time cutover)."""
    path = manuals_dir / "evolving.pdf"

    v1 = make_pdf(["Version one content about the water filter."])
    shutil.copy(v1, path)
    source = FakeDirectorySource(manuals_dir)
    ingest_all(source=source, embed=False)

    with get_conn() as conn:
        conn.execute("UPDATE documents SET review_status = 'approved' WHERE source_ref = %s",
                     (f"test_directory:{path.name}",))

    v2 = make_pdf(["Version two content, completely rewritten about the water filter replacement."], name="v2.pdf")
    shutil.copy(v2, path)  # same source_ref (path relative to the corpus root), different bytes
    ingest_all(source=source, embed=False)

    source_ref = f"test_directory:{path.name}"
    with get_conn() as conn:
        active = conn.execute(
            "SELECT id, review_status FROM documents WHERE source_ref = %s AND deactivated_at IS NULL "
            "ORDER BY ingested_at, id", (source_ref,)
        ).fetchall()
    assert len(active) == 2, \
        "the already-approved old document must stay active alongside the new pending one, not get retired on ingest"
    assert active[0]["review_status"] == "approved"
    assert active[1]["review_status"] == "pending", "a fresh replacement must not be auto-approved"


def test_relocated_corpus_root_does_not_create_a_duplicate_row(test_env, make_pdf, manuals_dir, tmp_path):
    """Independent review evidence: 'A relocation test proved that moving an
    unchanged manual to another local folder creates a new duplicate document
    row' (71 -> 72 document rows for the same bytes). source_ref must be
    relative to the corpus root, not an absolute path, so the same file under
    a differently-located root is still recognized as the same document."""
    pdf = make_pdf(["Content about the ice machine condenser cleaning schedule."], name="relocatable.pdf")
    shutil.copy(pdf, manuals_dir / pdf.name)
    ingest_all(source=FakeDirectorySource(manuals_dir), embed=False)

    relocated_dir = tmp_path / "relocated_manuals_root"
    relocated_dir.mkdir()
    shutil.copy(pdf, relocated_dir / pdf.name)
    second = ingest_all(source=FakeDirectorySource(relocated_dir), embed=False)
    assert second.counts() == {"skipped_unchanged": 1}

    from app.db import get_conn
    with get_conn() as conn:
        active_count = conn.execute(
            "SELECT COUNT(*) c FROM documents WHERE deactivated_at IS NULL"
        ).fetchone()["c"]
    assert active_count == 1, "relocating the corpus root must not create a duplicate document row"


def test_document_missing_from_a_later_listing_is_not_deactivated(test_env, make_pdf, manuals_dir):
    """Independent follow-up review P0-2/P1-1: the pipeline iterates whatever
    files a listing happens to return but has no reconciliation step for
    active rows a listing doesn't mention -- documented in
    PRODUCTION_READINESS.md as 'true today, but not exercised by a dedicated
    test' until this one. A manual temporarily absent from a source listing
    (Drive outage, a partial page, a file briefly unshared) must not be
    deactivated just because one run's listing didn't include it -- there is
    no 'complete, verified listing' concept implemented to safely tell that
    apart from a real removal, so the current, deliberately conservative
    behavior is: never deactivate on absence alone."""
    pdf = make_pdf(["Content about the fryer oil filtration schedule."], name="fryer.pdf")
    shutil.copy(pdf, manuals_dir / pdf.name)
    source = FakeDirectorySource(manuals_dir)
    first = ingest_all(source=source, embed=False)
    assert first.counts() == {"indexed": 1}

    from app.db import get_conn
    with get_conn() as conn:
        original_id = conn.execute("SELECT id FROM documents WHERE deactivated_at IS NULL").fetchone()["id"]

    (manuals_dir / pdf.name).unlink()  # the file disappears from the next listing
    second = ingest_all(source=source, embed=False)
    assert second.counts() == {}, "an empty listing must not itself produce any outcome for the missing file"

    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, deactivated_at FROM documents WHERE id = %s", (original_id,)
        ).fetchone()
    assert row["deactivated_at"] is None, (
        "a document absent from one listing must stay active -- there is no safe way yet to "
        "distinguish a real removal from an outage or a partial listing"
    )


def test_failed_replacement_does_not_retire_the_still_good_active_document(test_env, make_pdf, manuals_dir):
    """Independent follow-up review P0-2: the active row used to be deactivated
    the moment content changed at a source_ref, before the replacement was
    extracted or validated. A corrupt/unreadable replacement must not take
    down a manual that was still working."""
    import fitz

    path = manuals_dir / "evolving.pdf"
    v1 = make_pdf(["Version one content about the water filter, valid and indexable."])
    shutil.copy(v1, path)
    source = FakeDirectorySource(manuals_dir)
    first = ingest_all(source=source, embed=False)
    assert first.counts() == {"indexed": 1}

    from app.db import get_conn
    with get_conn() as conn:
        original_id = conn.execute(
            "SELECT id FROM documents WHERE deactivated_at IS NULL"
        ).fetchone()["id"]

    # Replace the same path with an unreadable file (no text layer, no OCR
    # configured) -- same source_ref, different bytes, extraction fails.
    doc = fitz.open()
    doc.new_page()
    doc.save(path)
    doc.close()

    second = ingest_all(source=source, embed=False)
    assert second.counts() == {"unsupported": 1}

    with get_conn() as conn:
        active = conn.execute(
            "SELECT id, status FROM documents WHERE deactivated_at IS NULL"
        ).fetchall()
        failed_candidate = conn.execute(
            "SELECT id, deactivated_at FROM documents WHERE status = 'unsupported'"
        ).fetchone()

    assert len(active) == 1, "exactly one row must remain active at this source_ref"
    assert active[0]["id"] == original_id, "the original valid document must still be the active one"
    assert active[0]["status"] == "indexed"
    assert failed_candidate["deactivated_at"] is not None, (
        "the failed replacement candidate must be recorded but inserted already-inactive, "
        "not left active alongside the still-good document"
    )


def test_listing_failure_still_produces_a_visible_failed_run(test_env, manuals_dir):
    """Independent follow-up review P0-3: source.list_files() used to run
    before the ingestion_runs row was created, so a Drive auth/quota/network
    failure aborted the whole operation before any run existed -- the admin
    UI showed nothing happened. The run row must exist first, and a listing
    failure must land as a visible failed run with a recorded reason."""
    import pytest as _pytest

    class ExplodingSource:
        source_system = "exploding"

        def list_files(self):
            raise RuntimeError("simulated Drive auth failure")

    with _pytest.raises(RuntimeError, match="simulated Drive auth failure"):
        ingest_all(source=ExplodingSource(), embed=False)

    from app.db import get_conn
    with get_conn() as conn:
        run = conn.execute(
            "SELECT id, status FROM ingestion_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        event = conn.execute(
            "SELECT detail FROM ingestion_events WHERE run_id = %s ORDER BY id DESC LIMIT 1",
            (run["id"],),
        ).fetchone()

    assert run["status"] == "failed"
    assert "simulated Drive auth failure" in event["detail"]


def test_source_level_skips_are_recorded_as_visible_ingestion_events(test_env, make_pdf, manuals_dir):
    """Independent follow-up review P1-3: 'report every skipped item.' A
    DocumentSource can report items it noticed but didn't return from
    list_files() (e.g. GoogleDriveSource skipping an oversized file or a
    Google Workspace document) via pop_skipped(); ingest_all() must surface
    each one as a normal ingestion_events row and FileOutcome, not only a
    server log line an admin never sees."""
    from app.ingestion.sources import SkippedFile

    class SourceWithSkips(FakeDirectorySource):
        def pop_skipped(self):
            return [SkippedFile("huge_manual.pdf", "File is 350.0 MB, exceeding the 200 MB per-file limit.")]

    pdf = make_pdf(["Content about the walk-in cooler thermostat."], name="cooler.pdf")
    shutil.copy(pdf, manuals_dir / pdf.name)

    report = ingest_all(source=SourceWithSkips(manuals_dir), embed=False)

    assert report.counts().get("skipped") == 1
    assert report.counts().get("indexed") == 1

    from app.db import get_conn
    with get_conn() as conn:
        event = conn.execute(
            "SELECT original_filename, event, detail, document_id FROM ingestion_events "
            "WHERE run_id = %s AND event = 'skipped'", (report.run_id,),
        ).fetchone()
    assert event["original_filename"] == "huge_manual.pdf"
    assert "350.0 MB" in event["detail"]
    assert event["document_id"] is None, "a skip never produced a document row"


def test_unsupported_file_retried_after_capability_change_updates_in_place(test_env, manuals_dir):
    """Simulates a file that is unsupported on first pass (e.g. no OCR) and
    becomes indexable later without any bytes changing (e.g. OCR configured) —
    must update the existing row, not accumulate a new one every run."""
    import fitz

    doc = fitz.open()
    doc.new_page()  # no text layer -> unsupported without OCR
    path = manuals_dir / "scanned.pdf"
    doc.save(path)
    doc.close()

    source = FakeDirectorySource(manuals_dir)
    r1 = ingest_all(source=source, embed=False)
    assert r1.counts() == {"unsupported": 1}

    r2 = ingest_all(source=source, embed=False)
    assert r2.counts() == {"unsupported": 1}, "still-unsupported file should be re-attempted, not skipped forever"

    from app.db import get_conn
    with get_conn() as conn:
        rows = conn.execute("SELECT COUNT(*) c FROM documents WHERE deactivated_at IS NULL").fetchone()["c"]
        total_rows = conn.execute("SELECT COUNT(*) c FROM documents").fetchone()["c"]
    assert rows == 1
    assert total_rows == 1, "an unchanged outcome must update the row in place, not churn new rows every run"

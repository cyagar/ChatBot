"""Integration tests for the ingestion pipeline against a real temp SQLite DB
and real (synthetic) PDF files — no mocking of extraction or storage, since
idempotency bugs live exactly in that plumbing."""

import shutil

from app.ingestion.pipeline import ingest_all
from app.ingestion.sources import LocalDirectorySource


def test_full_run_indexes_every_file_exactly_once(test_env, make_pdf):
    manuals_dir = test_env.local_manuals_dir_resolved
    axiom = make_pdf(["Bunn Axiom brewer installation guide. Step 1: connect water line."], name="axiom.pdf")
    cma = make_pdf(["CMA 180UC dishmachine owner's manual. Rinse arm cleaning steps follow."], name="cma.pdf")
    shutil.copy(axiom, manuals_dir / axiom.name)
    shutil.copy(cma, manuals_dir / cma.name)

    report = ingest_all(source=LocalDirectorySource(manuals_dir), embed=False)
    counts = report.counts()
    assert sum(counts.values()) == len(list(manuals_dir.glob("*.pdf")))
    assert counts.get("indexed", 0) == len(list(manuals_dir.glob("*.pdf")))


def test_second_run_skips_unchanged_files(test_env, make_pdf):
    manuals_dir = test_env.local_manuals_dir_resolved
    pdf = make_pdf(["Some manual content about replacing the inlet valve."])
    shutil.copy(pdf, manuals_dir / pdf.name)

    source = LocalDirectorySource(manuals_dir)
    first = ingest_all(source=source, embed=False)
    assert first.counts().get("indexed") == 1

    second = ingest_all(source=source, embed=False)
    assert second.counts() == {"skipped_unchanged": 1}


def test_third_consecutive_run_still_stable(test_env, make_pdf):
    """The real acceptance criterion: indexing must be idempotent across
    repeated runs, not just the second one."""
    manuals_dir = test_env.local_manuals_dir_resolved
    pdf = make_pdf(["Content about the coffee brewer thermostat calibration procedure."])
    shutil.copy(pdf, manuals_dir / pdf.name)

    source = LocalDirectorySource(manuals_dir)
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


def test_exact_duplicate_bytes_are_flagged_and_excluded_from_retrieval(test_env, make_pdf):
    manuals_dir = test_env.local_manuals_dir_resolved
    pdf = make_pdf(["Identical content appearing under two different filenames in the source."])
    shutil.copy(pdf, manuals_dir / "original_name.pdf")
    shutil.copy(pdf, manuals_dir / "renamed_copy.pdf")

    report = ingest_all(source=LocalDirectorySource(manuals_dir), embed=False)
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


def test_content_change_at_same_path_creates_new_current_row(test_env, make_pdf, tmp_path):
    manuals_dir = test_env.local_manuals_dir_resolved
    path = manuals_dir / "evolving.pdf"

    v1 = make_pdf(["Version one content about the water filter."])
    shutil.copy(v1, path)
    source = LocalDirectorySource(manuals_dir)
    ingest_all(source=source, embed=False)

    v2 = make_pdf(["Version two content, completely rewritten about the water filter replacement."], name="v2.pdf")
    shutil.copy(v2, path)  # same source_ref (path relative to the corpus root), different bytes
    ingest_all(source=source, embed=False)

    source_ref = f"local_directory:{path.name}"
    from app.db import get_conn
    with get_conn() as conn:
        active = conn.execute(
            "SELECT id FROM documents WHERE source_ref = ? AND deactivated_at IS NULL", (source_ref,)
        ).fetchall()
        all_rows = conn.execute(
            "SELECT id, deactivated_at FROM documents WHERE source_ref = ?", (source_ref,)
        ).fetchall()
    assert len(active) == 1, "only the latest content for a given path should be active"
    assert len(all_rows) == 2, "the superseded version should be kept (deactivated) for audit, not deleted"


def test_relocated_corpus_root_does_not_create_a_duplicate_row(test_env, make_pdf, tmp_path):
    """Independent review evidence: 'A relocation test proved that moving an
    unchanged manual to another local folder creates a new duplicate document
    row' (71 -> 72 document rows for the same bytes). source_ref must be
    relative to the corpus root, not an absolute path, so the same file under
    a differently-located root is still recognized as the same document."""
    manuals_dir = test_env.local_manuals_dir_resolved
    pdf = make_pdf(["Content about the ice machine condenser cleaning schedule."], name="relocatable.pdf")
    shutil.copy(pdf, manuals_dir / pdf.name)
    ingest_all(source=LocalDirectorySource(manuals_dir), embed=False)

    relocated_dir = tmp_path / "relocated_manuals_root"
    relocated_dir.mkdir()
    shutil.copy(pdf, relocated_dir / pdf.name)
    second = ingest_all(source=LocalDirectorySource(relocated_dir), embed=False)
    assert second.counts() == {"skipped_unchanged": 1}

    from app.db import get_conn
    with get_conn() as conn:
        active_count = conn.execute(
            "SELECT COUNT(*) c FROM documents WHERE deactivated_at IS NULL"
        ).fetchone()["c"]
    assert active_count == 1, "relocating the corpus root must not create a duplicate document row"


def test_unsupported_file_retried_after_capability_change_updates_in_place(test_env, tmp_path):
    """Simulates a file that is unsupported on first pass (e.g. no OCR) and
    becomes indexable later without any bytes changing (e.g. OCR configured) —
    must update the existing row, not accumulate a new one every run."""
    import fitz

    manuals_dir = test_env.local_manuals_dir_resolved
    doc = fitz.open()
    doc.new_page()  # no text layer -> unsupported without OCR
    path = manuals_dir / "scanned.pdf"
    doc.save(path)
    doc.close()

    source = LocalDirectorySource(manuals_dir)
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

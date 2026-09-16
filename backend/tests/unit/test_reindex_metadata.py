"""P1-10 (independent follow-up review): "reindex_metadata.py STILL DOES NOT
MATCH ITS DOCUMENTATION... update each non-overridden metadata field
transactionally, preserve per-field human overrides, clear resolved stale
notes, and test every promised field." The script's docstring always
promised manufacturer/doc_type/title/revision/doc_number/machine_links, but
the code only ever touched machine_links -- these tests cover the fix,
field by field, plus the two things a naive per-document (not per-field)
override check or a naive notes-append would get wrong.

extract() and extract_metadata() are monkeypatched per test: the interesting
behavior here is reindex_documents()'s diffing/override-skip/apply logic,
not PDF text extraction (covered elsewhere), and controlling the extracted
DocMetadata directly keeps each test's intent explicit.
"""

from __future__ import annotations

from types import SimpleNamespace

from app.db import get_conn
from app.ingestion.metadata import DocMetadata, MachineMatch
from scripts import reindex_metadata as rm


def _seed_document(conn, storage_dir, *, doc_id=1, filename="axiom.pdf",
                    manufacturer_name=None, doc_type="unknown", title="Untitled",
                    revision=None, doc_number=None, status_reason=None):
    # doc_id is not written as an explicit id (Postgres's GENERATED ALWAYS AS
    # IDENTITY on documents.id would reject that) -- it's only used to build
    # a unique sha256 below. RESTART IDENTITY (tests/conftest.py's test_env
    # fixture) plus every caller inserting documents in the same 1, 2, ...
    # order this argument already implies means the generated id naturally
    # comes out equal to doc_id anyway.
    manu_id = None
    if manufacturer_name:
        conn.execute(
            "INSERT INTO manufacturers (name) VALUES (%s) ON CONFLICT (name) DO NOTHING", (manufacturer_name,)
        )
        manu_id = conn.execute(
            "SELECT id FROM manufacturers WHERE name = %s", (manufacturer_name,)
        ).fetchone()["id"]
    conn.execute(
        "INSERT INTO documents (original_filename, storage_path, source_system, source_ref, "
        "file_type, sha256, byte_size, status, review_status, manufacturer_id, doc_type, title, "
        "revision, doc_number, status_reason) "
        "VALUES (%s, %s, 'local_directory', %s, 'pdf', %s, 100, 'indexed', 'approved', %s, %s, %s, %s, %s, %s)",
        (filename, filename, filename, f"hash{doc_id}", manu_id, doc_type, title,
         revision, doc_number, status_reason),
    )
    (storage_dir / filename).write_bytes(b"dummy")


def _override(conn, doc_id, field_name, corrected_value="whatever"):
    conn.execute(
        "INSERT INTO metadata_overrides (document_id, field, corrected_value, corrected_by) "
        "VALUES (%s, %s, %s, 'admin@example.com')",
        (doc_id, field_name, corrected_value),
    )


def _patch_extraction(monkeypatch, meta_by_filename: dict[str, DocMetadata]):
    def fake_extract(path, ocr_available):
        return "pdf", SimpleNamespace(pages=[]), None

    def fake_extract_metadata(filename, extracted):
        return meta_by_filename[filename]

    monkeypatch.setattr(rm, "extract", fake_extract)
    monkeypatch.setattr(rm, "extract_metadata", fake_extract_metadata)


def _meta(**overrides):
    defaults = dict(manufacturer=None, doc_type="unknown", title="Untitled", revision=None, doc_number=None)
    defaults.update(overrides)
    return DocMetadata(**defaults)


def _read_document(conn, doc_id):
    return conn.execute(
        "SELECT d.*, mf.name AS manufacturer_name FROM documents d "
        "LEFT JOIN manufacturers mf ON mf.id = d.manufacturer_id WHERE d.id = %s",
        (doc_id,),
    ).fetchone()


def test_manufacturer_updates_when_not_overridden(test_env, monkeypatch):
    with get_conn() as conn:
        storage_dir = __import__("app.config", fromlist=["get_settings"]).get_settings().local_storage_dir_resolved
        storage_dir.mkdir(parents=True, exist_ok=True)
        _seed_document(conn, storage_dir, manufacturer_name=None)
        _patch_extraction(monkeypatch, {"axiom.pdf": _meta(manufacturer="Bunn-O-Matic Corporation")})

        report = rm.reindex_documents(conn, storage_dir, ocr_available=False, apply=True)

        assert len(report.changed) == 1
        assert any(fc.field == "manufacturer" and fc.new == "Bunn-O-Matic Corporation" for fc in report.changed[0].field_changes)
        doc = _read_document(conn, 1)
        assert doc["manufacturer_name"] == "Bunn-O-Matic Corporation"


def test_manufacturer_preserved_when_overridden(test_env, monkeypatch):
    with get_conn() as conn:
        from app.config import get_settings
        storage_dir = get_settings().local_storage_dir_resolved
        storage_dir.mkdir(parents=True, exist_ok=True)
        _seed_document(conn, storage_dir, manufacturer_name="Human-Corrected Corp")
        _override(conn, 1, "manufacturer")
        _patch_extraction(monkeypatch, {"axiom.pdf": _meta(manufacturer="Bunn-O-Matic Corporation")})

        report = rm.reindex_documents(conn, storage_dir, ocr_available=False, apply=True)

        assert report.changed == []
        doc = _read_document(conn, 1)
        assert doc["manufacturer_name"] == "Human-Corrected Corp", "a human-corrected field must never be overwritten"


def test_doc_type_updates_when_not_overridden(test_env, monkeypatch):
    with get_conn() as conn:
        from app.config import get_settings
        storage_dir = get_settings().local_storage_dir_resolved
        storage_dir.mkdir(parents=True, exist_ok=True)
        _seed_document(conn, storage_dir, doc_type="unknown")
        _patch_extraction(monkeypatch, {"axiom.pdf": _meta(doc_type="service_repair")})

        rm.reindex_documents(conn, storage_dir, ocr_available=False, apply=True)

        assert _read_document(conn, 1)["doc_type"] == "service_repair"


def test_doc_type_preserved_when_overridden(test_env, monkeypatch):
    with get_conn() as conn:
        from app.config import get_settings
        storage_dir = get_settings().local_storage_dir_resolved
        storage_dir.mkdir(parents=True, exist_ok=True)
        _seed_document(conn, storage_dir, doc_type="parts")
        _override(conn, 1, "doc_type")
        _patch_extraction(monkeypatch, {"axiom.pdf": _meta(doc_type="service_repair")})

        rm.reindex_documents(conn, storage_dir, ocr_available=False, apply=True)

        assert _read_document(conn, 1)["doc_type"] == "parts"


def test_title_updates_when_not_overridden(test_env, monkeypatch):
    with get_conn() as conn:
        from app.config import get_settings
        storage_dir = get_settings().local_storage_dir_resolved
        storage_dir.mkdir(parents=True, exist_ok=True)
        _seed_document(conn, storage_dir, title="Untitled")
        _patch_extraction(monkeypatch, {"axiom.pdf": _meta(title="Axiom Service Manual")})

        rm.reindex_documents(conn, storage_dir, ocr_available=False, apply=True)

        assert _read_document(conn, 1)["title"] == "Axiom Service Manual"


def test_title_preserved_when_overridden(test_env, monkeypatch):
    with get_conn() as conn:
        from app.config import get_settings
        storage_dir = get_settings().local_storage_dir_resolved
        storage_dir.mkdir(parents=True, exist_ok=True)
        _seed_document(conn, storage_dir, title="Human-Corrected Title")
        _override(conn, 1, "title")
        _patch_extraction(monkeypatch, {"axiom.pdf": _meta(title="Axiom Service Manual")})

        rm.reindex_documents(conn, storage_dir, ocr_available=False, apply=True)

        assert _read_document(conn, 1)["title"] == "Human-Corrected Title"


def test_revision_updates_when_not_overridden(test_env, monkeypatch):
    with get_conn() as conn:
        from app.config import get_settings
        storage_dir = get_settings().local_storage_dir_resolved
        storage_dir.mkdir(parents=True, exist_ok=True)
        _seed_document(conn, storage_dir, revision=None)
        _patch_extraction(monkeypatch, {"axiom.pdf": _meta(revision="Rev C")})

        rm.reindex_documents(conn, storage_dir, ocr_available=False, apply=True)

        assert _read_document(conn, 1)["revision"] == "Rev C"


def test_revision_preserved_when_overridden(test_env, monkeypatch):
    with get_conn() as conn:
        from app.config import get_settings
        storage_dir = get_settings().local_storage_dir_resolved
        storage_dir.mkdir(parents=True, exist_ok=True)
        _seed_document(conn, storage_dir, revision="Rev A (confirmed by admin)")
        _override(conn, 1, "revision")
        _patch_extraction(monkeypatch, {"axiom.pdf": _meta(revision="Rev C")})

        rm.reindex_documents(conn, storage_dir, ocr_available=False, apply=True)

        assert _read_document(conn, 1)["revision"] == "Rev A (confirmed by admin)"


def test_doc_number_always_updates_even_when_every_other_field_is_overridden(test_env, monkeypatch):
    """doc_number has no override mechanism at all (not exposed by the admin
    correction endpoint) -- it must always refresh, independent of whatever
    else on the document a human has locked down."""
    with get_conn() as conn:
        from app.config import get_settings
        storage_dir = get_settings().local_storage_dir_resolved
        storage_dir.mkdir(parents=True, exist_ok=True)
        _seed_document(conn, storage_dir, manufacturer_name="Locked Corp", doc_type="parts",
                        title="Locked Title", revision="Locked Rev", doc_number="OLD-0001")
        for f in ("manufacturer", "doc_type", "title", "revision", "machine_links"):
            _override(conn, 1, f)
        _patch_extraction(monkeypatch, {"axiom.pdf": _meta(
            manufacturer="Bunn-O-Matic Corporation", doc_type="service_repair",
            title="Axiom Service Manual", revision="Rev C", doc_number="NEW-0002",
        )})

        report = rm.reindex_documents(conn, storage_dir, ocr_available=False, apply=True)

        doc = _read_document(conn, 1)
        assert doc["manufacturer_name"] == "Locked Corp"
        assert doc["doc_type"] == "parts"
        assert doc["title"] == "Locked Title"
        assert doc["revision"] == "Locked Rev"
        assert doc["doc_number"] == "NEW-0002", "doc_number must refresh even when every overridable field is locked"
        assert len(report.changed) == 1
        assert [fc.field for fc in report.changed[0].field_changes] == ["doc_number"]
        assert set(report.changed[0].skipped_overridden_fields) == {"manufacturer", "doc_type", "title", "revision", "machine_links"}


def test_machine_links_update_when_not_overridden(test_env, monkeypatch):
    with get_conn() as conn:
        from app.config import get_settings
        storage_dir = get_settings().local_storage_dir_resolved
        storage_dir.mkdir(parents=True, exist_ok=True)
        conn.execute("INSERT INTO manufacturers (name) VALUES ('Bunn-O-Matic Corporation')")
        _seed_document(conn, storage_dir)
        _patch_extraction(monkeypatch, {"axiom.pdf": _meta(machine_matches=[
            MachineMatch(manufacturer="Bunn-O-Matic Corporation", model_name="Axiom",
                         family="Axiom Series", machine_type="coffee brewer", confidence=0.9),
        ])})

        rm.reindex_documents(conn, storage_dir, ocr_available=False, apply=True)

        links = conn.execute(
            "SELECT m.model_name FROM document_machines dm JOIN machines m ON m.id = dm.machine_id "
            "WHERE dm.document_id = 1"
        ).fetchall()
        assert [r["model_name"] for r in links] == ["Axiom"]


def test_machine_links_preserved_when_overridden(test_env, monkeypatch):
    with get_conn() as conn:
        from app.config import get_settings
        storage_dir = get_settings().local_storage_dir_resolved
        storage_dir.mkdir(parents=True, exist_ok=True)
        conn.execute("INSERT INTO manufacturers (name) VALUES ('Bunn-O-Matic Corporation')")
        conn.execute("INSERT INTO machines (manufacturer_id, model_name) VALUES (1, 'Axiom')")
        _seed_document(conn, storage_dir)
        conn.execute(
            "INSERT INTO document_machines (document_id, machine_id, confidence, review_status) "
            "VALUES (1, 1, 1.0, 'approved')"
        )
        _override(conn, 1, "machine_links")
        _patch_extraction(monkeypatch, {"axiom.pdf": _meta(machine_matches=[])})

        rm.reindex_documents(conn, storage_dir, ocr_available=False, apply=True)

        links = conn.execute("SELECT machine_id FROM document_machines WHERE document_id = 1").fetchall()
        assert [r["machine_id"] for r in links] == [1], "an admin-approved link must survive even though extraction now finds nothing"


def test_stale_metadata_note_is_cleared_when_no_longer_produced(test_env, monkeypatch):
    with get_conn() as conn:
        from app.config import get_settings
        storage_dir = get_settings().local_storage_dir_resolved
        storage_dir.mkdir(parents=True, exist_ok=True)
        _seed_document(conn, storage_dir, status_reason="No machine model matched; needs admin review and catalog update.")
        _patch_extraction(monkeypatch, {"axiom.pdf": _meta(notes=[])})

        rm.reindex_documents(conn, storage_dir, ocr_available=False, apply=True)

        assert _read_document(conn, 1)["status_reason"] is None


def test_new_metadata_note_is_appended_without_duplicating_on_rerun(test_env, monkeypatch):
    with get_conn() as conn:
        from app.config import get_settings
        storage_dir = get_settings().local_storage_dir_resolved
        storage_dir.mkdir(parents=True, exist_ok=True)
        _seed_document(conn, storage_dir, status_reason=None)
        _patch_extraction(monkeypatch, {"axiom.pdf": _meta(
            notes=["Document type not confidently detected from title-page keywords."]
        )})

        rm.reindex_documents(conn, storage_dir, ocr_available=False, apply=True)
        assert _read_document(conn, 1)["status_reason"] == "Document type not confidently detected from title-page keywords."

        # Re-running with the same extraction result must not duplicate it.
        rm.reindex_documents(conn, storage_dir, ocr_available=False, apply=True)
        assert _read_document(conn, 1)["status_reason"] == "Document type not confidently detected from title-page keywords."


def test_non_metadata_status_reason_content_is_never_touched(test_env, monkeypatch):
    """status_reason is a single shared flat string -- an admin deactivation
    note or an ingestion-time extraction reason living alongside a stale
    metadata note must survive clearing that note, since the reconciliation
    only ever removes segments matching metadata extraction's own formats."""
    with get_conn() as conn:
        from app.config import get_settings
        storage_dir = get_settings().local_storage_dir_resolved
        storage_dir.mkdir(parents=True, exist_ok=True)
        _seed_document(
            conn, storage_dir,
            status_reason="OCR quality low on page 3 | No machine model matched; needs admin review and catalog update.",
        )
        _patch_extraction(monkeypatch, {"axiom.pdf": _meta(notes=[])})

        rm.reindex_documents(conn, storage_dir, ocr_available=False, apply=True)

        assert _read_document(conn, 1)["status_reason"] == "OCR quality low on page 3"


def test_dry_run_makes_no_database_changes(test_env, monkeypatch):
    from app.config import get_settings
    storage_dir = get_settings().local_storage_dir_resolved
    storage_dir.mkdir(parents=True, exist_ok=True)

    # Seed and commit in its own transaction first -- the dry run's own
    # rollback (below) must not also discard the seed data itself.
    with get_conn() as conn:
        _seed_document(conn, storage_dir, doc_type="unknown", status_reason="No machine model matched; needs admin review and catalog update.")

    _patch_extraction(monkeypatch, {"axiom.pdf": _meta(doc_type="service_repair", notes=[])})

    with get_conn() as conn:
        report = rm.reindex_documents(conn, storage_dir, ocr_available=False, apply=False)
        conn.rollback()

    assert len(report.changed) == 1  # still reported, just not written
    with get_conn() as conn:
        doc = _read_document(conn, 1)
    assert doc["doc_type"] == "unknown"
    assert doc["status_reason"] == "No machine model matched; needs admin review and catalog update."


def test_extraction_failure_is_recorded_and_does_not_abort_the_run(test_env, monkeypatch):
    with get_conn() as conn:
        from app.config import get_settings
        storage_dir = get_settings().local_storage_dir_resolved
        storage_dir.mkdir(parents=True, exist_ok=True)
        _seed_document(conn, storage_dir, doc_id=1, filename="broken.pdf")
        _seed_document(conn, storage_dir, doc_id=2, filename="fine.pdf")

        def fake_extract(path, ocr_available):
            if path.name == "broken.pdf":
                raise ValueError("corrupt PDF")
            return "pdf", SimpleNamespace(pages=[]), None

        monkeypatch.setattr(rm, "extract", fake_extract)
        monkeypatch.setattr(rm, "extract_metadata", lambda filename, extracted: _meta(doc_type="service_repair"))

        report = rm.reindex_documents(conn, storage_dir, ocr_available=False, apply=True)

    assert len(report.errors) == 1
    assert report.errors[0][0] == "broken.pdf"
    assert len(report.changed) == 1
    assert report.changed[0].filename == "fine.pdf", "one document's extraction failure must not block another's update"

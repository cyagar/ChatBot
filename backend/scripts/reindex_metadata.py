"""Re-run metadata extraction (manufacturer/doc_type/title/revision/doc_number
and machine links) over already-ingested documents, without re-chunking or
re-embedding.

Exists to apply fixes to app/ingestion/metadata.py's heuristics (e.g. the
accessory-context and filename-priority fix for wrong-machine links) to
documents that were ingested before that fix existed, without the cost of a
full re-index (re-extraction, re-chunking, re-embedding all documents).

Per-field, not per-document: a document
with a human-corrected machine_links override still gets its doc_number
refreshed and its stale notes cleared; a document with a corrected title
still gets its machine links re-synced. Never overwrites a field a human has
corrected via PATCH /api/admin/documents/{id} -- checked per field against
metadata_overrides, not per document. doc_number has no override mechanism
(the admin correction endpoint doesn't expose it), so it is always refreshed.

Stale "needs admin review" notes are removed from status_reason once the
extraction that produced them no longer does -- but ONLY entries that match
metadata extraction's own recognizable note formats (see
_is_metadata_extraction_note below), never other content sharing that field
(ingestion/extraction reasons, admin deactivation notes), since status_reason
is a single shared flat string with no per-source tagging.

Runs as one transaction: all per-field updates for all documents commit
together on success, or nothing does. A single bad document's extraction
failure is caught and skipped (recorded, not fatal) without writing anything
for that document; a genuine DB-level failure mid-run aborts and rolls back
the entire run rather than leaving any document partially updated.

Usage: python scripts/reindex_metadata.py [--apply]
Defaults to a dry run (prints what would change). Pass --apply to write.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings  # noqa: E402
from app.db import get_conn  # noqa: E402
from app.ingestion.extractors import extract  # noqa: E402
from app.ingestion.metadata import extract_metadata  # noqa: E402
from app.ingestion.pipeline import _get_or_create_machine, _get_or_create_manufacturer  # noqa: E402

# The overridable-field list lives inline as `simple_fields` below
# (manufacturer/doc_type/title/revision; machine_links has its own separate
# handling further down). doc_number is deliberately absent: there is no way
# for a human to override it via PATCH /api/admin/documents/{id}
# (app/api/routes_admin.py's MetadataCorrection), so it's always safe to
# refresh -- see the doc_number block below.

_STATIC_METADATA_NOTES = {
    "Manufacturer not confidently detected; needs admin review.",
    "No machine model matched; needs admin review and catalog update.",
    "Document type not confidently detected from title-page keywords.",
}
# Mirrors the per-machine-match note format in app/ingestion/metadata.py's
# extract_metadata -- kept as a regex so a note is recognized as "extraction-
# generated" (and therefore safe to clear once stale) regardless of which
# document or model name it names.
_MACHINE_MATCH_NOTE_RE = re.compile(
    r"^'.+' (?:also names a machine already confidently identified from the filename"
    r"|mentioned only in accessory/compatibility context), not linked automatically "
    r"— review before approving\.$"
)


def _is_metadata_extraction_note(segment: str) -> bool:
    return segment in _STATIC_METADATA_NOTES or bool(_MACHINE_MATCH_NOTE_RE.match(segment))


def _reconcile_notes(existing_status_reason: str | None, current_notes: list[str]) -> str | None:
    """Keep every existing segment that ISN'T a recognizable metadata-
    extraction note untouched (other ingestion/admin content), drop any
    metadata-extraction-shaped segment no longer reproduced by the current
    extraction (resolved), and append any current note not already present."""
    existing_parts = [p.strip() for p in (existing_status_reason or "").split(" | ") if p.strip()]
    kept = [p for p in existing_parts if not _is_metadata_extraction_note(p)]
    for note in current_notes:
        if note not in kept:
            kept.append(note)
    return " | ".join(kept) if kept else None


@dataclass
class FieldChange:
    field: str
    old: object
    new: object


@dataclass
class DocumentReindexResult:
    filename: str
    field_changes: list[FieldChange] = field(default_factory=list)
    skipped_overridden_fields: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass
class ReindexReport:
    scanned: int = 0
    changed: list[DocumentReindexResult] = field(default_factory=list)
    unreviewed_notes: list[tuple[str, list[str]]] = field(default_factory=list)
    errors: list[tuple[str, str]] = field(default_factory=list)


def reindex_documents(conn, storage_dir: Path, ocr_available: bool, apply: bool) -> ReindexReport:
    docs = conn.execute(
        "SELECT d.id, d.original_filename, d.storage_path, d.doc_type, d.title, d.revision, "
        "d.doc_number, d.status_reason, mf.name AS manufacturer_name "
        "FROM documents d LEFT JOIN manufacturers mf ON mf.id = d.manufacturer_id "
        "WHERE d.status IN ('indexed','partial') AND d.deactivated_at IS NULL ORDER BY d.id"
    ).fetchall()

    overridden_fields_by_doc: dict[int, set[str]] = {}
    for row in conn.execute("SELECT DISTINCT document_id, field FROM metadata_overrides"):
        overridden_fields_by_doc.setdefault(row["document_id"], set()).add(row["field"])

    report = ReindexReport()

    for doc in docs:
        report.scanned += 1
        overridden = overridden_fields_by_doc.get(doc["id"], set())

        path = storage_dir / doc["storage_path"]
        if not path.exists():
            report.errors.append((doc["original_filename"], f"stored file missing: {path}"))
            continue

        try:
            _file_type, extracted, _mismatch_note = extract(path, ocr_available=ocr_available)
        except Exception as e:  # noqa: BLE001 - report and continue, one bad file shouldn't abort the run
            report.errors.append((doc["original_filename"], repr(e)))
            continue

        # extract() returning normally with status="failed"/"unsupported"
        # (a corrupt or now-unreadable stored file, an extractor regression)
        # must be treated the same as a raised exception -- passing that
        # near-empty ExtractedDocument to extract_metadata() would produce
        # metadata (manufacturer/title/etc. all None or filename-derived
        # only) that looks like a genuine change from the document's real,
        # good existing values and overwrites them. Skip the document
        # entirely instead, write nothing for it.
        if extracted.status in ("failed", "unsupported"):
            report.errors.append(
                (doc["original_filename"], f"extraction status={extracted.status!r}: {extracted.reason}")
            )
            continue

        try:
            meta = extract_metadata(doc["original_filename"], extracted)
        except Exception as e:  # noqa: BLE001 - report and continue, one bad file shouldn't abort the run
            report.errors.append((doc["original_filename"], repr(e)))
            continue

        result = DocumentReindexResult(filename=doc["original_filename"])

        # --- manufacturer/doc_type/title/revision: skip whichever the ---
        # --- human already corrected, refresh the rest.               ---
        simple_fields = [
            ("manufacturer", doc["manufacturer_name"], meta.manufacturer),
            ("doc_type", doc["doc_type"], meta.doc_type),
            ("title", doc["title"], meta.title),
            ("revision", doc["revision"], meta.revision),
        ]
        for name, old_value, new_value in simple_fields:
            if name in overridden:
                result.skipped_overridden_fields.append(name)
                continue
            if old_value != new_value:
                result.field_changes.append(FieldChange(name, old_value, new_value))
                if apply:
                    if name == "manufacturer":
                        manu_id = _get_or_create_manufacturer(conn, new_value)
                        conn.execute("UPDATE documents SET manufacturer_id = %s WHERE id = %s", (manu_id, doc["id"]))
                    else:
                        conn.execute(f"UPDATE documents SET {name} = %s WHERE id = %s", (new_value, doc["id"]))

        # --- doc_number: no override mechanism exists, always refresh ---
        if doc["doc_number"] != meta.doc_number:
            result.field_changes.append(FieldChange("doc_number", doc["doc_number"], meta.doc_number))
            if apply:
                conn.execute("UPDATE documents SET doc_number = %s WHERE id = %s", (meta.doc_number, doc["id"]))

        # --- machine_links ---
        # Old links are identified by (manufacturer, model_name) together,
        # not model_name alone -- the same model name from two different
        # manufacturers is a real, expected catalog collision. Only a
        # still-pending (never human-reviewed) link may be added or removed
        # by this script; review_document_machine_link (routes_admin.py)
        # only ever updates review_status, never confidence, so an approved
        # link is routinely still <1.0 -- an approved/rejected link is a
        # human decision and survives regardless of what the current
        # extraction proposes.
        if "machine_links" in overridden:
            result.skipped_overridden_fields.append("machine_links")
        else:
            old_rows = conn.execute(
                "SELECT dm.machine_id, mf.name AS manufacturer, m.model_name, dm.review_status "
                "FROM document_machines dm JOIN machines m ON m.id = dm.machine_id "
                "LEFT JOIN manufacturers mf ON mf.id = m.manufacturer_id "
                "WHERE dm.document_id = %s",
                (doc["id"],),
            ).fetchall()
            old_by_key = {(r["manufacturer"], r["model_name"]): r for r in old_rows}
            old_pending_keys = {k for k, r in old_by_key.items() if r["review_status"] == "pending"}
            reviewed_keys = set(old_by_key) - old_pending_keys

            new_by_key = {(m.manufacturer, m.model_name): m for m in meta.machine_matches}
            new_keys = set(new_by_key)

            to_remove = old_pending_keys - new_keys
            # Never re-propose a link an admin already approved or rejected,
            # even if this run's extraction still finds it.
            to_add = new_keys - old_pending_keys - reviewed_keys

            if to_remove or to_add:
                result.field_changes.append(FieldChange(
                    "machine_links",
                    sorted(f"{manu} {model}" for manu, model in old_pending_keys),
                    sorted(f"{manu} {model}" for manu, model in new_keys),
                ))
                if apply:
                    for key in to_remove:
                        conn.execute(
                            "DELETE FROM document_machines WHERE document_id = %s AND machine_id = %s "
                            "AND review_status = 'pending'",
                            (doc["id"], old_by_key[key]["machine_id"]),
                        )
                    for key in to_add:
                        match = new_by_key[key]
                        mid = _get_or_create_machine(conn, match)
                        conn.execute(
                            "INSERT INTO document_machines (document_id, machine_id, confidence) "
                            "VALUES (%s, %s, %s) ON CONFLICT (document_id, machine_id) DO NOTHING",
                            (doc["id"], mid, match.confidence),
                        )

        # --- notes: append newly-flagged, clear resolved-stale ---
        reconciled = _reconcile_notes(doc["status_reason"], meta.notes)
        if reconciled != (doc["status_reason"] or None):
            if apply:
                conn.execute("UPDATE documents SET status_reason = %s WHERE id = %s", (reconciled, doc["id"]))

        if meta.notes:
            result.notes = meta.notes
            report.unreviewed_notes.append((doc["original_filename"], meta.notes))

        if result.field_changes:
            report.changed.append(result)

    return report


def main() -> None:
    apply = "--apply" in sys.argv
    settings = get_settings()
    storage_dir = settings.local_storage_dir_resolved
    ocr_available = bool(settings.tesseract_cmd)

    with get_conn() as conn:
        report = reindex_documents(conn, storage_dir, ocr_available, apply)
        if not apply:
            conn.rollback()

    mode = "APPLIED" if apply else "DRY RUN (pass --apply to write)"
    print(f"Mode: {mode}")
    print(f"Documents scanned: {report.scanned}")
    print(f"Documents with changed fields: {len(report.changed)}")
    for doc_result in report.changed:
        print(f"  {doc_result.filename}")
        for fc in doc_result.field_changes:
            print(f"    {fc.field}: was {fc.old!r} -> now {fc.new!r}")
        if doc_result.skipped_overridden_fields:
            print(f"    (skipped, human-overridden: {', '.join(doc_result.skipped_overridden_fields)})")
    print(f"\nDocuments flagged for admin review (unresolved candidates): {len(report.unreviewed_notes)}")
    for fname, notes in report.unreviewed_notes:
        print(f"  {fname}")
        for n in notes:
            print(f"    - {n}")
    if report.errors:
        print(f"\nErrors ({len(report.errors)}):")
        for fname, e in report.errors:
            print(f"  {fname}: {e}")


if __name__ == "__main__":
    main()

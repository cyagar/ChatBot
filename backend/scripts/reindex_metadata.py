"""Re-run metadata extraction (manufacturer/doc_type/title/revision/doc_number
and machine links) over already-ingested documents, without re-chunking or
re-embedding.

Exists to apply fixes to app/ingestion/metadata.py's heuristics (e.g. the
accessory-context and filename-priority fix for wrong-machine links) to
documents that were ingested before that fix existed, without the cost of a
full re-index (re-extraction, re-chunking, re-embedding all 71 documents).

Never touches a document a human has already corrected: any document with a
prior metadata_overrides row for 'machine_links' is left untouched, so this
is safe to re-run after every metadata.py change.

Usage: python scripts/reindex_metadata.py [--apply]
Defaults to a dry run (prints what would change). Pass --apply to write.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings  # noqa: E402
from app.db import get_conn  # noqa: E402
from app.ingestion.extractors import extract  # noqa: E402
from app.ingestion.metadata import extract_metadata  # noqa: E402
from app.ingestion.pipeline import _get_or_create_machine  # noqa: E402


def main() -> None:
    apply = "--apply" in sys.argv
    settings = get_settings()
    storage_dir = settings.local_storage_dir_resolved
    ocr_available = bool(settings.tesseract_cmd)

    changed: list[tuple[str, list[str], list[str]]] = []
    unreviewed_notes: list[tuple[str, list[str]]] = []
    errors: list[tuple[str, str]] = []

    with get_conn() as conn:
        docs = conn.execute(
            "SELECT id, original_filename, storage_path FROM documents "
            "WHERE status IN ('indexed','partial') AND deactivated_at IS NULL "
            "ORDER BY id"
        ).fetchall()

        overridden_doc_ids = {
            r["document_id"]
            for r in conn.execute(
                "SELECT DISTINCT document_id FROM metadata_overrides WHERE field = 'machine_links'"
            ).fetchall()
        }

        scanned = 0
        for doc in docs:
            if doc["id"] in overridden_doc_ids:
                continue
            scanned += 1

            path = storage_dir / doc["storage_path"]
            if not path.exists():
                errors.append((doc["original_filename"], f"stored file missing: {path}"))
                continue

            try:
                _file_type, extracted, _mismatch_note = extract(path, ocr_available=ocr_available)
                meta = extract_metadata(doc["original_filename"], extracted)
            except Exception as e:  # noqa: BLE001 - report and continue, one bad file shouldn't abort the run
                errors.append((doc["original_filename"], repr(e)))
                continue

            old_rows = conn.execute(
                "SELECT m.model_name FROM document_machines dm JOIN machines m ON m.id = dm.machine_id "
                "WHERE dm.document_id = ? AND dm.confidence < 1.0",
                (doc["id"],),
            ).fetchall()
            old_names = sorted(r["model_name"] for r in old_rows)
            new_names = sorted(m.model_name for m in meta.machine_matches)

            if old_names != new_names:
                changed.append((doc["original_filename"], old_names, new_names))
                if apply:
                    conn.execute(
                        "DELETE FROM document_machines WHERE document_id = ? AND confidence < 1.0",
                        (doc["id"],),
                    )
                    for match in meta.machine_matches:
                        mid = _get_or_create_machine(conn, match)
                        conn.execute(
                            "INSERT OR IGNORE INTO document_machines (document_id, machine_id, confidence) "
                            "VALUES (?, ?, ?)",
                            (doc["id"], mid, match.confidence),
                        )

            if meta.notes:
                unreviewed_notes.append((doc["original_filename"], meta.notes))
                if apply:
                    row = conn.execute(
                        "SELECT status_reason FROM documents WHERE id = ?", (doc["id"],)
                    ).fetchone()
                    existing = row["status_reason"] or ""
                    new_notes = [n for n in meta.notes if n not in existing]
                    if new_notes:
                        combined = " | ".join(p for p in [existing, *new_notes] if p)
                        conn.execute(
                            "UPDATE documents SET status_reason = ? WHERE id = ?", (combined, doc["id"])
                        )

        if not apply:
            conn.rollback()

    mode = "APPLIED" if apply else "DRY RUN (pass --apply to write)"
    print(f"Mode: {mode}")
    print(f"Documents scanned (excluding human-overridden): {scanned}")
    print(f"Documents with changed machine links: {len(changed)}")
    for fname, old, new in changed:
        print(f"  {fname}\n    was: {old or '(none)'}\n    now: {new or '(none)'}")
    print(f"\nDocuments flagged for admin review (unresolved candidates): {len(unreviewed_notes)}")
    for fname, notes in unreviewed_notes:
        print(f"  {fname}")
        for n in notes:
            print(f"    - {n}")
    if errors:
        print(f"\nErrors ({len(errors)}):")
        for fname, e in errors:
            print(f"  {fname}: {e}")


if __name__ == "__main__":
    main()

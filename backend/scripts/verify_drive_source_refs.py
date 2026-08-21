"""Audit: does every active google_drive document's source_ref still resolve
to a real Drive file, and does the DB's recorded sha256 still match its
current content?

Checked in per independent follow-up review P0-4: the 2026-08-21
local_directory -> google_drive source_ref remap (paired by sha256, with
exact-filename matching to break the 5 shared-hash collisions, and
deterministic-but-arbitrary pairing for the small remainder of genuinely
byte-identical content) was applied as a one-off interactive script against
the live DB, not a checked-in migration. That script and its output mapping
(gdrive_manifest.json / gdrive_remap_mapping.json) were not kept, so the
original pairing decisions are not reconstructible after the fact -- that's
a real, acknowledged gap; see docs/PRODUCTION_READINESS.md.

What IS reconstructible, and what this script provides, is a going-forward
audit: given the DB's current source_refs, confirm each one still names a
real Drive file with matching content. This is the tool a reviewer or admin
should run to independently verify the corpus is what it claims to be,
instead of taking a prior narrative claim on faith.

Usage (from backend/):
    py scripts/verify_drive_source_refs.py
Exit code 0 if every active google_drive document checks out, 1 otherwise.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings
from app.db import get_conn
from app.ingestion.sources import GoogleDriveSource


def main() -> int:
    settings = get_settings()
    source = GoogleDriveSource(
        folder_id=settings.google_drive_folder_id,
        service_account_path=settings.google_service_account_json_path_resolved,
        cache_dir=settings.gdrive_cache_dir_resolved,
    )

    print("Listing current Drive folder contents (downloads/hashes anything not already cached)...")
    drive_files = {f.source_ref: f for f in source.list_files()}
    print(f"Drive currently lists {len(drive_files)} downloadable file(s).\n")

    with get_conn() as conn:
        docs = conn.execute(
            "SELECT id, original_filename, source_ref, sha256, status FROM documents "
            "WHERE source_system = 'google_drive' AND deactivated_at IS NULL"
        ).fetchall()

    problems = []
    for d in docs:
        drive_file = drive_files.get(d["source_ref"])
        if drive_file is None:
            problems.append(
                f"MISSING   document {d['id']} ({d['original_filename']!r}): "
                f"source_ref {d['source_ref']!r} no longer appears in the Drive folder listing."
            )
            continue
        if drive_file.sha256 != d["sha256"]:
            problems.append(
                f"MISMATCH  document {d['id']} ({d['original_filename']!r}): "
                f"DB sha256={d['sha256'][:12]}... but Drive currently has "
                f"sha256={drive_file.sha256[:12]}... (content changed since last ingest -- "
                f"re-index to pick this up)."
            )
        elif drive_file.filename != d["original_filename"]:
            problems.append(
                f"RENAMED   document {d['id']}: DB filename {d['original_filename']!r} vs "
                f"Drive's current name {drive_file.filename!r} (same content, cosmetic only)."
            )

    print(f"Checked {len(docs)} active google_drive document(s).")
    if not problems:
        print("All active documents' source_refs resolve to matching Drive content. OK.")
        return 0

    print(f"\n{len(problems)} issue(s) found:")
    for p in problems:
        print(f"  {p}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

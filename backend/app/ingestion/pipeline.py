"""End-to-end ingestion pipeline.

Idempotent and resumable: a file whose sha256 already exists as an indexed
document is skipped (`skipped_unchanged`) rather than reprocessed, so an
interrupted run can simply be re-run. Chunks/embeddings for a document are
written in a single transaction per document, so a crash mid-document leaves no
half-indexed document behind.
"""

from __future__ import annotations

import json
import logging
import shutil
import threading
from dataclasses import dataclass, field
from pathlib import Path

import psycopg

from app.config import get_settings
from app.db import get_conn
from app.ingestion import dedup
from app.ingestion.chunking import CURRENT_CHUNKING_VERSION, chunk_document
from app.ingestion.extractors import CURRENT_EXTRACTION_VERSION, extract
from app.ingestion.metadata import extract_metadata
from app.ingestion.sources import DocumentSource, get_document_source

logger = logging.getLogger(__name__)

# Guards against two ingestion runs (e.g. an upload-triggered reindex and a
# manual "Run re-index now" click) racing through the module-level dedup
# caches and database writes at the same time (independent review concern
# #14). Process-local only -- cheap, and still worth keeping as the fast
# path for the common same-process case, but P1-14 (external review,
# 2026-09-21) pointed out it does nothing against a SECOND process (another
# gunicorn worker, or two app instances briefly overlapping during a
# rolling deploy) starting a concurrent run -- see _try_acquire_db_lock
# below for the cross-process guard that actually closes that gap.
_INGEST_LOCK = threading.Lock()

# Arbitrary fixed key identifying "an ingestion run is in progress" as a
# Postgres advisory lock -- any int64 works; this one has no other meaning.
_ADVISORY_LOCK_KEY = 851234001


def _try_acquire_db_lock() -> psycopg.Connection | None:
    """Session-scoped advisory lock, held for the whole run across every
    process/worker talking to this database -- not just this one. Must use
    the UNPOOLED connection: a PgBouncer transaction-mode connection (what
    get_conn() uses) can hand the underlying server connection to a
    different session between statements, silently dropping a session-scoped
    lock -- the same constraint documented on db.py's run_migrations. The
    returned connection must be kept open for the run's duration and closed
    via _release_db_lock in a finally block (closing it also releases the
    lock even if the explicit unlock is skipped, but doing both keeps the
    release deterministic and testable rather than relying on GC/close timing).
    Returns None if another session already holds it."""
    settings = get_settings()
    conn = psycopg.connect(settings.database_url_unpooled, autocommit=True)
    acquired = conn.execute("SELECT pg_try_advisory_lock(%s)", (_ADVISORY_LOCK_KEY,)).fetchone()[0]
    if not acquired:
        conn.close()
        return None
    return conn


def _release_db_lock(conn: psycopg.Connection) -> None:
    try:
        conn.execute("SELECT pg_advisory_unlock(%s)", (_ADVISORY_LOCK_KEY,))
    finally:
        conn.close()


def _record_lock_failure(run_id: int | None, trigger: str, detail: str) -> None:
    if run_id is None:
        return
    with get_conn() as conn:
        conn.execute(
            "UPDATE ingestion_runs SET status='failed', finished_at=now(), trigger=%s WHERE id = %s",
            (trigger, run_id),
        )
        _record_event(conn, run_id, "(run)", "failed", detail, None)


@dataclass
class FileOutcome:
    filename: str
    status: str          # indexed | duplicate | partial | failed | unsupported | skipped_unchanged | skipped
    detail: str | None
    document_id: int | None = None
    chunk_count: int = 0
    page_count: int | None = None
    manufacturer: str | None = None
    doc_type: str | None = None
    machines: list[str] = field(default_factory=list)


# Per-run caches. Reset at the start of each ingest_all() call.
_SHINGLE_CACHE: dict[int, set[str]] = {}
_NEAR_DUP_SCORES: list[tuple[str, int, float]] = []


@dataclass
class IngestionReport:
    run_id: int
    outcomes: list[FileOutcome] = field(default_factory=list)
    near_duplicate_scores: list[tuple[str, int, float]] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for o in self.outcomes:
            out[o.status] = out.get(o.status, 0) + 1
        return out


def _ocr_available() -> bool:
    return bool(get_settings().tesseract_cmd)


def _store_file(local_path: Path, sha256: str) -> str:
    """Copy the source file into object storage under a content-addressed name so
    the manual viewer can serve it later without touching the ingest directory."""
    settings = get_settings()
    dest_dir = settings.local_storage_dir_resolved
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{sha256[:16]}{local_path.suffix.lower()}"
    if not dest.exists():
        shutil.copy2(local_path, dest)
    return dest.name


def _get_or_create_manufacturer(conn, name: str | None) -> int | None:
    if not name:
        return None
    row = conn.execute("SELECT id FROM manufacturers WHERE name = %s", (name,)).fetchone()
    if row:
        return row["id"]
    cur = conn.execute("INSERT INTO manufacturers (name) VALUES (%s) RETURNING id", (name,))
    return cur.fetchone()["id"]


def _get_or_create_machine(conn, match) -> int:
    manu_id = _get_or_create_manufacturer(conn, match.manufacturer)
    row = conn.execute(
        "SELECT id FROM machines WHERE manufacturer_id = %s AND model_name = %s",
        (manu_id, match.model_name),
    ).fetchone()
    if row:
        return row["id"]
    cur = conn.execute(
        "INSERT INTO machines (manufacturer_id, model_name, family, machine_type, aliases) "
        "VALUES (%s, %s, %s, %s, %s) RETURNING id",
        (manu_id, match.model_name, match.family, match.machine_type, json.dumps([])),
    )
    return cur.fetchone()["id"]


def _document_full_text(conn, document_id: int) -> str:
    rows = conn.execute(
        "SELECT content FROM chunks WHERE document_id = %s ORDER BY ordinal", (document_id,)
    ).fetchall()
    return "\n".join(r["content"] for r in rows)


def ingest_all(
    source: DocumentSource | None = None, embed: bool = True, trigger: str = "manual",
    run_id: int | None = None,
) -> IngestionReport:
    """run_id: independent follow-up review 2026-08-24 P0-6: an admin's "run
    re-index now" click returns 202 before this function ever executes (it
    runs as a FastAPI BackgroundTask). If the process restarted in that gap
    -- before this function created its own ingestion_runs row -- a run the
    admin was told had started would leave no trace at all. routes_admin.py's
    trigger_reindex now creates that row synchronously, inside the request
    handler, before responding, and passes its id through here so this
    function updates that same row instead of creating a second one. The
    scheduler's own timer-triggered calls (trigger='scheduled') pass no
    run_id and keep creating their own row exactly as before -- there's no
    HTTP response for that path to race against."""
    if not _INGEST_LOCK.acquire(blocking=False):
        _record_lock_failure(run_id, trigger, "Could not start: another ingestion run was already in progress.")
        raise RuntimeError(
            "An ingestion run is already in progress. Wait for it to finish before starting another."
        )
    try:
        db_lock_conn = _try_acquire_db_lock()
        if db_lock_conn is None:
            _record_lock_failure(
                run_id, trigger,
                "Could not start: another ingestion run was already in progress on a different worker.",
            )
            raise RuntimeError(
                "An ingestion run is already in progress. Wait for it to finish before starting another."
            )
        try:
            return _ingest_all_locked(source, embed, trigger, run_id)
        finally:
            _release_db_lock(db_lock_conn)
    finally:
        _INGEST_LOCK.release()


def _ingest_all_locked(
    source: DocumentSource | None, embed: bool, trigger: str, run_id: int | None = None
) -> IngestionReport:
    settings = get_settings()
    source = source or get_document_source(settings)

    # Run row created BEFORE the source is listed (independent review P0-3):
    # listing a Google Drive folder does live auth + API calls and can fail
    # (bad credentials, revoked access, quota, network). If that happens
    # before any run row exists, the reindex endpoint returns 202 and the
    # admin UI shows nothing -- no evidence an ingestion was even attempted.
    # `trigger` ('manual' | 'scheduled', P1-4) records who started this run,
    # so an admin can see the scheduler is actually running rather than
    # taking it on faith.
    if run_id is None:
        with get_conn() as conn:
            cur = conn.execute(
                "INSERT INTO ingestion_runs (status, trigger) VALUES ('running', %s) RETURNING id", (trigger,)
            )
            run_id = cur.fetchone()["id"]

    report = IngestionReport(run_id=run_id)
    had_error = False

    try:
        files = source.list_files()

        # Items the source noticed but couldn't/wouldn't include (P1-3: "report
        # every skipped item") get the same visibility as every other outcome
        # -- an ingestion_events row and a FileOutcome -- instead of only ever
        # reaching a server log.
        for skipped in source.pop_skipped():
            # P1-05 (external review, 2026-09-21): a genuine failure reported
            # via pop_skipped() (e.g. a download that errored out) used to be
            # visually indistinguishable from an intentional skip (subfolder,
            # shortcut, oversized file) AND didn't affect the run's overall
            # status -- an all-failed-download run still finished
            # status='completed'. See SkippedFile.is_error.
            if skipped.is_error:
                had_error = True
            with get_conn() as conn:
                _record_event(conn, run_id, skipped.filename, "skipped", skipped.reason, None)
            report.outcomes.append(FileOutcome(skipped.filename, "skipped", skipped.reason))

        _SHINGLE_CACHE.clear()
        _NEAR_DUP_SCORES.clear()

        # Per-file isolation: one file raising (a corrupt PDF, an OCR crash,
        # ...) must not abort every other file in the run, and must not leave
        # the run stuck at status='running' forever (independent review
        # concern #14 -- this is exactly the gap it names).
        for sf in files:
            try:
                outcome = _ingest_one(run_id, source, sf)
            except Exception as e:
                had_error = True
                logger.exception("Ingestion failed for %s", sf.filename)
                with get_conn() as conn:
                    _record_event(conn, run_id, sf.filename, "failed", f"Unhandled error: {e}", None)
                outcome = FileOutcome(sf.filename, "failed", f"Unhandled error: {e}")
            # P1-05 (external review, 2026-09-21): a HANDLED extraction
            # failure (extract() returning status="failed" rather than
            # raising) never flipped had_error -- only an unhandled
            # exception did. An all-failed synthetic run (every file a
            # corrupt/unreadable PDF, none of them raising) finished
            # status='completed', identical to a clean run. "unsupported" is
            # deliberately NOT included here: it's an intentional, expected
            # classification (a file type this pipeline will never parse),
            # not a failure -- see test_unsupported_file_retried_after_....
            if outcome.status == "failed":
                had_error = True
            report.outcomes.append(outcome)

        report.near_duplicate_scores = list(_NEAR_DUP_SCORES)

        if embed:
            _embed_pending_chunks()
    except Exception as e:
        logger.exception("Ingestion run %s aborted", run_id)
        with get_conn() as conn:
            conn.execute(
                "UPDATE ingestion_runs SET status='failed', finished_at=now() WHERE id = %s",
                (run_id,),
            )
            _record_event(conn, run_id, "(run)", "failed", f"Ingestion run aborted: {e}", None)
        raise

    final_status = "completed_with_errors" if had_error else "completed"
    with get_conn() as conn:
        conn.execute(
            "UPDATE ingestion_runs SET status=%s, finished_at=now() WHERE id = %s",
            (final_status, run_id),
        )
    return report


def _record_event(conn, run_id: int, filename: str, event: str, detail: str | None, document_id: int | None):
    conn.execute(
        "INSERT INTO ingestion_events (run_id, document_id, original_filename, event, detail) "
        "VALUES (%s, %s, %s, %s, %s)",
        (run_id, document_id, filename, event, detail),
    )


def _ingest_one(run_id: int, source: DocumentSource, sf) -> FileOutcome:
    settings = get_settings()

    # --- Idempotency/resume: this exact source file was already processed. ---
    # Keyed on source_ref (not sha256): the same bytes arriving under a *different*
    # source_ref is a duplicate, not a resume, and must fall through to the
    # duplicate branch below so it lands in duplicate_matches.
    #
    # 'indexed'/'partial'/'duplicate' are stable outcomes for unchanged bytes:
    # nothing about re-running changes them, so they're skipped outright.
    # 'unsupported'/'failed' are NOT assumed stable, because the outcome can
    # depend on tooling that may have improved since the last attempt (OCR,
    # etc.) — those get re-extracted, and the existing row is only touched if
    # the result actually changes (see stable_retry_id below), so a permanently
    # unsupported file (e.g. .indd) doesn't accumulate a new row every run.
    # superseded_candidate_id: the currently-active row at this source_ref,
    # when the incoming bytes differ from it. Deliberately NOT deactivated
    # here, and NOT deactivated once extraction/chunking succeeds either
    # (independent review P0-2, both the original claim and the 2026-08-24
    # follow-up): retiring a working, *approved* manual as soon as its
    # replacement merely parses is still not safe -- the replacement's
    # review_status defaults to 'pending' (migration 0003), so a technician
    # could be left with zero approved manuals for this machine for however
    # long the replacement sits unreviewed, even though extraction/chunking
    # "succeeded". The old document is only deactivated once a human approves
    # the replacement -- see the cutover in routes_admin.py's review_document,
    # which deactivates whatever else is still active at this source_ref in
    # the same transaction as the approval. A rejected replacement therefore
    # never takes down the manual it was meant to replace.
    #
    # Because of this, more than one row can be simultaneously "active"
    # (deactivated_at IS NULL) at the same source_ref -- the still-approved
    # old one, and however many pending replacement attempts have piled up
    # since. The idempotency/resume check below always compares against the
    # most recently ingested one, not an arbitrary one, and matches on it by
    # id explicitly rather than assuming source_ref alone is unique.
    # needs_reprocessing (independent follow-up review 2026-08-24 P0-7):
    # unchanged bytes used to be skipped unconditionally -- so a document
    # extracted/chunked before a pipeline-logic fix shipped would never
    # receive it, silently, forever, since nothing ever re-examined it once
    # its content stopped changing. 'indexed'/'partial' rows now also compare
    # extraction_version/chunking_version against the code's current
    # versions; a mismatch is reported (not skipped_unchanged) so the gap is
    # visible instead of invisible. Not auto-reprocessed this run -- see
    # DocumentOut.needs_reprocessing in routes_admin.py and
    # docs/PRODUCTION_READINESS.md for what's built and what isn't.
    stable_retry_id: int | None = None
    superseded_candidate_id: int | None = None
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT id, status, sha256, extraction_version, chunking_version FROM documents "
            "WHERE source_ref = %s AND deactivated_at IS NULL ORDER BY ingested_at DESC, id DESC LIMIT 1",
            (sf.source_ref,),
        ).fetchone()

        if existing and existing["sha256"] == sf.sha256:
            stale_pipeline_version = (
                existing["status"] in ("indexed", "partial")
                and (existing["extraction_version"] != CURRENT_EXTRACTION_VERSION
                     or existing["chunking_version"] != CURRENT_CHUNKING_VERSION)
            )
            if existing["status"] in ("indexed", "partial", "duplicate") and not stale_pipeline_version:
                _record_event(conn, run_id, sf.filename, "skipped_unchanged",
                              f"Unchanged since document {existing['id']} was last processed "
                              f"(status={existing['status']}).", existing["id"])
                return FileOutcome(sf.filename, "skipped_unchanged",
                                   f"Unchanged since document {existing['id']} was last processed.",
                                   existing["id"])
            if stale_pipeline_version:
                detail = (
                    f"Content unchanged, but document {existing['id']} was extracted/chunked at an "
                    f"older pipeline version (extraction v{existing['extraction_version']}, "
                    f"chunking v{existing['chunking_version']} vs current v{CURRENT_EXTRACTION_VERSION}/"
                    f"v{CURRENT_CHUNKING_VERSION}). Not reprocessed automatically this run."
                )
                _record_event(conn, run_id, sf.filename, "needs_reprocessing", detail, existing["id"])
                return FileOutcome(sf.filename, "needs_reprocessing", detail, existing["id"])
            stable_retry_id = existing["id"]
        elif existing:
            superseded_candidate_id = existing["id"]

    local_path = sf.local_path
    file_type, extracted, mismatch_note = extract(local_path, ocr_available=_ocr_available())

    # --- Unreadable / unsupported: still recorded, never silently dropped ---
    if extracted.status in ("unsupported", "failed"):
        with get_conn() as conn:
            storage_name = _store_file(local_path, sf.sha256)
            if stable_retry_id is not None:
                conn.execute(
                    "UPDATE documents SET status = %s, status_reason = %s, page_count = %s, "
                    "ingested_at = now() WHERE id = %s",
                    (extracted.status, extracted.reason, extracted.page_count or None, stable_retry_id),
                )
                doc_id = stable_retry_id
            elif superseded_candidate_id is not None:
                # The replacement failed validation -- record the attempt, but
                # insert it already deactivated so the still-good active row
                # at this source_ref is left untouched (P0-2).
                reason = (f"Replacement for document {superseded_candidate_id} failed validation "
                          f"and did not replace it: {extracted.reason}")
                cur = conn.execute(
                    "INSERT INTO documents (original_filename, storage_path, source_system, source_ref, "
                    "file_type, sha256, byte_size, page_count, status, status_reason, ingested_at, "
                    "deactivated_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now(), now()) "
                    "RETURNING id",
                    (sf.filename, storage_name, source.source_system, sf.source_ref, file_type,
                     sf.sha256, sf.byte_size, extracted.page_count or None,
                     extracted.status, reason),
                )
                doc_id = cur.fetchone()["id"]
                _record_event(conn, run_id, sf.filename, extracted.status, reason, doc_id)
                return FileOutcome(sf.filename, extracted.status, reason, doc_id,
                                   page_count=extracted.page_count or None)
            else:
                cur = conn.execute(
                    "INSERT INTO documents (original_filename, storage_path, source_system, source_ref, "
                    "file_type, sha256, byte_size, page_count, status, status_reason, ingested_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now()) RETURNING id",
                    (sf.filename, storage_name, source.source_system, sf.source_ref, file_type,
                     sf.sha256, sf.byte_size, extracted.page_count or None,
                     extracted.status, extracted.reason),
                )
                doc_id = cur.fetchone()["id"]
            _record_event(conn, run_id, sf.filename, extracted.status, extracted.reason, doc_id)
        return FileOutcome(sf.filename, extracted.status, extracted.reason, doc_id,
                           page_count=extracted.page_count or None)

    # Extraction succeeded where a prior attempt hadn't: retire the stale
    # unsupported/failed row now that a real (indexed/partial/duplicate) row
    # is about to be created below.
    if stable_retry_id is not None:
        with get_conn() as conn:
            conn.execute(
                "UPDATE documents SET deactivated_at = now(), "
                "status_reason = COALESCE(status_reason || ' | ', '') "
                "|| 'Superseded: re-processing succeeded where a prior attempt did not.' WHERE id = %s",
                (stable_retry_id,),
            )

    # Extraction succeeded, but the old document at this source_ref is
    # deliberately left active here -- see the comment above superseded_candidate_id.
    # It's only retired once a human approves this replacement (routes_admin.py's
    # review_document), not merely because extraction/chunking produced *a*
    # result; a pending or rejected replacement must never take down a manual
    # technicians can currently retrieve.

    # --- Exact duplicate of an already-stored file ---
    with get_conn() as conn:
        dup_row = conn.execute(
            "SELECT id, original_filename FROM documents WHERE sha256 = %s AND status IN ('indexed','partial') "
            "AND deactivated_at IS NULL LIMIT 1",
            (sf.sha256,),
        ).fetchone()
        if dup_row:
            storage_name = _store_file(local_path, sf.sha256)
            cur = conn.execute(
                "INSERT INTO documents (original_filename, storage_path, source_system, source_ref, "
                "file_type, sha256, byte_size, status, status_reason, duplicate_of, ingested_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, 'duplicate', %s, %s, now()) RETURNING id",
                (sf.filename, storage_name, source.source_system, sf.source_ref, file_type,
                 sf.sha256, sf.byte_size,
                 f"Byte-identical to '{dup_row['original_filename']}' (document {dup_row['id']}).",
                 dup_row["id"]),
            )
            doc_id = cur.fetchone()["id"]
            conn.execute(
                "INSERT INTO duplicate_matches (kept_document_id, duplicate_document_id, match_type, similarity) "
                "VALUES (%s, %s, 'exact_hash', 1.0)",
                (dup_row["id"], doc_id),
            )
            detail = f"Byte-identical to '{dup_row['original_filename']}'. Not indexed for retrieval."
            _record_event(conn, run_id, sf.filename, "duplicate", detail, doc_id)
        else:
            doc_id = None

    if doc_id is not None:
        return FileOutcome(sf.filename, "duplicate",
                           f"Byte-identical to '{dup_row['original_filename']}'.", doc_id)

    # --- Metadata + chunking ---
    meta = extract_metadata(sf.filename, extracted)
    chunks = chunk_document(extracted)

    if not chunks:
        with get_conn() as conn:
            storage_name = _store_file(local_path, sf.sha256)
            if superseded_candidate_id is not None:
                reason = (f"Replacement for document {superseded_candidate_id} produced no usable "
                          "chunks and did not replace it.")
                cur = conn.execute(
                    "INSERT INTO documents (original_filename, storage_path, source_system, source_ref, "
                    "file_type, sha256, byte_size, page_count, status, status_reason, ingested_at, "
                    "deactivated_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'failed', %s, now(), "
                    "now()) RETURNING id",
                    (sf.filename, storage_name, source.source_system, sf.source_ref, file_type,
                     sf.sha256, sf.byte_size, extracted.page_count, reason),
                )
            else:
                reason = "Text was extracted but produced no usable chunks."
                cur = conn.execute(
                    "INSERT INTO documents (original_filename, storage_path, source_system, source_ref, "
                    "file_type, sha256, byte_size, page_count, status, status_reason, ingested_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'failed', %s, now()) RETURNING id",
                    (sf.filename, storage_name, source.source_system, sf.source_ref, file_type,
                     sf.sha256, sf.byte_size, extracted.page_count, reason),
                )
            doc_id = cur.fetchone()["id"]
            _record_event(conn, run_id, sf.filename, "failed", reason, doc_id)
        return FileOutcome(sf.filename, "failed", reason, doc_id)

    candidate_text = "\n".join(c.content for c in chunks)

    with get_conn() as conn:
        # --- Near-duplicate check against already-indexed docs ---
        existing_docs = conn.execute(
            "SELECT id FROM documents WHERE status IN ('indexed','partial') AND deactivated_at IS NULL"
        ).fetchall()
        for r in existing_docs:
            if r["id"] not in _SHINGLE_CACHE:
                _SHINGLE_CACHE[r["id"]] = dedup.shingles(_document_full_text(conn, r["id"]))
        near, near_scores = dedup.find_near_duplicate_cached(
            candidate_text, {r["id"]: _SHINGLE_CACHE[r["id"]] for r in existing_docs}
        )
        _NEAR_DUP_SCORES.extend((sf.filename, did, s) for did, s in near_scores)

        storage_name = _store_file(local_path, sf.sha256)
        manu_id = _get_or_create_manufacturer(conn, meta.manufacturer)

        status = "indexed" if extracted.status == "ok" else "partial"
        status_reason_parts = []
        if extracted.reason:
            status_reason_parts.append(extracted.reason)
        if mismatch_note:
            status_reason_parts.append(mismatch_note)
        if meta.notes:
            status_reason_parts.extend(meta.notes)

        cur = conn.execute(
            "INSERT INTO documents (original_filename, storage_path, source_system, source_ref, "
            "file_type, sha256, byte_size, page_count, manufacturer_id, doc_type, title, revision, "
            "doc_number, status, status_reason, extraction_version, chunking_version, ingested_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now()) "
            "RETURNING id",
            (sf.filename, storage_name, source.source_system, sf.source_ref, file_type,
             sf.sha256, sf.byte_size, extracted.page_count, manu_id, meta.doc_type,
             meta.title, meta.revision, meta.doc_number, status,
             " | ".join(status_reason_parts) if status_reason_parts else None,
             CURRENT_EXTRACTION_VERSION, CURRENT_CHUNKING_VERSION),
        )
        doc_id = cur.fetchone()["id"]

        for match in meta.machine_matches:
            machine_id = _get_or_create_machine(conn, match)
            conn.execute(
                "INSERT INTO document_machines (document_id, machine_id, confidence) "
                "VALUES (%s, %s, %s) ON CONFLICT (document_id, machine_id) DO NOTHING",
                (doc_id, machine_id, match.confidence),
            )

        for ordinal, ch in enumerate(chunks):
            conn.execute(
                "INSERT INTO chunks (document_id, page_number, section_heading, chunk_type, "
                "content, char_count, ordinal) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (doc_id, ch.page_number, ch.section_heading, ch.chunk_type,
                 ch.content, len(ch.content), ordinal),
            )
        # No SQLite chunks_fts sync step needed -- chunks.content_tsv is a
        # Postgres GENERATED column, auto-maintained on every insert.

        if near:
            near_id, sim = near
            conn.execute(
                "INSERT INTO duplicate_matches (kept_document_id, duplicate_document_id, match_type, similarity) "
                "VALUES (%s, %s, 'near_duplicate_content', %s)",
                (near_id, doc_id, sim),
            )
            note = (f"Near-duplicate of document {near_id} (content similarity {sim:.2f}). "
                    "Both kept; revision comparison surfaces conflicts at answer time.")
            conn.execute(
                "UPDATE documents SET status_reason = COALESCE(status_reason || ' | ', '') || %s WHERE id = %s",
                (note, doc_id),
            )
            status_reason_parts.append(note)

        detail = " | ".join(status_reason_parts) if status_reason_parts else f"{len(chunks)} chunks indexed."
        _record_event(conn, run_id, sf.filename, status, detail, doc_id)

    _SHINGLE_CACHE[doc_id] = dedup.shingles(candidate_text)

    return FileOutcome(
        filename=sf.filename,
        status=status,
        detail=" | ".join(status_reason_parts) if status_reason_parts else None,
        document_id=doc_id,
        chunk_count=len(chunks),
        page_count=extracted.page_count,
        manufacturer=meta.manufacturer,
        doc_type=meta.doc_type,
        machines=[m.model_name for m in meta.machine_matches],
    )


def _embed_pending_chunks(batch_size: int = 64) -> int:
    """Embed every chunk with no embedding for the CURRENTLY configured model
    fingerprint (model name + revision -- see embedding_fingerprint's
    docstring, P1-15). Resumable: re-running only processes what's missing --
    which now includes a chunk whose only embedding row is from a since
    -changed model/revision, not just one with no row at all."""
    from app.retrieval.embeddings import embed_texts, embedding_fingerprint, vector_to_blob

    fingerprint = embedding_fingerprint()
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT c.id, c.content FROM chunks c "
            "LEFT JOIN embeddings e ON e.chunk_id = c.id AND e.model_name = %s "
            "WHERE e.chunk_id IS NULL",
            (fingerprint,),
        ).fetchall()

    if not rows:
        return 0

    total = 0
    for start in range(0, len(rows), batch_size):
        batch = rows[start : start + batch_size]
        vectors = embed_texts([r["content"] for r in batch])
        with get_conn() as conn:
            for row, vec in zip(batch, vectors):
                conn.execute(
                    # ON CONFLICT ... DO UPDATE (SQLite's INSERT OR REPLACE,
                    # ported) against embeddings' own chunk_id PRIMARY KEY --
                    # REPLACE semantics update in place rather than
                    # delete-then-insert, which matters here since nothing
                    # else references embeddings by a surrogate row id.
                    "INSERT INTO embeddings (chunk_id, model_name, dim, vector) "
                    "VALUES (%s, %s, %s, %s) "
                    "ON CONFLICT (chunk_id) DO UPDATE SET "
                    "model_name = EXCLUDED.model_name, dim = EXCLUDED.dim, vector = EXCLUDED.vector",
                    (row["id"], fingerprint, len(vec), vector_to_blob(vec)),
                )
        total += len(batch)
    return total

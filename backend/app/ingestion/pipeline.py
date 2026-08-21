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

from app.config import get_settings
from app.db import get_conn
from app.ingestion import dedup
from app.ingestion.chunking import chunk_document
from app.ingestion.extractors import extract
from app.ingestion.metadata import extract_metadata
from app.ingestion.sources import DocumentSource, get_document_source

logger = logging.getLogger(__name__)

# Guards against two ingestion runs (e.g. an upload-triggered reindex and a
# manual "Run re-index now" click) racing through the module-level dedup
# caches and database writes at the same time (independent review concern
# #14). Process-local only -- correct for the documented single-instance
# pilot deployment, not a multi-worker/multi-process one.
_INGEST_LOCK = threading.Lock()


@dataclass
class FileOutcome:
    filename: str
    status: str          # indexed | duplicate | partial | failed | unsupported | skipped_unchanged
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
    row = conn.execute("SELECT id FROM manufacturers WHERE name = ?", (name,)).fetchone()
    if row:
        return row["id"]
    cur = conn.execute("INSERT INTO manufacturers (name) VALUES (?)", (name,))
    return cur.lastrowid


def _get_or_create_machine(conn, match) -> int:
    manu_id = _get_or_create_manufacturer(conn, match.manufacturer)
    row = conn.execute(
        "SELECT id FROM machines WHERE manufacturer_id = ? AND model_name = ?",
        (manu_id, match.model_name),
    ).fetchone()
    if row:
        return row["id"]
    cur = conn.execute(
        "INSERT INTO machines (manufacturer_id, model_name, family, machine_type, aliases) "
        "VALUES (?, ?, ?, ?, ?)",
        (manu_id, match.model_name, match.family, match.machine_type, json.dumps([])),
    )
    return cur.lastrowid


def _document_full_text(conn, document_id: int) -> str:
    rows = conn.execute(
        "SELECT content FROM chunks WHERE document_id = ? ORDER BY ordinal", (document_id,)
    ).fetchall()
    return "\n".join(r["content"] for r in rows)


def ingest_all(source: DocumentSource | None = None, embed: bool = True) -> IngestionReport:
    if not _INGEST_LOCK.acquire(blocking=False):
        raise RuntimeError(
            "An ingestion run is already in progress. Wait for it to finish before starting another."
        )
    try:
        return _ingest_all_locked(source, embed)
    finally:
        _INGEST_LOCK.release()


def _ingest_all_locked(source: DocumentSource | None, embed: bool) -> IngestionReport:
    settings = get_settings()
    source = source or get_document_source(settings)

    # Run row created BEFORE the source is listed (independent review P0-3):
    # listing a Google Drive folder does live auth + API calls and can fail
    # (bad credentials, revoked access, quota, network). If that happens
    # before any run row exists, the trigger returns 202 and the admin UI
    # shows nothing -- no evidence an ingestion was even attempted.
    with get_conn() as conn:
        cur = conn.execute("INSERT INTO ingestion_runs (status) VALUES ('running')")
        run_id = cur.lastrowid

    report = IngestionReport(run_id=run_id)
    had_error = False

    try:
        files = source.list_files()

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
            report.outcomes.append(outcome)

        report.near_duplicate_scores = list(_NEAR_DUP_SCORES)

        if embed:
            _embed_pending_chunks()
    except Exception as e:
        logger.exception("Ingestion run %s aborted", run_id)
        with get_conn() as conn:
            conn.execute(
                "UPDATE ingestion_runs SET status='failed', finished_at=datetime('now') WHERE id = ?",
                (run_id,),
            )
            _record_event(conn, run_id, "(run)", "failed", f"Ingestion run aborted: {e}", None)
        raise

    final_status = "completed_with_errors" if had_error else "completed"
    with get_conn() as conn:
        conn.execute(
            "UPDATE ingestion_runs SET status=?, finished_at=datetime('now') WHERE id = ?",
            (final_status, run_id),
        )
    return report


def _record_event(conn, run_id: int, filename: str, event: str, detail: str | None, document_id: int | None):
    conn.execute(
        "INSERT INTO ingestion_events (run_id, document_id, original_filename, event, detail) "
        "VALUES (?, ?, ?, ?, ?)",
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
    # here -- independent review P0-2: retiring a working manual before its
    # replacement has been extracted and validated means a corrupt/unreadable
    # replacement can take down a manual technicians could otherwise still
    # use. It's only deactivated once a validated outcome (indexed/partial/
    # duplicate) actually exists to take its place, further down.
    stable_retry_id: int | None = None
    superseded_candidate_id: int | None = None
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT id, status, sha256 FROM documents WHERE source_ref = ? AND deactivated_at IS NULL",
            (sf.source_ref,),
        ).fetchone()

        if existing and existing["sha256"] == sf.sha256:
            if existing["status"] in ("indexed", "partial", "duplicate"):
                _record_event(conn, run_id, sf.filename, "skipped_unchanged",
                              f"Unchanged since document {existing['id']} was last processed "
                              f"(status={existing['status']}).", existing["id"])
                return FileOutcome(sf.filename, "skipped_unchanged",
                                   f"Unchanged since document {existing['id']} was last processed.",
                                   existing["id"])
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
                    "UPDATE documents SET status = ?, status_reason = ?, page_count = ?, "
                    "ingested_at = datetime('now') WHERE id = ?",
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
                    "deactivated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))",
                    (sf.filename, storage_name, source.source_system, sf.source_ref, file_type,
                     sf.sha256, sf.byte_size, extracted.page_count or None,
                     extracted.status, reason),
                )
                doc_id = cur.lastrowid
                _record_event(conn, run_id, sf.filename, extracted.status, reason, doc_id)
                return FileOutcome(sf.filename, extracted.status, reason, doc_id,
                                   page_count=extracted.page_count or None)
            else:
                cur = conn.execute(
                    "INSERT INTO documents (original_filename, storage_path, source_system, source_ref, "
                    "file_type, sha256, byte_size, page_count, status, status_reason, ingested_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))",
                    (sf.filename, storage_name, source.source_system, sf.source_ref, file_type,
                     sf.sha256, sf.byte_size, extracted.page_count or None,
                     extracted.status, extracted.reason),
                )
                doc_id = cur.lastrowid
            _record_event(conn, run_id, sf.filename, extracted.status, extracted.reason, doc_id)
        return FileOutcome(sf.filename, extracted.status, extracted.reason, doc_id,
                           page_count=extracted.page_count or None)

    # Extraction succeeded where a prior attempt hadn't: retire the stale
    # unsupported/failed row now that a real (indexed/partial/duplicate) row
    # is about to be created below.
    if stable_retry_id is not None:
        with get_conn() as conn:
            conn.execute(
                "UPDATE documents SET deactivated_at = datetime('now'), "
                "status_reason = COALESCE(status_reason || ' | ', '') "
                "|| 'Superseded: re-processing succeeded where a prior attempt did not.' WHERE id = ?",
                (stable_retry_id,),
            )

    # Extraction succeeded and produced usable content: the replacement is now
    # validated, so it's safe to retire the row it's superseding (P0-2 -- this
    # only runs once we know there's a real replacement to take its place, not
    # before).
    if superseded_candidate_id is not None:
        with get_conn() as conn:
            conn.execute(
                "UPDATE documents SET deactivated_at = datetime('now'), "
                "status_reason = COALESCE(status_reason || ' | ', '') "
                "|| 'Superseded: content changed at this source path.' WHERE id = ?",
                (superseded_candidate_id,),
            )

    # --- Exact duplicate of an already-stored file ---
    with get_conn() as conn:
        dup_row = conn.execute(
            "SELECT id, original_filename FROM documents WHERE sha256 = ? AND status IN ('indexed','partial') "
            "AND deactivated_at IS NULL LIMIT 1",
            (sf.sha256,),
        ).fetchone()
        if dup_row:
            storage_name = _store_file(local_path, sf.sha256)
            cur = conn.execute(
                "INSERT INTO documents (original_filename, storage_path, source_system, source_ref, "
                "file_type, sha256, byte_size, status, status_reason, duplicate_of, ingested_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'duplicate', ?, ?, datetime('now'))",
                (sf.filename, storage_name, source.source_system, sf.source_ref, file_type,
                 sf.sha256, sf.byte_size,
                 f"Byte-identical to '{dup_row['original_filename']}' (document {dup_row['id']}).",
                 dup_row["id"]),
            )
            doc_id = cur.lastrowid
            conn.execute(
                "INSERT INTO duplicate_matches (kept_document_id, duplicate_document_id, match_type, similarity) "
                "VALUES (?, ?, 'exact_hash', 1.0)",
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
                    "deactivated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'failed', ?, datetime('now'), "
                    "datetime('now'))",
                    (sf.filename, storage_name, source.source_system, sf.source_ref, file_type,
                     sf.sha256, sf.byte_size, extracted.page_count, reason),
                )
            else:
                reason = "Text was extracted but produced no usable chunks."
                cur = conn.execute(
                    "INSERT INTO documents (original_filename, storage_path, source_system, source_ref, "
                    "file_type, sha256, byte_size, page_count, status, status_reason, ingested_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'failed', ?, datetime('now'))",
                    (sf.filename, storage_name, source.source_system, sf.source_ref, file_type,
                     sf.sha256, sf.byte_size, extracted.page_count, reason),
                )
            doc_id = cur.lastrowid
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
            "doc_number, status, status_reason, ingested_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))",
            (sf.filename, storage_name, source.source_system, sf.source_ref, file_type,
             sf.sha256, sf.byte_size, extracted.page_count, manu_id, meta.doc_type,
             meta.title, meta.revision, meta.doc_number, status,
             " | ".join(status_reason_parts) if status_reason_parts else None),
        )
        doc_id = cur.lastrowid

        for match in meta.machine_matches:
            machine_id = _get_or_create_machine(conn, match)
            conn.execute(
                "INSERT OR IGNORE INTO document_machines (document_id, machine_id, confidence) "
                "VALUES (?, ?, ?)",
                (doc_id, machine_id, match.confidence),
            )

        for ordinal, ch in enumerate(chunks):
            conn.execute(
                "INSERT INTO chunks (document_id, page_number, section_heading, chunk_type, "
                "content, char_count, ordinal) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (doc_id, ch.page_number, ch.section_heading, ch.chunk_type,
                 ch.content, len(ch.content), ordinal),
            )
        conn.execute(
            "INSERT INTO chunks_fts (rowid, content) "
            "SELECT id, content FROM chunks WHERE document_id = ?",
            (doc_id,),
        )

        if near:
            near_id, sim = near
            conn.execute(
                "INSERT INTO duplicate_matches (kept_document_id, duplicate_document_id, match_type, similarity) "
                "VALUES (?, ?, 'near_duplicate_content', ?)",
                (near_id, doc_id, sim),
            )
            note = (f"Near-duplicate of document {near_id} (content similarity {sim:.2f}). "
                    "Both kept; revision comparison surfaces conflicts at answer time.")
            conn.execute(
                "UPDATE documents SET status_reason = COALESCE(status_reason || ' | ', '') || ? WHERE id = ?",
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
    """Embed every chunk that has no embedding yet. Resumable: re-running only
    processes what's missing."""
    from app.retrieval.embeddings import embed_texts, get_model, vector_to_blob

    with get_conn() as conn:
        rows = conn.execute(
            "SELECT c.id, c.content FROM chunks c "
            "LEFT JOIN embeddings e ON e.chunk_id = c.id WHERE e.chunk_id IS NULL"
        ).fetchall()

    if not rows:
        return 0

    model_name = get_settings().embedding_model
    total = 0
    for start in range(0, len(rows), batch_size):
        batch = rows[start : start + batch_size]
        vectors = embed_texts([r["content"] for r in batch])
        with get_conn() as conn:
            for row, vec in zip(batch, vectors):
                conn.execute(
                    "INSERT OR REPLACE INTO embeddings (chunk_id, model_name, dim, vector) "
                    "VALUES (?, ?, ?, ?)",
                    (row["id"], model_name, len(vec), vector_to_blob(vec)),
                )
        total += len(batch)
    return total

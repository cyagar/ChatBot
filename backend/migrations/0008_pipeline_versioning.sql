-- Independent follow-up review 2026-08-24 P0-7 (bounded slice): "unchanged
-- manuals don't receive new pipeline logic." A document whose bytes (sha256)
-- never change is always skipped by _ingest_one's idempotency check,
-- regardless of whether extraction.py/chunking.py's logic has changed since
-- it was last processed -- a parsing/chunking fix ships and silently never
-- reaches any already-ingested document. These columns record which
-- pipeline-stage version actually produced a document's current
-- chunks/extraction, so a mismatch against the code's current version can be
-- detected (surfaced via DocumentOut.needs_reprocessing) instead of being
-- invisible. Default 1 -- today's code is version 1 for both stages, so this
-- migration does not retroactively flag the existing corpus as stale.
ALTER TABLE documents ADD COLUMN extraction_version INTEGER NOT NULL DEFAULT 1;
ALTER TABLE documents ADD COLUMN chunking_version INTEGER NOT NULL DEFAULT 1;

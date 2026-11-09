-- extract_pdf (app/ingestion/extractors.py) can append warnings (e.g. a
-- per-page table-extraction failure) to ExtractedDocument.warnings while
-- still returning status="ok"/reason=None -- status_reason, built from
-- `reason` alone, never carried them, so they reached nowhere an admin could
-- ever see. This column lets pipeline.py persist them distinctly from
-- status_reason, which stays reserved for the document's own ok/partial/
-- failed/unsupported classification.
ALTER TABLE documents ADD COLUMN extraction_warnings TEXT;

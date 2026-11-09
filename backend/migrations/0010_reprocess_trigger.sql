-- An admin-triggered reprocess of a single already-indexed document (see
-- app.ingestion.pipeline.reprocess_document) is recorded as its own
-- ingestion_runs row, distinct from a full 'manual'/'scheduled' run over the
-- whole source, so the admin ingestion-runs view can tell them apart.
ALTER TABLE ingestion_runs DROP CONSTRAINT ingestion_runs_trigger_check;
ALTER TABLE ingestion_runs
    ADD CONSTRAINT ingestion_runs_trigger_check
        CHECK (trigger IN ('manual', 'scheduled', 'reprocess'));

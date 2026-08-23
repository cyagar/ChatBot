-- P1-4 (independent follow-up review): "Corpus freshness depends on an admin
-- remembering to reindex... Add a safe scheduled/push-triggered sync... and
-- visible last-success timestamp." Runs can now be started by the new
-- time-based scheduler as well as by an admin clicking "Run re-index now" --
-- this records which one started a given run, so an admin can see the
-- scheduler is actually running rather than taking it on faith.
ALTER TABLE ingestion_runs ADD COLUMN trigger TEXT NOT NULL DEFAULT 'manual'
    CHECK (trigger IN ('manual', 'scheduled'));

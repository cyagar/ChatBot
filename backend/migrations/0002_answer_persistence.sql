-- Independent review concern #7: a reload must show exactly what the technician
-- originally saw (selected citations, safety warnings, conflict note), not every
-- retrieved passage with the safety context silently dropped. These columns let
-- an assistant message store its own validated snapshot instead of the route
-- reconstructing something different on every read.
ALTER TABLE messages ADD COLUMN machine_id INTEGER REFERENCES machines(id);
ALTER TABLE messages ADD COLUMN safety_warnings TEXT;   -- JSON array, NULL = none
ALTER TABLE messages ADD COLUMN conflict_note TEXT;
ALTER TABLE messages ADD COLUMN provider TEXT;
ALTER TABLE messages ADD COLUMN answer_status TEXT NOT NULL DEFAULT 'completed';
    -- pending | completed | failed -- see concern #9 (observable answer states)

-- message_sources keeps every retrieved passage (retrieval-quality auditing,
-- already relied on elsewhere) but only rows with is_citation=1 are the ones
-- the provider actually selected and the technician actually saw. `excerpt` is
-- the exact text shown at generation time -- read from `chunks` on reload
-- instead would silently change what a saved answer displays if that chunk is
-- ever re-chunked/re-ingested differently.
ALTER TABLE message_sources ADD COLUMN is_citation INTEGER NOT NULL DEFAULT 0;
ALTER TABLE message_sources ADD COLUMN excerpt TEXT;

-- Consolidated Postgres schema, replacing the SQLite migration history
-- (0001_init through 0011_saved_answers_unique). The Postgres database
-- starts empty (owner decision, 2026-08-26: no data carried over from
-- SQLite -- Google Drive is the source of truth for documents, and no other
-- data was worth preserving), so this is the *final* schema shape only, not
-- a replay of every incremental historical change. Future schema changes
-- are new numbered migrations from here.
--
-- Dialect notes vs. the old SQLite schema:
--   - `INTEGER PRIMARY KEY` -> `INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY`
--   - `REAL` -> `DOUBLE PRECISION` (SQLite REAL is 8-byte, same as Postgres double precision)
--   - `BLOB` -> `BYTEA`
--   - `TEXT ... DEFAULT (datetime('now'))` -> `TIMESTAMPTZ ... DEFAULT now()`
--   - 0/1 flag columns -> native `BOOLEAN`
--   - the `chunks_fts` FTS5 virtual table -> a generated `tsvector` column
--     plus a GIN index directly on `chunks`, auto-maintained by Postgres
--     (no more manual `INSERT INTO chunks_fts ...` sync step after chunking)

CREATE TABLE manufacturers (
    id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name TEXT NOT NULL UNIQUE
);

CREATE TABLE users (
    id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'technician',  -- technician | administrator
    display_name TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_login_at TIMESTAMPTZ,
    is_disabled BOOLEAN NOT NULL DEFAULT false,
    disabled_at TIMESTAMPTZ,
    token_version INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE machines (
    id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    manufacturer_id INTEGER NOT NULL REFERENCES manufacturers(id),
    model_name TEXT NOT NULL,
    family TEXT,                 -- e.g. "Infusion Series", "Conveyor Dishmachines"
    machine_type TEXT,           -- e.g. "coffee brewer", "glasswasher", "espresso machine"
    aliases TEXT,                -- JSON array of alternate names/model codes
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (manufacturer_id, model_name)
);

CREATE TABLE documents (
    id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    original_filename TEXT NOT NULL,
    storage_path TEXT NOT NULL,       -- path under STORAGE_BACKEND
    source_system TEXT NOT NULL,      -- 'local_directory' | 'google_drive'
    source_ref TEXT,                  -- e.g. Drive file id, for incremental sync
    file_type TEXT NOT NULL,          -- pdf | doc | docx | image | indd
    sha256 TEXT NOT NULL,
    byte_size INTEGER NOT NULL,
    page_count INTEGER,

    -- Metadata (auto-detected, admin-correctable via metadata_overrides)
    manufacturer_id INTEGER REFERENCES manufacturers(id),
    doc_type TEXT,                    -- service_repair | installation_operating | parts | programming | spec_sheet | training | brochure | use_and_care | unknown
    title TEXT,
    revision TEXT,
    doc_number TEXT,                  -- e.g. "58039.0002 D 04/26"

    -- Lifecycle
    status TEXT NOT NULL,             -- indexed | duplicate | partial | failed | unsupported | pending
    status_reason TEXT,
    is_current_revision BOOLEAN NOT NULL DEFAULT true,
    superseded_by INTEGER REFERENCES documents(id),
    duplicate_of INTEGER REFERENCES documents(id),

    ingested_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    deactivated_at TIMESTAMPTZ,

    review_status TEXT NOT NULL DEFAULT 'pending',
    reviewed_by INTEGER REFERENCES users(id),
    reviewed_at TIMESTAMPTZ,
    review_note TEXT,
    extraction_version INTEGER NOT NULL DEFAULT 1,
    chunking_version INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE chunks (
    id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    page_number INTEGER,
    section_heading TEXT,
    chunk_type TEXT NOT NULL,         -- text | table | procedure | error_code | warning | spec
    content TEXT NOT NULL,
    content_tsv TSVECTOR GENERATED ALWAYS AS (to_tsvector('english', content)) STORED,
    char_count INTEGER NOT NULL,
    ordinal INTEGER NOT NULL          -- position within document, for stable resumable ingestion
);

-- Vector embeddings: one row per chunk. Brute-force cosine at query time in
-- Python (numpy), same as before -- pgvector is enabled on this database
-- (see docs/OWNER_DECISION_GATE.md section 3) but deliberately not used for
-- ANN search here -- that would be a retrieval-behavior change nobody asked
-- for, not a storage-engine port.
CREATE TABLE embeddings (
    chunk_id INTEGER PRIMARY KEY REFERENCES chunks(id) ON DELETE CASCADE,
    model_name TEXT NOT NULL,
    dim INTEGER NOT NULL,
    vector BYTEA NOT NULL             -- float32 little-endian, length = dim*4 bytes
);

CREATE TABLE document_machines (
    document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    machine_id INTEGER NOT NULL REFERENCES machines(id) ON DELETE CASCADE,
    confidence DOUBLE PRECISION NOT NULL DEFAULT 1.0,
    review_status TEXT NOT NULL DEFAULT 'pending',
    reviewed_by INTEGER REFERENCES users(id),
    reviewed_at TIMESTAMPTZ,
    PRIMARY KEY (document_id, machine_id)
);

CREATE TABLE duplicate_matches (
    id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    kept_document_id INTEGER NOT NULL REFERENCES documents(id),
    duplicate_document_id INTEGER NOT NULL REFERENCES documents(id),
    match_type TEXT NOT NULL,         -- exact_hash | near_duplicate_title | near_duplicate_content
    similarity DOUBLE PRECISION,
    detected_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE conversations (
    id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    machine_id INTEGER REFERENCES machines(id),
    title TEXT,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    pending_message_id INTEGER  -- FK added below, after messages exists (circular reference)
);

CREATE TABLE messages (
    id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role TEXT NOT NULL,               -- user | assistant | system
    content TEXT NOT NULL,
    is_clarifying_question BOOLEAN NOT NULL DEFAULT false,
    is_no_answer BOOLEAN NOT NULL DEFAULT false,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    machine_id INTEGER REFERENCES machines(id),
    safety_warnings TEXT,
    conflict_note TEXT,
    provider TEXT,
    answer_status TEXT NOT NULL DEFAULT 'completed',
    resolved_query TEXT,
    clarifying_options TEXT,
    retry_count INTEGER NOT NULL DEFAULT 0,
    idempotency_key TEXT
);

ALTER TABLE conversations
    ADD CONSTRAINT fk_conversations_pending_message
    FOREIGN KEY (pending_message_id) REFERENCES messages(id);

CREATE TABLE message_sources (
    message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    chunk_id INTEGER NOT NULL REFERENCES chunks(id),
    rank INTEGER NOT NULL,
    lexical_score DOUBLE PRECISION,
    vector_score DOUBLE PRECISION,
    combined_score DOUBLE PRECISION,
    is_citation BOOLEAN NOT NULL DEFAULT false,
    excerpt TEXT,
    citation_ordinal INTEGER,
    PRIMARY KEY (message_id, chunk_id)
);

CREATE TABLE feedback (
    id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id),
    rating TEXT NOT NULL,             -- helpful | incorrect | missing_info
    comment TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE saved_answers (
    id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    saved_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE recent_machines (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    machine_id INTEGER NOT NULL REFERENCES machines(id) ON DELETE CASCADE,
    last_used_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    is_favorite BOOLEAN NOT NULL DEFAULT false,
    PRIMARY KEY (user_id, machine_id)
);

CREATE TABLE invitations (
    id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    token_hash TEXT NOT NULL UNIQUE,  -- raw token is shown once at creation, never stored
    email TEXT NOT NULL,              -- registration must match this email exactly
    role TEXT NOT NULL DEFAULT 'technician',
    created_by INTEGER NOT NULL REFERENCES users(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    used_at TIMESTAMPTZ,
    used_by INTEGER REFERENCES users(id),
    revoked_at TIMESTAMPTZ
);

CREATE TABLE audit_events (
    id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    actor_user_id INTEGER REFERENCES users(id),
    event_type TEXT NOT NULL,  -- admin_bootstrap | invite_created | invite_used |
                                -- user_disabled | user_enabled | document_reviewed |
                                -- document_machine_reviewed
    target_type TEXT,
    target_id INTEGER,
    detail TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE ingestion_runs (
    id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    status TEXT NOT NULL DEFAULT 'running',  -- running | completed | completed_with_errors | failed
    trigger TEXT NOT NULL DEFAULT 'manual'
        CHECK (trigger IN ('manual', 'scheduled'))
);

CREATE TABLE ingestion_events (
    id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES ingestion_runs(id) ON DELETE CASCADE,
    document_id INTEGER REFERENCES documents(id),
    original_filename TEXT NOT NULL,
    event TEXT NOT NULL,  -- indexed | duplicate | partial | failed | unsupported | skipped_unchanged
    detail TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE metadata_overrides (
    id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    field TEXT NOT NULL,              -- manufacturer | doc_type | title | revision | machine_links
    previous_value TEXT,
    corrected_value TEXT NOT NULL,
    corrected_by TEXT NOT NULL,
    reason TEXT,
    corrected_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_chunks_document ON chunks(document_id);
CREATE INDEX idx_chunks_type ON chunks(chunk_type);
CREATE INDEX idx_chunks_content_tsv ON chunks USING GIN (content_tsv);

CREATE INDEX idx_conversations_user ON conversations(user_id);

CREATE INDEX idx_document_machines_machine ON document_machines(machine_id);

CREATE INDEX idx_documents_manufacturer ON documents(manufacturer_id);
CREATE INDEX idx_documents_sha256 ON documents(sha256);
CREATE INDEX idx_documents_status ON documents(status);

CREATE INDEX idx_ingestion_events_run ON ingestion_events(run_id);

CREATE INDEX idx_invitations_email ON invitations(email);

CREATE INDEX idx_machines_manufacturer ON machines(manufacturer_id);

CREATE INDEX idx_messages_conversation ON messages(conversation_id);

CREATE UNIQUE INDEX idx_messages_conversation_idempotency_key
    ON messages(conversation_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;

CREATE UNIQUE INDEX idx_saved_answers_user_message
    ON saved_answers(user_id, message_id);

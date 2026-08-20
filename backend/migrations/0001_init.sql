-- Technician Manual Assistant — initial schema
-- SQLite substitute for the plan's preferred PostgreSQL+pgvector, chosen because this
-- development machine has no Docker/Postgres available. See docs/ARCHITECTURE.md
-- "Storage & retrieval substitution" for the migration path back to Postgres+pgvector.
-- Vectors are stored as raw float32 BLOBs and searched with brute-force cosine
-- similarity in Python (backend/app/retrieval/vector_store.py) — fast enough at the
-- current corpus size (tens of documents / low thousands of chunks).

PRAGMA foreign_keys = ON;

-- schema_migrations is created by app/db.py before any migration runs.

-- ---------------------------------------------------------------------------
-- Machine catalog
-- ---------------------------------------------------------------------------

CREATE TABLE manufacturers (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE
);

CREATE TABLE machines (
    id INTEGER PRIMARY KEY,
    manufacturer_id INTEGER NOT NULL REFERENCES manufacturers(id),
    model_name TEXT NOT NULL,
    family TEXT,                 -- e.g. "Infusion Series", "Conveyor Dishmachines"
    machine_type TEXT,           -- e.g. "coffee brewer", "glasswasher", "espresso machine"
    aliases TEXT,                -- JSON array of alternate names/model codes
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (manufacturer_id, model_name)
);

CREATE INDEX idx_machines_manufacturer ON machines(manufacturer_id);

-- ---------------------------------------------------------------------------
-- Documents
-- ---------------------------------------------------------------------------

CREATE TABLE documents (
    id INTEGER PRIMARY KEY,
    original_filename TEXT NOT NULL,
    storage_path TEXT NOT NULL,       -- path under STORAGE_BACKEND
    source_system TEXT NOT NULL,      -- 'local_directory' | 'google_drive'
    source_ref TEXT,                  -- e.g. Drive file id, for future incremental sync
    file_type TEXT NOT NULL,          -- pdf | doc | docx | image | indd
    sha256 TEXT NOT NULL,
    byte_size INTEGER NOT NULL,
    page_count INTEGER,

    -- Metadata (auto-detected; admin-correctable via metadata_overrides)
    manufacturer_id INTEGER REFERENCES manufacturers(id),
    doc_type TEXT,                    -- service_repair | installation_operating | parts | programming | spec_sheet | training | brochure | use_and_care | unknown
    title TEXT,
    revision TEXT,
    doc_number TEXT,                  -- e.g. "58039.0002 D 04/26"

    -- Lifecycle
    status TEXT NOT NULL,             -- indexed | duplicate | partial | failed | unsupported | pending
    status_reason TEXT,
    is_current_revision INTEGER NOT NULL DEFAULT 1,
    superseded_by INTEGER REFERENCES documents(id),
    duplicate_of INTEGER REFERENCES documents(id),

    ingested_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    deactivated_at TEXT
);

CREATE INDEX idx_documents_sha256 ON documents(sha256);
CREATE INDEX idx_documents_status ON documents(status);
CREATE INDEX idx_documents_manufacturer ON documents(manufacturer_id);

CREATE TABLE document_machines (
    document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    machine_id INTEGER NOT NULL REFERENCES machines(id) ON DELETE CASCADE,
    confidence REAL NOT NULL DEFAULT 1.0,
    PRIMARY KEY (document_id, machine_id)
);

CREATE INDEX idx_document_machines_machine ON document_machines(machine_id);

-- Admin corrections to auto-detected metadata (kept as an audit trail rather than
-- overwriting in place, per "Review document metadata and correct ..." requirement).
CREATE TABLE metadata_overrides (
    id INTEGER PRIMARY KEY,
    document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    field TEXT NOT NULL,              -- manufacturer | doc_type | title | revision | machine_links
    previous_value TEXT,
    corrected_value TEXT NOT NULL,
    corrected_by TEXT NOT NULL,
    reason TEXT,
    corrected_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ---------------------------------------------------------------------------
-- Chunks + retrieval indexes
-- ---------------------------------------------------------------------------

CREATE TABLE chunks (
    id INTEGER PRIMARY KEY,
    document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    page_number INTEGER,
    section_heading TEXT,
    chunk_type TEXT NOT NULL,         -- text | table | procedure | error_code | warning | spec
    content TEXT NOT NULL,
    char_count INTEGER NOT NULL,
    ordinal INTEGER NOT NULL          -- position within document, for stable resumable ingestion
);

CREATE INDEX idx_chunks_document ON chunks(document_id);
CREATE INDEX idx_chunks_type ON chunks(chunk_type);

-- Lexical (BM25-style) index via FTS5, external-content against chunks.
CREATE VIRTUAL TABLE chunks_fts USING fts5(
    content,
    content='chunks',
    content_rowid='id',
    tokenize='porter unicode61'
);

-- Vector embeddings: one row per chunk. Brute-force cosine at query time.
CREATE TABLE embeddings (
    chunk_id INTEGER PRIMARY KEY REFERENCES chunks(id) ON DELETE CASCADE,
    model_name TEXT NOT NULL,
    dim INTEGER NOT NULL,
    vector BLOB NOT NULL              -- float32 little-endian, length = dim*4 bytes
);

-- ---------------------------------------------------------------------------
-- Duplicate / near-duplicate tracking
-- ---------------------------------------------------------------------------

CREATE TABLE duplicate_matches (
    id INTEGER PRIMARY KEY,
    kept_document_id INTEGER NOT NULL REFERENCES documents(id),
    duplicate_document_id INTEGER NOT NULL REFERENCES documents(id),
    match_type TEXT NOT NULL,         -- exact_hash | near_duplicate_title | near_duplicate_content
    similarity REAL,
    detected_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ---------------------------------------------------------------------------
-- Ingestion runs (idempotent/resumable)
-- ---------------------------------------------------------------------------

CREATE TABLE ingestion_runs (
    id INTEGER PRIMARY KEY,
    started_at TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at TEXT,
    status TEXT NOT NULL DEFAULT 'running'   -- running | completed | failed
);

CREATE TABLE ingestion_events (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES ingestion_runs(id) ON DELETE CASCADE,
    document_id INTEGER REFERENCES documents(id),
    original_filename TEXT NOT NULL,
    event TEXT NOT NULL,              -- indexed | duplicate | partial | failed | unsupported | skipped_unchanged
    detail TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_ingestion_events_run ON ingestion_events(run_id);

-- ---------------------------------------------------------------------------
-- Users / auth
-- ---------------------------------------------------------------------------

CREATE TABLE users (
    id INTEGER PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'technician',  -- technician | administrator
    display_name TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_login_at TEXT
);

CREATE TABLE recent_machines (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    machine_id INTEGER NOT NULL REFERENCES machines(id) ON DELETE CASCADE,
    last_used_at TEXT NOT NULL DEFAULT (datetime('now')),
    is_favorite INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, machine_id)
);

-- ---------------------------------------------------------------------------
-- Conversations / messages / feedback
-- ---------------------------------------------------------------------------

CREATE TABLE conversations (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    machine_id INTEGER REFERENCES machines(id),
    title TEXT,
    started_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_conversations_user ON conversations(user_id);

CREATE TABLE messages (
    id INTEGER PRIMARY KEY,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role TEXT NOT NULL,               -- user | assistant | system
    content TEXT NOT NULL,
    is_clarifying_question INTEGER NOT NULL DEFAULT 0,
    is_no_answer INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_messages_conversation ON messages(conversation_id);

-- Logs which retrieved chunks backed a given assistant message (for citations
-- and for retrieval-quality auditing per the plan's logging requirement).
CREATE TABLE message_sources (
    message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    chunk_id INTEGER NOT NULL REFERENCES chunks(id),
    rank INTEGER NOT NULL,
    lexical_score REAL,
    vector_score REAL,
    combined_score REAL,
    PRIMARY KEY (message_id, chunk_id)
);

CREATE TABLE feedback (
    id INTEGER PRIMARY KEY,
    message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id),
    rating TEXT NOT NULL,             -- helpful | incorrect | missing_info
    comment TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE saved_answers (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    saved_at TEXT NOT NULL DEFAULT (datetime('now'))
);

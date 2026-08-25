-- Android Rewrite Plan sec 9 ("Requires an Idempotency-Key header ... A
-- duplicate key for the same user and payload returns the original result,
-- not another user message"), sec 11.1 ("idempotency-key records scoped to
-- user and operation"), sec 16/17 (release criterion / named risk: losing
-- connectivity or retrying must never create a duplicate question).
--
-- This is the SQLite-scoped version of that requirement against the current
-- backend -- the full outbox/durable-attempt design (sec 5.1/9) needs
-- PostgreSQL/a job queue and is deferred to that migration. This closes the
-- same duplicate-question hazard (a flaky-connection retry, or a double-tap
-- that slips past the client's disabled-button guard) without it.
--
-- Scoped to (conversation_id, idempotency_key) rather than a separate table
-- keyed on user_id: routes_chat.py's ask_question already resolves and
-- authorizes the conversation (_require_own_conversation) before this column
-- is ever read or written, so conversation-scoping is equivalent to
-- user-scoping here without an extra join. The partial index explicitly
-- excludes NULL keys (every message inserted before this migration, plus any
-- caller -- e.g. the existing PWA JS -- that doesn't send the header) so
-- old/legacy rows never collide with each other.
ALTER TABLE messages ADD COLUMN idempotency_key TEXT;

CREATE UNIQUE INDEX idx_messages_conversation_idempotency_key
    ON messages(conversation_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;

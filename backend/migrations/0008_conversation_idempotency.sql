-- A retried "start conversation" request returns the conversation it already
-- created instead of adding an empty one.
ALTER TABLE conversations ADD COLUMN idempotency_key TEXT;
CREATE UNIQUE INDEX conversations_user_idempotency_key
    ON conversations (user_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;

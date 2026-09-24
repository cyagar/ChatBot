-- Explicit link from an assistant message to the user message it answers.
ALTER TABLE messages ADD COLUMN reply_to_message_id INTEGER REFERENCES messages(id);

-- Rows written before this column existed are linked to the nearest preceding
-- user message in the same conversation.
UPDATE messages a
SET reply_to_message_id = (
    SELECT MAX(u.id) FROM messages u
    WHERE u.conversation_id = a.conversation_id AND u.role = 'user' AND u.id < a.id
)
WHERE a.role = 'assistant';

CREATE INDEX messages_reply_to_idx ON messages (reply_to_message_id);

-- A user turn has at most one real answer; a clarifying question and the
-- answer that later resumes it may share the same turn.
CREATE UNIQUE INDEX messages_one_answer_per_question
    ON messages (reply_to_message_id)
    WHERE role = 'assistant' AND NOT is_clarifying_question AND reply_to_message_id IS NOT NULL;

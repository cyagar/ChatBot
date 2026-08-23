-- P1-8 (independent follow-up review): "Resolve follow-ups into a stored
-- standalone retrieval query before hybrid_search... Confirming a machine
-- must resume the existing pending message [instead of inserting a
-- duplicate user turn]."

-- The retrieval query used for a follow-up, when it differs from the
-- question's original wording (content is never rewritten -- only what's
-- sent to hybrid_search is). NULL when resolution didn't change anything.
ALTER TABLE messages ADD COLUMN resolved_query TEXT;

-- Points at the user message a clarifying question is waiting on. Set when
-- a clarifying "which machine?" question is emitted; cleared once that
-- question is actually answered (machine confirmed) or once the user moves
-- on by asking something new instead.
ALTER TABLE conversations ADD COLUMN pending_message_id INTEGER REFERENCES messages(id);

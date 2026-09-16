-- Owner decision (2026-09-16): concurrent questions in one conversation are
-- not supported -- a second question must be rejected server-side while one
-- is still being answered, not merely disabled on the client. This column is
-- the claim flag: ask_question/retry_answer atomically flip it false->true
-- before doing any retrieval/provider work and flip it back in a finally, so
-- only one of those can be "in progress" for a given conversation at a time.
ALTER TABLE conversations ADD COLUMN is_processing BOOLEAN NOT NULL DEFAULT false;

-- The plain is_processing boolean (migration 0002) alone is only cleared by
-- a Python `finally` -- a killed worker, a lost DB connection during
-- release, or a process shutdown between claim and `finally` would leave it
-- true forever, rejecting every future question/retry with 409 with no way
-- out. These columns turn the boolean claim into a real lease:
-- processing_claimed_at records when the current
-- claim was taken, so a claim older than PROCESSING_LEASE_SECONDS
-- (app/api/routes_chat.py) can be reclaimed by a later request instead of
-- blocking forever. processing_attempt_id is a random fencing token
-- identifying WHICH attempt holds the lease, so a slow "zombie" worker whose
-- provider call finally returns after its lease already expired and was
-- reclaimed by someone else can be told, via its token no longer matching,
-- not to persist its answer -- otherwise a late write from the old attempt
-- could silently overwrite or duplicate the new attempt's real answer.
-- TEXT, not UUID: every attempt token in this codebase (see
-- app/main.py's correlation_id) is a plain str(uuid.uuid4()) compared as a
-- string, never adapted through a driver-level UUID type.
ALTER TABLE conversations ADD COLUMN processing_attempt_id TEXT;
ALTER TABLE conversations ADD COLUMN processing_claimed_at TIMESTAMPTZ;

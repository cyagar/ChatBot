-- P1-20 (external review, 2026-09-21): users.email/invitations.email are
-- TEXT with case-sensitive (or, for invitations, no) uniqueness, and every
-- write/read compared whatever case was typed -- "John@Example.com" and
-- "john@example.com" could become two separate accounts, and a user who
-- registered with mixed case could not log in with a lowercased address.
-- app/auth/security.py's normalize_email is now the single point every
-- registration/login/invitation-creation goes through -- this backfills
-- existing rows to match and adds a database-level backstop.
--
-- The backfill UPDATEs fail loudly (aborting this whole migration, same as
-- any other migration failure -- see run_migrations's per-migration
-- transaction) if two existing rows would collide once lowercased -- that
-- is a real pre-existing duplicate-identity account pair an operator must
-- resolve by hand (merge or rename one) before this can apply, not
-- something safe to silently resolve automatically by picking a winner.
UPDATE users SET email = lower(email) WHERE email <> lower(email);
UPDATE invitations SET email = lower(email) WHERE email <> lower(email);

-- Every write already normalizes to lowercase going forward, so this is a
-- backstop against a future write that does not (e.g. a script bypassing
-- normalize_email), not the primary enforcement mechanism.
CREATE UNIQUE INDEX users_email_lower_unique ON users (lower(email));

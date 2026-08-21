-- Independent follow-up review P0-5 (production enrollment still publicly
-- claimable) and P0-6 (Drive is a location, not an approval boundary).

-- --- P0-5: close public registration, allow account disable + session revocation ---

ALTER TABLE users ADD COLUMN is_disabled INTEGER NOT NULL DEFAULT 0;
ALTER TABLE users ADD COLUMN disabled_at TEXT;
-- Bumped to invalidate every session token issued before the bump (disable,
-- password reset, "sign out everywhere") -- sessions are stateless JWTs, so
-- this is the only way to revoke one before its natural expiry. Sourced only
-- from this column, never from settings, so it can't drift with an unrelated
-- config reload.
ALTER TABLE users ADD COLUMN token_version INTEGER NOT NULL DEFAULT 0;

-- Registration now requires an administrator-issued, single-use, expiring,
-- email-bound invitation -- there is no more "first HTTP registrant becomes
-- administrator" race. The very first administrator is created out-of-band by
-- scripts/bootstrap_admin.py, which refuses to run once any user exists.
CREATE TABLE invitations (
    id INTEGER PRIMARY KEY,
    token_hash TEXT NOT NULL UNIQUE,   -- raw token is shown once at creation, never stored
    email TEXT NOT NULL,               -- registration must match this email exactly
    role TEXT NOT NULL DEFAULT 'technician',
    created_by INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at TEXT NOT NULL,
    used_at TEXT,
    used_by INTEGER REFERENCES users(id),
    revoked_at TEXT
);

CREATE INDEX idx_invitations_email ON invitations(email);

-- Minimal audit trail for exactly the security-relevant actions named in
-- P0-5's required fix (account disable, invitations) and P0-6's (document/
-- link review decisions) -- not a general-purpose event log.
CREATE TABLE audit_events (
    id INTEGER PRIMARY KEY,
    actor_user_id INTEGER REFERENCES users(id),
    event_type TEXT NOT NULL,   -- admin_bootstrap | invite_created | invite_used |
                                 -- user_disabled | user_enabled | document_reviewed |
                                 -- document_machine_reviewed
    target_type TEXT,
    target_id INTEGER,
    detail TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- --- P0-6: documents and document-machine links need an approval boundary ---

ALTER TABLE documents ADD COLUMN review_status TEXT NOT NULL DEFAULT 'pending';
    -- pending | approved | rejected
ALTER TABLE documents ADD COLUMN reviewed_by INTEGER REFERENCES users(id);
ALTER TABLE documents ADD COLUMN reviewed_at TEXT;
ALTER TABLE documents ADD COLUMN review_note TEXT;

ALTER TABLE document_machines ADD COLUMN review_status TEXT NOT NULL DEFAULT 'pending';
ALTER TABLE document_machines ADD COLUMN reviewed_by INTEGER REFERENCES users(id);
ALTER TABLE document_machines ADD COLUMN reviewed_at TEXT;

-- Grandfather whatever already existed the moment this migration runs:
-- retrieval is about to start requiring review_status='approved' on both the
-- document and its document_machines link, and this corpus was already live
-- in front of the pilot. This is NOT a claim that each one was individually
-- re-reviewed (reviewed_by stays NULL, review_note says so explicitly) -- it
-- only prevents the entire existing corpus from silently going dark the
-- moment this migration applies. Everything ingested from this point on
-- defaults to 'pending' (see the ALTERs above) and must be explicitly
-- approved through the admin review queue before it is retrievable.
UPDATE documents SET
    review_status = 'approved',
    review_note = 'Grandfathered when the document/link approval workflow was introduced; not individually re-reviewed.'
WHERE review_status = 'pending';

UPDATE document_machines SET review_status = 'approved' WHERE review_status = 'pending';

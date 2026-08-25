-- Found via live tablet testing (2026-08-25): a ChatViewModel recreated
-- after the current user's request (app restart, leaving and re-entering a
-- conversation, or switching between the single-pane and two-pane adaptive
-- layouts) never knew a message had already been saved, so the "Save"
-- button reappeared as if untouched -- a re-tap silently inserted a second
-- saved_answers row for the same (user_id, message_id). Reproduced and
-- confirmed against the live pilot DB, then the duplicate rows were removed
-- by hand before this migration was written.
--
-- Unlike feedback (see that table's own comment/tests -- a technician
-- resubmitting a *different* rating after reconsidering is an intentional,
-- allowed case, so that table stays unconstrained), a duplicate save always
-- carries the same (user_id, message_id) with no new information. There is
-- no legitimate case for two rows, so this is a real UNIQUE constraint
-- rather than an append-only log.
--
-- Dedupe first: keep the earliest row (lowest id, i.e. the original save)
-- per (user_id, message_id) and drop any later duplicates, so this migration
-- doesn't fail on a pre-existing pilot DB that already has some.
DELETE FROM saved_answers
WHERE id NOT IN (
    SELECT MIN(id) FROM saved_answers GROUP BY user_id, message_id
);

CREATE UNIQUE INDEX idx_saved_answers_user_message
    ON saved_answers(user_id, message_id);

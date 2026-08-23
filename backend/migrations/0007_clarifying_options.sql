-- P1-7 (independent follow-up review): "Persist clarification candidates/
-- pending question... Require exact live-versus-reload equality." The live
-- POST /messages response for a clarifying question includes the specific
-- candidate machines found (clarifying_options), but that list was never
-- persisted -- only is_clarifying_question and the prompt text were. A
-- reload (GET /messages) therefore reproduced the clarifying bubble's TEXT
-- but not its tappable candidate buttons, silently downgrading to a generic
-- "choose a machine" fallback that -- unlike the specific-candidate buttons
-- -- does not resume this conversation's pending question at all.

ALTER TABLE messages ADD COLUMN clarifying_options TEXT;

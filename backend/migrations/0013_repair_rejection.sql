-- Records why the FIRST attempt needed a repair retry (NULL if none ran),
-- regardless of whether the repair succeeded, abstained, or also failed --
-- distinct from answer_reason, which only reflects the FINAL outcome and
-- cannot tell a model that abstained outright from one that only abstained
-- after a rejected first attempt. Admin-only: surfaced in /unanswered and
-- /feedback, never on MessageOut.
ALTER TABLE messages ADD COLUMN repair_rejection TEXT;
ALTER TABLE messages
    ADD CONSTRAINT messages_repair_rejection_check
        CHECK (repair_rejection IN ('schema', 'grounding', 'ordering', 'isolation_gate'));

-- Distinguishes WHY an assistant message carries is_no_answer/failed state,
-- so Android can show a next action that fits what actually happened
-- instead of one generic "no verified answer" heading for every cause.
-- NULL for a completed, substantive answer, or a clarifying question.
ALTER TABLE messages ADD COLUMN answer_reason TEXT;
ALTER TABLE messages
    ADD CONSTRAINT messages_answer_reason_check
        CHECK (answer_reason IN (
            'no_eligible_manual', 'insufficient_evidence', 'model_abstained',
            'validation_rejected', 'service_unavailable'
        ));

-- A technician reporting that the manual actually contains the answer the
-- app failed to surface is itself useful feedback, not just "helpful" or
-- "incorrect" -- which both presuppose an answer was given.
ALTER TABLE feedback DROP CONSTRAINT feedback_rating_check;
ALTER TABLE feedback
    ADD CONSTRAINT feedback_rating_check
        CHECK (rating IN ('helpful', 'incorrect', 'missing_info', 'missing_answer'));

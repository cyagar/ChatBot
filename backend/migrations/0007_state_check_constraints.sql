-- Allowed values for the state columns the application treats as closed sets, so
-- a script or manual edit cannot create a state the application considers
-- impossible.
ALTER TABLE users
    ADD CONSTRAINT users_role_check CHECK (role IN ('technician', 'administrator'));
ALTER TABLE messages
    ADD CONSTRAINT messages_role_check CHECK (role IN ('user', 'assistant')),
    ADD CONSTRAINT messages_answer_status_check
        CHECK (answer_status IN ('pending', 'completed', 'failed', 'retrying'));
ALTER TABLE documents
    ADD CONSTRAINT documents_review_status_check
        CHECK (review_status IN ('pending', 'approved', 'rejected')),
    ADD CONSTRAINT documents_status_check
        CHECK (status IN ('pending', 'indexed', 'partial', 'duplicate', 'failed', 'unsupported'));
ALTER TABLE document_machines
    ADD CONSTRAINT document_machines_review_status_check
        CHECK (review_status IN ('pending', 'approved', 'rejected'));
ALTER TABLE ingestion_runs
    ADD CONSTRAINT ingestion_runs_status_check
        CHECK (status IN ('running', 'completed', 'completed_with_errors', 'failed'));
ALTER TABLE feedback
    ADD CONSTRAINT feedback_rating_check CHECK (rating IN ('helpful', 'incorrect', 'missing_info'));

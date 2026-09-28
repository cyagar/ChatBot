-- More closed-set columns, so a script or manual edit cannot create a state the
-- application considers impossible (same intent as 0007_state_check_constraints.sql).
-- file_type and documents.source_system are deliberately NOT constrained here:
-- file_type is derived from magic-byte sniffing (app/ingestion/extractors.py)
-- against a set that grows whenever a new container format is recognized, not a
-- fixed application enum, and source_system carries ad-hoc values from whichever
-- DocumentSource ingested a document (including test-only sources) rather than a
-- small closed set -- constraining either here would risk rejecting a real,
-- correctly-classified value.
ALTER TABLE chunks
    ADD CONSTRAINT chunks_chunk_type_check
        CHECK (chunk_type IN ('text', 'table', 'procedure', 'error_code', 'warning', 'spec'));
ALTER TABLE documents
    ADD CONSTRAINT documents_doc_type_check
        CHECK (doc_type IN (
            'service_repair', 'parts', 'installation_operating', 'programming',
            'use_and_care', 'spec_sheet', 'training', 'brochure', 'unknown'
        ));
ALTER TABLE invitations
    ADD CONSTRAINT invitations_role_check CHECK (role IN ('technician', 'administrator'));

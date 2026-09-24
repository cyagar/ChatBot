-- At most one retrievable revision (active, approved, current) per source path.
-- Existing violations are resolved first by keeping the newest such document
-- and retiring the rest, so the unique index can be created on real data.
WITH ranked AS (
    SELECT id,
           first_value(id) OVER (PARTITION BY source_ref ORDER BY id DESC) AS keeper_id,
           row_number() OVER (PARTITION BY source_ref ORDER BY id DESC) AS rn
    FROM documents
    WHERE source_ref IS NOT NULL
      AND deactivated_at IS NULL
      AND review_status = 'approved'
      AND is_current_revision
)
UPDATE documents d
SET deactivated_at = now(),
    is_current_revision = false,
    superseded_by = r.keeper_id,
    status_reason = COALESCE(d.status_reason || ' | ', '')
        || 'Superseded: document ' || r.keeper_id::text || ' is the single current revision at this source path.'
FROM ranked r
WHERE d.id = r.id AND r.rn > 1;

-- A retired revision points at its replacement.
UPDATE documents d
SET superseded_by = keeper.id
FROM documents keeper
WHERE d.superseded_by IS NULL
  AND d.deactivated_at IS NOT NULL
  AND d.source_ref IS NOT NULL
  AND d.status_reason LIKE '%Superseded: document ' || keeper.id::text || ' was approved at this source path.%';

CREATE UNIQUE INDEX documents_one_current_per_source_ref
    ON documents (source_ref)
    WHERE source_ref IS NOT NULL
      AND deactivated_at IS NULL
      AND review_status = 'approved'
      AND is_current_revision;

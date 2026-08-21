-- P1-7 (independent follow-up review): "Order-preservingly deduplicate
-- citation IDs before both response and persistence. Require exact
-- live-versus-reload equality."
--
-- message_sources.rank is RETRIEVAL rank -- the order hybrid_search returned
-- passages in. Citations were persisted with that rank and reloaded with
-- "ORDER BY ms.rank", but the live response returned them in PROVIDER order
-- (the order the provider actually cited them, which for the claims/steps
-- contract is the order the claims appear in the answer). Those two orders
-- are not the same, so a reloaded conversation could show the same citations
-- in a different order than the technician originally saw -- the citation
-- numbering under an answer would not line up with the answer's own claims.
--
-- citation_ordinal stores the provider's citation order explicitly, leaving
-- rank free to keep meaning retrieval order for retrieval-quality auditing.
-- NULL for non-citation rows and for rows written before this migration.
ALTER TABLE message_sources ADD COLUMN citation_ordinal INTEGER;

-- Existing citation rows keep their historical order (rank order) rather than
-- being left NULL, so already-stored conversations still reload in a stable,
-- defined order instead of falling back to arbitrary row order.
UPDATE message_sources
SET citation_ordinal = rank
WHERE is_citation = 1 AND citation_ordinal IS NULL;

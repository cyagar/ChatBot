"""Regression coverage for a gap found by directly probing the live corpus with
bare part-number/error-code queries (see docs/PRODUCTION_READINESS.md): the
vector-only relevance gate (MIN_VECTOR_SIMILARITY_FOR_ANSWER) scores identifier
lookups below threshold even when a chunk contains the exact string verbatim,
because embeddings encode meaning, not identifiers. Real measured example: a
chunk containing the literal part number "81-118-31" scored only 0.62 cosine
similarity against the query "81-118-31" -- borderline against the gate --
despite a BM25 lexical score of 19.4, the highest measured all session.

`_code_token_rescue` fixes this narrowly: only an exact tokenized match (not a
substring match) rescues a passage, so a query like "E4" cannot be rescued by
an unrelated chunk whose entire content is the single letter "E"."""

from app.providers.extractive import ExtractiveProvider, _code_token_rescue, _extract_code_tokens
from app.retrieval.search import RetrievedChunk


def _chunk(chunk_id, content, vector_score=0.5) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=chunk_id, document_id=1, content=content,
        page_number=3, section_heading=None, chunk_type="table",
        original_filename="parts.pdf", title=None, doc_type="parts",
        revision=None, manufacturer="Dema", is_current_revision=True,
        lexical_score=10.0, vector_score=vector_score, combined_score=0.1,
    )


def test_extract_code_tokens_finds_part_numbers_and_error_codes():
    assert _extract_code_tokens("What is part 81-187-1?") == {"81-187-1"}
    assert _extract_code_tokens("E4") == {"E4"}
    assert _extract_code_tokens("is it safe to operate this") == set()


def test_exact_token_match_rescues_low_vector_score_passage():
    chunk = _chunk(1, "1 | 1 | 81-118-31 | CONTROL BOARD AND DISPLAY ASSY.", vector_score=0.62)
    rescued = _code_token_rescue("81-118-31", [chunk])
    assert rescued is chunk


def test_partial_substring_does_not_rescue():
    """A query token must match a whole token in the content, not just appear
    as a substring of some other token -- otherwise "E4" would be "rescued" by
    unrelated content that merely contains an "E" or a longer code like "E40"."""
    garbage = _chunk(2, "E", vector_score=0.75)
    assert _code_token_rescue("E4", [garbage]) is None

    different_code = _chunk(3, "See code E40 in the fault table.", vector_score=0.5)
    assert _code_token_rescue("E4", [different_code]) is None


def test_no_rescue_when_question_has_no_code_token():
    chunk = _chunk(4, "Some unrelated table content.", vector_score=0.3)
    assert _code_token_rescue("is this machine safe to operate?", [chunk]) is None


def test_provider_answers_exact_part_number_lookup_that_fails_the_vector_gate():
    """End-to-end: a passage scoring below MIN_VECTOR_SIMILARITY_FOR_ANSWER must
    still produce a real answer (not a no-answer refusal) when it contains the
    exact identifier asked about."""
    provider = ExtractiveProvider()
    passage = _chunk(5, "4 | 1 | 81-187-1 | MOUNTING BRACKET", vector_score=0.60)

    result = provider.generate("81-187-1", "Titan II", [passage])

    assert result.is_no_answer is False
    assert result.citations[0].chunk_id == 5


def test_provider_still_refuses_when_no_code_token_and_low_vector_score():
    provider = ExtractiveProvider()
    passage = _chunk(6, "Unrelated maintenance note about a different topic.", vector_score=0.3)

    result = provider.generate("is this machine safe to operate?", "Titan II", [passage])

    assert result.is_no_answer is True


def test_relevant_lower_ranked_passage_still_answers():
    """RRF can rank a passage first purely on lexical evidence, leaving its
    vector_score at 0 because it never appeared in the vector search's own
    candidate list -- found via a live survey where "what parts are needed
    for a routine service" had passages[0].vector_score == 0.0 even though a
    genuinely relevant passage sat a few ranks down at vector_score 0.727.
    Gating on passages[0] alone would wrongly refuse a clearly answerable
    question -- the provider must look across the top candidates, not just #1."""
    provider = ExtractiveProvider()
    lexical_winner = _chunk(7, "CAUTION: surfaces are hot.", vector_score=0.0)
    genuinely_relevant = _chunk(8, "Routine service requires part 90-100-2, the drip tray gasket.", vector_score=0.73)

    result = provider.generate(
        "what parts are needed for a routine service", "Axiom", [lexical_winner, genuinely_relevant]
    )

    assert result.is_no_answer is False
    # The displayed answer must be anchored on the passage that's actually
    # relevant, not whichever one happened to rank first pre-gate.
    assert result.citations[0].chunk_id == 8
    assert "drip tray gasket" in result.answer


def test_supporting_passages_exclude_reassigned_top_by_identity_not_position():
    """When `top` gets reassigned away from passages[0], the "Additional
    relevant passages" list must not re-include it just because it's no
    longer at position 0, and must not silently drop a genuinely relevant
    passage that happens to sit at position 0."""
    provider = ExtractiveProvider()
    lexical_winner = _chunk(7, "CAUTION: surfaces are hot.", vector_score=0.0)
    relevant_a = _chunk(8, "Part 90-100-2, the drip tray gasket.", vector_score=0.73)
    relevant_b = _chunk(9, "Part 90-100-3, the float switch.", vector_score=0.70)

    result = provider.generate(
        "what parts are needed for a routine service", "Axiom", [lexical_winner, relevant_a, relevant_b]
    )

    cited_ids = {c.chunk_id for c in result.citations}
    assert 8 in cited_ids and 9 in cited_ids
    assert "float switch" in result.answer  # relevant_b must appear in "Additional relevant passages"


def test_relevance_check_is_scoped_to_top_five_candidates():
    """The relevance check deliberately looks at passages[:5], not the whole
    list -- a relevant-looking passage ranked 6th or worse (i.e. retrieval
    already scored it as a weak match relative to five better candidates)
    should not rescue an otherwise off-topic top-ranked group."""
    provider = ExtractiveProvider()
    weak_candidates = [_chunk(i, f"unrelated content {i}", vector_score=0.1) for i in range(5)]
    relevant_but_far = _chunk(99, "Part 90-100-2, the drip tray gasket.", vector_score=0.9)

    result = provider.generate(
        "what parts are needed for a routine service", "Axiom", weak_candidates + [relevant_but_far]
    )

    assert result.is_no_answer is True

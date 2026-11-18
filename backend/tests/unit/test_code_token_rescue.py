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


def test_a_bare_number_does_not_rescue_an_unrelated_passage_sharing_that_number():
    """'How long does it take, 10 minutes?' must not be 'rescued' by a
    passage that happens to contain '10' for an entirely different reason
    (a step number here) -- a bare number is too ambiguous to treat as an
    identifier on its own."""
    chunk = _chunk(10, "Step 10: tighten the screw.", vector_score=0.3)
    assert _code_token_rescue("How long does it take, 10 minutes?", [chunk]) is None

    voltage_chunk = _chunk(11, "See page 120 for details.", vector_score=0.3)
    assert _code_token_rescue("Is 120 volts correct?", [voltage_chunk]) is None


def test_a_bare_number_with_explicit_intent_still_rescues_a_matching_error_code():
    """The bare-number ban isn't absolute: 'error 12' is explicit
    error-code intent, immediately adjacent to the number, and a passage
    that documents that exact code the same way must still rescue."""
    chunk = _chunk(12, "Error 12: low water level detected.", vector_score=0.3)
    assert _code_token_rescue("What does error 12 mean?", [chunk]) is chunk


def test_explicit_intent_on_one_side_only_is_not_enough():
    """The question naming 'error 12' does not license a passage that
    merely contains '12' for an unrelated reason, with no code-like
    context of its own -- both sides must independently confirm the
    number, or an error-code LOOKUP could rescue any passage that happens
    to share that number."""
    chunk = _chunk(13, "Replace the gasket every 12 months.", vector_score=0.3)
    assert _code_token_rescue("What does error 12 mean?", [chunk]) is None


def test_intent_requires_adjacency_not_mere_presence_in_the_question():
    """'number' appears in this question, but not immediately before the
    digit -- an intent WORD anywhere in a longer question must not license
    an unrelated number later in the same sentence."""
    chunk = _chunk(14, "See page 200 for the specification table.", vector_score=0.3)
    assert _code_token_rescue("What number do I set it to, 200?", [chunk]) is None


def test_a_passage_classified_as_an_error_code_table_confirms_its_own_bare_numbers():
    """The chunk's own chunk_type is itself the context -- a bare number
    inside a passage retrieval already classified as an error-code table
    doesn't need an adjacent intent phrase in its own text to confirm it."""
    chunk = _chunk(15, "12 - Low water level detected", vector_score=0.3)
    chunk.chunk_type = "error_code"
    assert _code_token_rescue("What does error 12 mean?", [chunk]) is chunk


def test_a_two_group_digit_range_does_not_rescue_without_explicit_intent():
    """'2-3' is shaped like a short part number but is at least as likely
    an ordinary duration -- the same ambiguity as a bare number, and the
    same fix applies."""
    chunk = _chunk(16, "Replace the filter every 2-3 months.", vector_score=0.3)
    assert _code_token_rescue("Should I run it for 2-3 minutes?", [chunk]) is None

    voltage_chunk = _chunk(22, "This model operates on 120-240 at 50/60Hz.", vector_score=0.3)
    assert _code_token_rescue("Is 120-240 volts correct?", [voltage_chunk]) is None


def test_a_hyphenated_letter_prefixed_code_still_rescues():
    """'E-04' is the hyphenated counterpart of 'E4' -- just as specific an
    identifier, and must rescue unconditionally the same way."""
    chunk = _chunk(17, "Error E-04 indicates a float switch fault.", vector_score=0.3)
    assert _code_token_rescue("What does Error E-04 mean?", [chunk]) is chunk


def test_a_standalone_hash_mark_confirms_a_catalog_part_number_with_no_other_intent_word():
    """A real corpus case: 'pump cover #082-6307' names its part number
    with a bare '#', not the word 'part' -- the number right after a '#'
    is its own, word-independent intent signal."""
    chunk = _chunk(18, "Standard, cork gasket, pump cover #082-6302 for closed impeller.", vector_score=0.3)
    assert _code_token_rescue("What is part #082-6302?", [chunk]) is chunk


def test_an_asymmetric_two_group_part_number_rescues_with_a_bare_question():
    """'082-6302' (short first group, long second group) is this corpus's
    dominant two-group part-number shape -- distinctive enough to rescue
    unconditionally, with no '#' or other intent marker needed on EITHER
    side, unlike an ordinary two-group range."""
    chunk = _chunk(20, "Standard, cork gasket, pump cover #082-6302 for closed impeller.", vector_score=0.3)
    assert _code_token_rescue("082-6302", [chunk]) is chunk


def test_an_asymmetric_two_group_part_number_with_no_marker_anywhere_still_rescues():
    """'285-6127' appears in the real corpus with no '#', no 'part', no
    adjacent intent word of any kind -- its own shape is distinctive
    enough that it needs none."""
    chunk = _chunk(21, "Diverter duct (285-6147 L-R, 285-6127 R-L) is located inside the hood.", vector_score=0.3)
    assert _code_token_rescue("What is 285-6127?", [chunk]) is chunk


def test_bare_part_with_nothing_after_it_is_not_an_intent_trigger():
    """'part' alone, not followed by 'number'/'no.'/'#', is ordinary prose
    ('part of the cycle') -- it must not license a bare number elsewhere
    in the same sentence as a lookup target."""
    chunk = _chunk(19, "The spin cycle runs for 10 minutes total.", vector_score=0.3)
    assert _code_token_rescue("What part of the cycle takes 10 minutes?", [chunk]) is None


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

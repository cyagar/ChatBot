"""parse_and_validate must check that cited excerpt *numbers* exist AND that
the material content (number, identifier, warning text) attributed to each
citation is actually present in it -- ID validation alone would let a model
cite a real excerpt while inventing the number or warning text it attributed
to that excerpt. These tests exercise that adversarial surface (fabricated
part, fabricated voltage, invented safety warning, invented conflict)
directly against parse_and_validate, plus one positive case proving a
genuinely-supported answer still passes.

These exercise parse_and_validate in isolation -- they do not call the real
Anthropic API (no key is configured in this environment; AI_PROVIDER stays
local_extractive for the whole suite, see docs/PRODUCTION_READINESS.md).
They prove the validation logic that provider depends on is sound, not that
a live model's output currently passes it.
"""
from __future__ import annotations

import json

import pytest

from app.providers.base import NO_ANSWER_TEXT, parse_and_validate
from app.retrieval.search import RetrievedChunk


def _passage(chunk_id, document_id, content, *, filename="manual.pdf", revision=None, current=True) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=chunk_id, document_id=document_id, content=content,
        page_number=1, section_heading=None, chunk_type="text",
        original_filename=filename, title="Manual", doc_type="service_repair",
        revision=revision, manufacturer="Bunn-O-Matic Corporation", is_current_revision=current,
        lexical_score=1.0, vector_score=1.0, combined_score=1.0,
    )


def test_fabricated_part_number_is_rejected():
    """Excerpt names part 81-118-31; the claim invents 99-000-00 instead."""
    passages = [_passage(1, 1, "Replace the inlet fitting, part number 81-118-31, during service.")]
    raw = json.dumps({
        "is_no_answer": False,
        "claims": [{"text": "Replace the inlet fitting, part number 99-000-00.", "cited_excerpt_numbers": [1]}],
        "steps": [], "warnings": [],
    })
    assert parse_and_validate(raw, passages, "test") is None


def test_fabricated_voltage_is_rejected():
    """Excerpt states 120V; the claim invents 240V instead."""
    passages = [_passage(1, 1, "The heating element operates at 120V and draws 8.5A under normal load.")]
    raw = json.dumps({
        "is_no_answer": False,
        "claims": [{"text": "The heating element operates at 240V.", "cited_excerpt_numbers": [1]}],
        "steps": [], "warnings": [],
    })
    assert parse_and_validate(raw, passages, "test") is None


def test_invented_safety_warning_is_rejected():
    """The cited excerpt contains no warning text at all -- the model invented one."""
    passages = [_passage(1, 1, "Remove the four screws on the access panel to expose the control board.")]
    raw = json.dumps({
        "is_no_answer": False,
        "claims": [{"text": "The access panel is held by four screws.", "cited_excerpt_numbers": [1]}],
        "steps": [],
        "warnings": [{"text": "WARNING: Disconnect all power before removing the access panel.", "cited_excerpt_numbers": [1]}],
    })
    assert parse_and_validate(raw, passages, "test") is None


def test_invented_conflict_is_structurally_impossible():
    """The provider JSON contract has no conflict_note field for the model to
    populate -- conflict_note is always server-computed from passage
    metadata (detect_conflict), so a model cannot invent one. A stray
    conflict_note key in the raw response is simply ignored."""
    passages = [_passage(1, 1, "Set the brew temperature to 200F.")]
    raw = json.dumps({
        "is_no_answer": False,
        "conflict_note": "These documents fundamentally disagree on the wiring diagram.",
        "claims": [{"text": "Set the brew temperature to 200F.", "cited_excerpt_numbers": [1]}],
        "steps": [], "warnings": [],
    })
    result = parse_and_validate(raw, passages, "test")
    assert result is not None
    assert result.conflict_note is None


def test_real_conflict_among_cited_passages_is_detected_independently():
    """Two documents, different revisions, one superseded -- detect_conflict
    must surface this from the passages actually cited, regardless of
    whether the model said anything about it."""
    passages = [
        _passage(1, 1, "Torque the fitting to 25 ft-lb.", filename="manual_v1.pdf", revision="A", current=False),
        _passage(2, 2, "Torque the fitting to 30 ft-lb.", filename="manual_v2.pdf", revision="B", current=True),
    ]
    raw = json.dumps({
        "is_no_answer": False,
        "claims": [
            {"text": "Torque the fitting to 25 ft-lb.", "cited_excerpt_numbers": [1]},
            {"text": "Torque the fitting to 30 ft-lb.", "cited_excerpt_numbers": [2]},
        ],
        "steps": [], "warnings": [],
    })
    result = parse_and_validate(raw, passages, "test")
    assert result is not None
    assert result.conflict_note is not None
    assert "manual_v1.pdf" in result.conflict_note and "manual_v2.pdf" in result.conflict_note


def test_genuinely_supported_answer_passes():
    """The positive case: numbers, identifiers, and warning text are all
    drawn verbatim from their cited excerpts -- validation must accept it,
    not just reject fabrications."""
    passages = [
        _passage(1, 1, "Error E4 indicates an open thermistor circuit. Replace sensor part 81-118-31."),
        _passage(2, 1, "WARNING: Disconnect power at the breaker before servicing the sensor."),
    ]
    raw = json.dumps({
        "is_no_answer": False,
        "claims": [
            {"text": "Error E4 indicates an open thermistor circuit.", "cited_excerpt_numbers": [1]},
            {"text": "Replace sensor part 81-118-31.", "cited_excerpt_numbers": [1]},
        ],
        "steps": [{"text": "Disconnect power at the breaker.", "cited_excerpt_numbers": [2]}],
        "warnings": [{"text": "WARNING: Disconnect power at the breaker before servicing the sensor.", "cited_excerpt_numbers": [2]}],
    })
    result = parse_and_validate(raw, passages, "test")
    assert result is not None
    assert result.safety_warnings == ["WARNING: Disconnect power at the breaker before servicing the sensor."]
    assert sorted(c.chunk_id for c in result.citations) == [1, 2]
    assert "E4" in result.answer
    assert "81-118-31" in result.answer


def test_each_claim_and_step_carries_an_inline_marker_for_its_own_citation():
    """Two claims cite two different excerpts -- each line's marker must
    point at that citation's own position in result.citations, not just
    list every citation on every line."""
    passages = [
        _passage(1, 1, "Error E4 indicates an open thermistor circuit."),
        _passage(2, 2, "The replacement part is 81-118-31.", filename="parts.pdf"),
    ]
    raw = json.dumps({
        "is_no_answer": False,
        "claims": [
            {"text": "Error E4 indicates an open thermistor circuit.", "cited_excerpt_numbers": [1]},
            {"text": "The replacement part is 81-118-31.", "cited_excerpt_numbers": [2]},
        ],
        "steps": [{"text": "Replacement part 81-118-31.", "cited_excerpt_numbers": [1, 2]}],
        "warnings": [],
    })
    result = parse_and_validate(raw, passages, "test")
    assert result is not None
    citation_index = {c.chunk_id: i + 1 for i, c in enumerate(result.citations)}
    lines = result.answer.split("\n")

    e4_line = next(ln for ln in lines if "E4" in ln)
    assert f"[{citation_index[1]}]" in e4_line
    assert f"[{citation_index[2]}]" not in e4_line

    part_line = next(ln for ln in lines if "81-118-31" in ln and ln.startswith("-"))
    assert f"[{citation_index[2]}]" in part_line
    assert f"[{citation_index[1]}]" not in part_line

    step_line = next(ln for ln in lines if ln.startswith("1."))
    assert f"[{citation_index[1]}]" in step_line and f"[{citation_index[2]}]" in step_line


def test_low_confidence_answer_surfaces_a_caveat_in_the_response():
    """A low-confidence answer must say so in the response text itself. Every
    claim/step/warning still passes the exact same verbatim-evidence check
    as a high-confidence answer -- confidence only changes the displayed
    text, never what's allowed to be asserted."""
    passages = [_passage(1, 1, "Error E4 indicates an open thermistor circuit.")]
    raw = json.dumps({
        "is_no_answer": False,
        "confidence": "low",
        "claims": [{"text": "Error E4 indicates an open thermistor circuit.", "cited_excerpt_numbers": [1]}],
        "steps": [], "warnings": [],
    })
    result = parse_and_validate(raw, passages, "test")
    assert result is not None
    assert "low confidence" in result.answer.lower()
    assert "E4" in result.answer


def test_high_or_missing_confidence_has_no_caveat():
    """Backward compatible: a response with no "confidence" field at all
    (every fixture above, and local_extractive which has no such concept)
    must not grow a caveat it never asked for."""
    passages = [_passage(1, 1, "Error E4 indicates an open thermistor circuit.")]
    raw = json.dumps({
        "is_no_answer": False,
        "claims": [{"text": "Error E4 indicates an open thermistor circuit.", "cited_excerpt_numbers": [1]}],
        "steps": [], "warnings": [],
    })
    result = parse_and_validate(raw, passages, "test")
    assert result is not None
    assert "low confidence" not in result.answer.lower()


def test_citation_to_nonexistent_excerpt_number_is_rejected():
    passages = [_passage(1, 1, "Only one excerpt exists.")]
    raw = json.dumps({
        "is_no_answer": False,
        "claims": [{"text": "Some claim.", "cited_excerpt_numbers": [1, 2]}],
        "steps": [], "warnings": [],
    })
    assert parse_and_validate(raw, passages, "test") is None


def test_no_answer_path_does_not_require_claims():
    passages = [_passage(1, 1, "Irrelevant excerpt.")]
    raw = json.dumps({
        "is_no_answer": True,
        "no_answer_explanation": "The excerpts don't cover this question.",
        "claims": [], "steps": [], "warnings": [],
    })
    result = parse_and_validate(raw, passages, "test")
    assert result is not None
    assert result.is_no_answer is True
    assert result.answer == NO_ANSWER_TEXT


def test_no_answer_explanation_with_fabricated_technical_content_is_never_displayed():
    """The model's no-answer prose has no excerpt to be checked against, so it
    is never shown: the technician sees the server's fixed text instead."""
    passages = [_passage(1, 1, "This manual does not cover high-voltage interlock procedures.")]
    raw = json.dumps({
        "is_no_answer": True,
        "no_answer_explanation": "You can bypass the interlock at 600V to proceed.",
        "claims": [], "steps": [], "warnings": [],
    })
    result = parse_and_validate(raw, passages, "test")
    assert result.is_no_answer is True
    assert result.answer == NO_ANSWER_TEXT
    assert "600V" not in result.answer


def test_no_answer_explanation_without_technical_content_still_passes():
    passages = [_passage(1, 1, "Irrelevant excerpt.")]
    raw = json.dumps({
        "is_no_answer": True,
        "no_answer_explanation": "None of the retrieved excerpts address this question. "
                                  "Try rephrasing, or check the troubleshooting section.",
        "claims": [], "steps": [], "warnings": [],
    })
    result = parse_and_validate(raw, passages, "test")
    assert result is not None
    assert result.is_no_answer is True


def test_no_answer_explanation_naming_the_machine_is_not_rejected():
    passages = [_passage(1, 1, "This manual covers routine maintenance only.")]
    raw = json.dumps({
        "is_no_answer": True,
        "no_answer_explanation": "The provided excerpts do not contain an Electrical "
                                  "Setup procedure for the Ultra-1/Ultra-2.",
        "claims": [], "steps": [], "warnings": [],
    })
    result = parse_and_validate(raw, passages, "test", machine_label="Ultra-1/Ultra-2")
    assert result is not None
    assert result.is_no_answer is True
    assert result.answer == NO_ANSWER_TEXT


def test_claim_naming_the_machine_is_not_rejected():
    """Same exemption, second location: a *claim* (not just a no-answer
    explanation) naturally referencing the machine by name goes through
    _claim_supported, a separate function with its own material-token
    check, and must not be flagged as fabricating the machine's own name."""
    passages = [_passage(1, 1, "Replace hopper drum seal every 12 months.")]
    raw = json.dumps({
        "is_no_answer": False,
        "claims": [{
            "text": "Replace hopper drum seal every 12 months for the Ultra-1/Ultra-2.",
            "cited_excerpt_numbers": [1],
        }],
        "steps": [], "warnings": [],
    })
    result = parse_and_validate(raw, passages, "test", machine_label="Ultra-1/Ultra-2")
    assert result is not None
    assert result.is_no_answer is False


@pytest.mark.parametrize(
    "excerpt, claim, expect_supported",
    [
        # 5 vs 50 psi -- a single-digit claim not present in the excerpt at
        # all still needs a material token to check against.
        ("Set the regulator to 50 PSI.", "Set the regulator to 5 PSI.", False),
        ("Set the regulator to 50 PSI.", "Set the regulator to 50 PSI.", True),
        # 24 vs 240 V -- "24" is a genuine substring of "240", so a plain
        # containment check alone would wrongly accept it.
        ("The transformer outputs 240 V.", "The transformer outputs 24V.", False),
        ("The transformer outputs 240 V.", "The transformer outputs 240V.", True),
        # amperage vs voltage -- the unit suffix matters, not just the
        # numeral, so swapping the unit on a real numeral must be rejected.
        ("The heating element operates at 120V and draws 8.5A.", "The heating element draws 120A.", False),
        ("The heating element operates at 120V and draws 8.5A.", "The heating element draws 8.5A.", True),
        ("The heating element operates at 120V and draws 8.5A.", "The heating element operates at 120PSI.", False),
        # negative numbers -- a leading "-" must be captured, so a
        # fabricated negative is rejected against a positive excerpt.
        ("The sensor reads 240 V nominal.", "The sensor reads -240 V nominal.", False),
        ("The minimum storage temperature is -40F.", "The minimum storage temperature is -40F.", True),
        # unsupported parts -- a prefix of the real part number is a genuine
        # substring, so a plain "in" check alone would wrongly accept it.
        (
            "Replace the inlet fitting, part number 81-118-31, during service.",
            "Replace the inlet fitting, part number 81-118-3.",
            False,
        ),
        (
            "Replace the inlet fitting, part number 81-118-31, during service.",
            "Replace the inlet fitting, part number 81-118-31.",
            True,
        ),
        # compatibility / digit-embedding -- "5000" is a genuine,
        # character-for-character substring of "50000".
        (
            "This filter (part FL-200) fits machines up to serial 50000.",
            "This filter (part FL-200) fits machines up to serial 5000.",
            False,
        ),
        (
            "This filter (part FL-200) fits machines up to serial 50000.",
            "This filter (part FL-200) fits machines up to serial 50000.",
            True,
        ),
    ],
)
def test_numeric_claim_adversarial_table(excerpt, claim, expect_supported):
    """The number/identifier check must not normalize a unit-suffixed token
    down to its bare numeral (discarding the unit) and check substring
    presence anywhere in the cited passage -- that would miss a single-digit
    claim with no material token at all, accept "24" as a substring of
    "240", silently ignore a unit mismatch, and drop a leading sign before
    the token is even captured. Run as one table with a positive control
    alongside each adversarial case so the stricter checks are proven to
    still accept a genuinely-supported claim, not just reject everything (a
    check that rejects every case in this table would pass the negative rows
    by accident)."""
    passages = [_passage(1, 1, excerpt)]
    raw = json.dumps({
        "is_no_answer": False,
        "claims": [{"text": claim, "cited_excerpt_numbers": [1]}],
        "steps": [], "warnings": [],
    })
    result = parse_and_validate(raw, passages, "test")
    if expect_supported:
        assert result is not None, f"expected claim {claim!r} to be ACCEPTED against excerpt {excerpt!r}"
    else:
        assert result is None, f"expected claim {claim!r} to be REJECTED against excerpt {excerpt!r}"


@pytest.mark.parametrize(
    "excerpt, warning, expect_supported",
    [
        # negated instructions -- trimming the negation word off the front
        # of a real warning leaves a genuine, contiguous substring of it, so
        # a plain containment check alone would wrongly accept it.
        ("Do not operate with the cover removed.", "Operate with the cover removed.", False),
        ("Do not operate with the cover removed.", "Do not operate with the cover removed.", True),
        ("WARNING: Never bypass the interlock switch.", "Bypass the interlock switch.", False),
        ("WARNING: Never bypass the interlock switch.", "Never bypass the interlock switch.", True),
    ],
)
def test_warning_negation_adversarial_table(excerpt, warning, expect_supported):
    """A warning that's merely a proper substring of its cited excerpt must
    not pass unconditionally -- trimming a leading negation word off a real
    warning produces exactly that: a genuine substring with the opposite
    meaning. The claim alongside each warning here deliberately has no
    material token, so only the warning check is exercised."""
    passages = [_passage(1, 1, excerpt)]
    raw = json.dumps({
        "is_no_answer": False,
        "claims": [{"text": excerpt, "cited_excerpt_numbers": [1]}],
        "steps": [],
        "warnings": [{"text": warning, "cited_excerpt_numbers": [1]}],
    })
    result = parse_and_validate(raw, passages, "test")
    if expect_supported:
        assert result is not None, f"expected warning {warning!r} to be ACCEPTED against excerpt {excerpt!r}"
    else:
        assert result is None, f"expected warning {warning!r} to be REJECTED against excerpt {excerpt!r}"


def test_claim_naming_the_machine_plus_an_unrelated_fabrication_is_still_rejected():
    """Regression guard: the machine-name exemption must not become a
    blanket exemption for every material token in a claim that happens to
    also mention the machine -- a fabricated part number unrelated to the
    machine's own name is still exactly the risk _claim_supported exists
    to catch."""
    passages = [_passage(1, 1, "Replace hopper drum seal every 12 months.")]
    raw = json.dumps({
        "is_no_answer": False,
        "claims": [{
            "text": "Replace part 99-999-99 on the Ultra-1/Ultra-2 every 12 months.",
            "cited_excerpt_numbers": [1],
        }],
        "steps": [], "warnings": [],
    })
    assert parse_and_validate(raw, passages, "test", machine_label="Ultra-1/Ultra-2") is None


_SAFETY_PASSAGE = "Before service, disconnect power. Keep all guards installed during operation."


def _single_claim(text, passage_text=_SAFETY_PASSAGE, *, as_step=False):
    passages = [_passage(1, 1, passage_text)]
    item = [{"text": text, "cited_excerpt_numbers": [1]}]
    raw = json.dumps({
        "is_no_answer": False,
        "claims": [] if as_step else item,
        "steps": item if as_step else [],
        "warnings": [],
    })
    return parse_and_validate(raw, passages, "test")


@pytest.mark.parametrize("text", [
    "The machine is safe to operate with all guards removed.",
    "Bypass the safety interlock and operate with the cover removed.",
    "Remove the guards during operation.",
    "May operate while energized.",
])
def test_unsupported_qualitative_instruction_is_rejected(text):
    assert _single_claim(text) is None
    assert _single_claim(text, as_step=True) is None


def test_a_dropped_prohibition_is_rejected():
    assert _single_claim("Remove the guard during operation.", "Do not remove the guard during operation.") is None


def test_a_kept_prohibition_passes():
    assert _single_claim("Do not remove the guard during operation.", "Do not remove the guard during operation.")


def test_inverted_order_is_rejected():
    passage = "Disconnect power. Remove the cover."
    assert _single_claim("Remove the cover. Disconnect power.", passage, as_step=True) is None
    assert _single_claim("Disconnect power. Remove the cover.", passage, as_step=True) is not None


@pytest.mark.parametrize("text", [
    "Before service, disconnect power.",
    "Disconnect power.",
    "Keep all guards installed during operation.",
    "Keep guards installed.",
])
def test_a_claim_made_of_the_excerpts_own_words_passes(text):
    assert _single_claim(text) is not None


def test_a_reworded_permission_is_rejected_even_with_the_same_topic():
    passage = "Only a qualified technician may open the control panel."
    assert _single_claim("A qualified technician may open the control panel.", passage) is None
    assert _single_claim("Only a qualified technician may open the control panel.", passage) is not None


def test_a_claim_that_is_not_the_excerpts_wording_rejects_the_whole_response():
    """A failing claim must never be silently dropped while the rest of the
    answer displays -- that could remove a prerequisite (phrased by the model
    as a "claim") while a hazardous step stays. Any failure rejects the whole
    response, the same as a failing step."""
    passages = [_passage(1, 1, "Disconnect power. Keep all guards installed during operation.")]
    raw = json.dumps({
        "is_no_answer": False,
        "claims": [
            {"text": "Keep all guards installed during operation.", "cited_excerpt_numbers": [1]},
            {"text": "Bypass the safety interlock and operate with the cover removed.", "cited_excerpt_numbers": [1]},
        ],
        "steps": [], "warnings": [],
    })
    assert parse_and_validate(raw, passages, "test") is None


def test_a_step_that_is_not_the_excerpts_wording_rejects_the_whole_response():
    passages = [_passage(1, 1, "Disconnect power. Remove the cover.")]
    raw = json.dumps({
        "is_no_answer": False,
        "claims": [{"text": "Disconnect power.", "cited_excerpt_numbers": [1]}],
        "steps": [{"text": "Bypass the interlock.", "cited_excerpt_numbers": [1]}],
        "warnings": [],
    })
    assert parse_and_validate(raw, passages, "test") is None


def test_a_claim_may_not_add_a_negation_the_excerpt_lacks():
    assert _single_claim("Do not remove the guard during operation.", "Remove the guard during operation.") is None


def test_a_reworded_table_label_is_tolerated_but_a_reworded_action_is_not():
    passage = "PROBABLE CAUSE: Tank Heater failure."
    assert _single_claim("Possible cause: Tank Heater failure.", passage) is not None
    assert _single_claim("Replace the tank heater.", "Check the tank heater.") is None


def test_a_word_hyphenated_across_a_line_break_in_the_excerpt_still_matches():
    passage = "Remove the tank lid and clean in-\nside of tank with a deliming agent."
    assert _single_claim("Remove the tank lid and clean inside of tank with a deliming agent.", passage) is not None


def test_failing_items_names_what_did_not_check_out():
    from app.providers.base import failing_items

    passages = [_passage(1, 1, "Disconnect power. Keep all guards installed during operation.")]
    raw = json.dumps({
        "is_no_answer": False,
        "claims": [
            {"text": "Disconnect power.", "cited_excerpt_numbers": [1]},
            {"text": "Bypass the safety interlock.", "cited_excerpt_numbers": [1]},
        ],
        "steps": [], "warnings": [],
    })
    assert failing_items(raw, passages) == ["Bypass the safety interlock."]
    assert failing_items("not json", passages) == []


def test_a_safety_conclusion_from_the_wrong_clause_is_rejected():
    passages = [_passage(1, 1, "Hot water may cause severe burns. The machine is safe when disconnected.")]
    assert _single_claim("Hot water is safe.", passages[0].content) is None


def test_a_prohibition_is_not_flipped_by_padding_with_an_unrelated_clause():
    passages = [_passage(1, 1, "Do not operate the machine. Guards protect personnel during cleaning.")]
    claim = "Operate the machine; guards protect personnel during cleaning."
    assert _single_claim(claim, passages[0].content) is None


def test_a_relational_fact_cannot_be_reassembled_from_two_clauses():
    excerpt = "The red wire connects terminal A to terminal B. The blue wire connects terminal C to terminal D."
    assert _single_claim("The red wire connects terminal C to terminal D.", excerpt) is None
    assert _single_claim("The red wire connects terminal A to terminal B.", excerpt) is not None


def test_a_prerequisite_phrased_as_a_claim_cannot_silently_disappear():
    """A model can split an excerpt into a claim ("Disconnect power first.")
    and a step ("Remove the cover."). If the claim doesn't check out, it must
    not be dropped while the step still displays -- that would show "Remove
    the cover." with no mention of disconnecting power first. A failing claim
    rejects the whole response, the same as a failing step."""
    passages = [_passage(1, 1, "Disconnect power before removing the cover. Remove the cover.")]
    raw = json.dumps({
        "is_no_answer": False,
        "claims": [{"text": "Disconnect power first.", "cited_excerpt_numbers": [1]}],
        "steps": [{"text": "Remove the cover.", "cited_excerpt_numbers": [1]}],
        "warnings": [],
    })
    assert parse_and_validate(raw, passages, "test") is None


# A claim can keep every one of the excerpt's own words -- no invented number,
# no invented identifier, nothing _claim_supported alone would catch -- and
# still swap which entity plays which role. Coverage-based grounding (every
# claim word present *somewhere* in the matched excerpt sentence) cannot tell
# these apart from the genuine claim; only checking that the matched words
# keep the excerpt's own relative order can.
def test_a_cause_and_effect_cannot_be_swapped():
    assert _single_claim("Hot water causes severe burns.", "Severe burns cause hot water.") is None


def test_a_before_after_identifier_pair_cannot_be_swapped():
    assert _single_claim("Remove panel B before panel A.", "Remove panel A before panel B.") is None


def test_a_flow_direction_cannot_be_reversed():
    assert _single_claim(
        "Water flows from the spray arm to the tank.",
        "Water flows from the tank to the spray arm.",
    ) is None


# Distinct from the reversal above: there the two entities themselves swap
# position, which the order check alone already catches. Here the entities
# keep the excerpt's own order -- only "from"/"to" swap which one is the
# source and which is the destination -- so only tracking "to"/"from"/"by"
# as words in their own right (_RELATIONAL_STEMS), not discarding them as
# ordinary stopwords, catches it.
def test_a_source_and_destination_preposition_pair_cannot_be_swapped():
    assert _single_claim(
        "Move water to the wash tank from the spray arm.",
        "Move water from the wash tank to the spray arm.",
    ) is None


# Also distinct from test_a_cause_and_effect_cannot_be_swapped: there both
# texts are active voice with the two entities swapped, which the order
# check catches. Here every word stays in the same left-to-right order --
# dropping "by" is what turns the passive "caused by" into an active
# "causes", reversing which entity is the cause and which is the effect.
def test_a_passive_cause_loses_its_by_and_is_rejected():
    assert _single_claim(
        "Severe burns cause hot water.",
        "Severe burns are caused by hot water.",
    ) is None


def test_a_wire_to_terminal_pairing_cannot_be_swapped():
    assert _single_claim(
        "Connect the red wire to terminal B and the black wire to terminal A.",
        "Connect the red wire to terminal A and the black wire to terminal B.",
    ) is None


def test_a_before_after_action_pair_cannot_be_swapped():
    assert _single_claim("Open the pump before starting the valve.", "Open the valve before starting the pump.") is None


def test_a_value_cannot_be_reassigned_to_the_wrong_step():
    assert _single_claim(
        "Use 180F for the rinse cycle and 150F for the sanitize cycle.",
        "Use 150F for the rinse cycle and 180F for the sanitize cycle.",
    ) is None


def test_two_clauses_in_one_sentence_cannot_be_reordered():
    """Both clauses individually match the same excerpt sentence -- only
    comparing where each one matches WITHIN that sentence (not just which
    sentence) catches the swap."""
    assert _single_claim(
        "Remove the cover and disconnect power.",
        "Disconnect power and remove the cover.",
    ) is None


def test_a_negation_cannot_be_stripped_behind_a_long_qualifying_clause():
    """A fixed character window around the matched span would miss a
    negation word this far from it; the check now scans the whole sentence
    the match sits in."""
    excerpt = (
        "WARNING: Under no circumstances, for any reason, regardless of "
        "training level, should you ever operate this unit with the access "
        "panel removed. Replace the cover before use."
    )
    passages = [_passage(1, 1, excerpt)]
    raw = json.dumps({
        "is_no_answer": False,
        "claims": [], "steps": [{"text": "Replace the cover before use.", "cited_excerpt_numbers": [1]}],
        "warnings": [{
            "text": "operate this unit with the access panel removed",
            "cited_excerpt_numbers": [1],
        }],
    })
    assert parse_and_validate(raw, passages, "test") is None


def test_a_required_warning_is_added_even_when_the_model_omits_it():
    """A model can ground a real step in a passage and simply never mention
    the WARNING sentence sitting next to it in that same passage -- nothing
    in _claim_grounded catches an omission, since there's no failing item to
    reject. The passage's own labeled warning is attached to the answer
    regardless of what the model's "warnings" list contains."""
    passages = [_passage(1, 1, "WARNING: Disconnect power before servicing. Remove the cover.")]
    raw = json.dumps({
        "is_no_answer": False,
        "claims": [],
        "steps": [{"text": "Remove the cover.", "cited_excerpt_numbers": [1]}],
        "warnings": [],
    })
    result = parse_and_validate(raw, passages, "test")
    assert result is not None
    assert result.safety_warnings == ["WARNING: Disconnect power before servicing."]


def test_a_required_warning_is_added_even_from_a_passage_the_model_never_cited():
    """Distinct from the test above: there the warning lives in the SAME
    passage the model cited for its step. Here it lives in a DIFFERENT
    retrieved passage the model simply never cited at all -- citation is
    about which passage backs a specific claim, not about which retrieved
    passages are safety-relevant to the question, so the warning must still
    surface."""
    passages = [
        _passage(1, 1, "Remove the cover."),
        _passage(2, 1, "WARNING: Disconnect power before servicing."),
    ]
    raw = json.dumps({
        "is_no_answer": False,
        "claims": [],
        "steps": [{"text": "Remove the cover.", "cited_excerpt_numbers": [1]}],
        "warnings": [],
    })
    result = parse_and_validate(raw, passages, "test")
    assert result is not None
    assert result.safety_warnings == ["WARNING: Disconnect power before servicing."]


def test_a_required_warning_is_found_after_a_heading_not_just_at_the_start_of_the_excerpt():
    """_excerpt_lines splits on sentence-ending punctuation, not on every
    newline, so a heading with no period of its own ("Safety Precautions")
    stays in the SAME unit as the WARNING sentence that follows it on the
    next physical line -- a check anchored only at that unit's own start
    would see "SAFETY PRECAUTIONS WARNING: ..." and never match. The
    backfilled text must also start at the label, not include the heading."""
    passages = [_passage(1, 1, "Safety Precautions\nWARNING: Disconnect power before servicing. Remove the cover.")]
    raw = json.dumps({
        "is_no_answer": False,
        "claims": [],
        "steps": [{"text": "Remove the cover.", "cited_excerpt_numbers": [1]}],
        "warnings": [],
    })
    result = parse_and_validate(raw, passages, "test")
    assert result is not None
    assert result.safety_warnings == ["WARNING: Disconnect power before servicing."]


def test_two_clauses_in_one_sentence_in_the_correct_order_still_passes():
    """The order-check tightening above must not reject a claim that
    genuinely follows the excerpt's own word order."""
    assert _single_claim(
        "Disconnect power and remove the cover.",
        "Disconnect power and remove the cover.",
    ) is not None


def test_a_number_correctly_attached_to_its_own_step_still_passes():
    assert _single_claim(
        "Use 150F for the rinse cycle.",
        "Use 150F for the rinse cycle and 180F for the sanitize cycle.",
    ) is not None


def test_a_warning_quoted_from_a_notice_labeled_passage_is_not_rejected_as_a_negation_drop():
    """The negation-drop check used to scan for the literal substring "NOT"
    anywhere in the excerpt sentence, which also matches inside the word
    "NOTICE" -- a label this corpus's troubleshooting tables actually use.
    That falsely treated every NOTICE-labeled passage as if it contained a
    dropped negation, rejecting an otherwise fully-grounded, correctly
    quoted warning."""
    passages = [_passage(1, 1, (
        "NOTICE: Brew water temperature is factory set at 200F. "
        "Areas of high altitude will require lowering this temperature to prevent boiling."
    ))]
    raw = json.dumps({
        "is_no_answer": False,
        "claims": [{"text": "Brew water temperature is factory set at 200F.", "cited_excerpt_numbers": [1]}],
        "steps": [],
        "warnings": [{
            "text": "Areas of high altitude will require lowering this temperature to prevent boiling.",
            "cited_excerpt_numbers": [1],
        }],
    })
    assert parse_and_validate(raw, passages, "test") is not None


def test_a_bare_list_marker_number_in_a_claim_does_not_impose_a_bogus_order():
    """A claim that copies an excerpt's own "1./2." enumeration verbatim
    (exactly what the system prompt asks for when quoting a numbered
    troubleshooting entry) used to have those bare numerals treated as
    ordinary content clauses, which could match an unrelated digit
    elsewhere in the excerpt and falsely trip the clause-order check. A
    clause left with nothing but digits after stopword filtering carries no
    fact of its own and must impose no order, the same as an empty clause."""
    excerpt = (
        "Wire For Shorts\n1. Water temperature in the tank does not meet the ready "
        "temperature.\n1. Tank Heater failure.\n2. Control Board/Thermistor failure"
    )
    assert _single_claim(
        "Water temperature in the tank does not meet the ready temperature. "
        "1. Tank Heater failure. 2. Control Board/Thermistor failure",
        excerpt,
    ) is not None

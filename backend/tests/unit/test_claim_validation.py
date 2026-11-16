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

from app.providers.base import (
    NO_ANSWER_TEXT,
    REJECTION_GROUNDING,
    REJECTION_ISOLATION_GATE,
    REJECTION_ORDERING,
    REJECTION_SCHEMA,
    parse_and_validate,
    validate_with_report,
)
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
        # the REVERSE direction: an unsigned claim
        # must not be satisfied by a genuinely negative excerpt either --
        # the sign is part of the fact in both directions.
        ("The pump is rated at -24 V.", "The pump is rated at 24 V.", False),
        ("The pump is rated at -24 V.", "The pump is rated at -24 V.", True),
        # decimal and degree-unit variants of the same reverse check.
        ("The minimum temperature is -5.5°F.", "The minimum temperature is 5.5°F.", False),
        # Unicode minus sign (U+2212), not just ASCII hyphen-minus.
        ("The reading is −40PSI.", "The reading is 40PSI.", False),
        # a genuine range hyphen is not a sign -- an unsigned claim must
        # still match a value that's part of a hyphenated range.
        ("Voltage range is 200-240V.", "Voltage range is 240V.", True),
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


def test_a_claim_cannot_stitch_two_table_rows_into_one_record():
    """A table whose rows ran together during extraction becomes one
    _excerpt_lines window with no row boundary (see _best_overlap_scope's
    own docstring) -- a claim naming row 1's identifier could otherwise
    borrow row 2's unrelated text just because the words appear in order
    somewhere in the flattened table. "E4 Fill timeout" borrows E4 from row
    1 and "Fill timeout" from row 2's cause column; it names no real row."""
    passages = [_passage(1, 1, (
        "| Code | Cause | Remedy |\n"
        "| E4 | Thermistor open | Replace thermistor |\n"
        "| E5 | Fill timeout | Check water supply |"
    ))]
    raw = json.dumps({
        "is_no_answer": False,
        "claims": [{"text": "E4 Fill timeout", "cited_excerpt_numbers": [1]}],
        "steps": [], "warnings": [],
    })
    assert parse_and_validate(raw, passages, "test") is None
    # Positive control: a claim naming one row's own identifier and its own
    # cause, with no foreign row identifier in between, must still pass --
    # this must reject cross-row stitching specifically, not every claim
    # against a multi-row table.
    raw_ok = json.dumps({
        "is_no_answer": False,
        "claims": [{"text": "E4 Thermistor open", "cited_excerpt_numbers": [1]}],
        "steps": [], "warnings": [],
    })
    assert parse_and_validate(raw_ok, passages, "test") is not None


def test_a_spec_value_sharing_its_sentence_with_another_is_not_mistaken_for_a_second_record():
    """The cross-row identifier guard above must not fire on two ordinary
    measurements in one sentence ("120V and draws 8.5A") -- a unit-suffixed
    numeral ("120v") is shaped exactly like a letter+digit identifier
    ("e4") at the token level, but it is not a second record."""
    assert _single_claim(
        "The heating element draws 8.5A.",
        "The heating element operates at 120V and draws 8.5A.",
    ) is not None


def test_two_steps_citing_duplicate_copies_of_the_same_passage_cannot_be_reversed():
    """Two different excerpt numbers can be the same passage re-chunked or
    duplicated into the retrieved set -- the cross-item ordering check is
    keyed on the excerpt's own normalized text, so two steps citing two
    different copies of an identical passage are still compared against
    each other: reversing them (step 2's text cited against copy 1, step
    1's text cited against copy 2) must still be rejected."""
    excerpt = "1. Disconnect power. 2. Remove the cover."
    passages = [_passage(1, 1, excerpt), _passage(2, 1, excerpt)]
    raw = json.dumps({
        "is_no_answer": False,
        "claims": [],
        "steps": [
            {"text": "Remove the cover.", "cited_excerpt_numbers": [1]},
            {"text": "Disconnect power.", "cited_excerpt_numbers": [2]},
        ],
        "warnings": [],
    })
    assert parse_and_validate(raw, passages, "test") is None


def test_a_reversed_isolation_step_cannot_dodge_ordering_by_calling_it_a_claim():
    """A claim phrased as a hazard-isolation action ("Disconnect power.") is
    held to the manual's own order against every excerpt citing the same
    underlying text, exactly like a step -- labeling a reversed two-step
    procedure as CLAIMS instead of STEPS, citing the same excerpt, must not
    bypass that ordering."""
    passages = [_passage(1, 1, "1. Disconnect power. 2. Remove the cover.")]
    raw = json.dumps({
        "is_no_answer": False,
        "claims": [
            {"text": "Remove the cover.", "cited_excerpt_numbers": [1]},
            {"text": "Disconnect power.", "cited_excerpt_numbers": [1]},
        ],
        "steps": [], "warnings": [],
    })
    assert parse_and_validate(raw, passages, "test") is None


def test_a_standalone_isolation_prerequisite_claim_with_a_full_separate_steps_list_still_passes():
    """No step here restates "Disconnect power.", and none needs to:
    nothing in STEPS comes from before it in the manual, so there is no
    later action hiding behind an unlisted isolation step -- the
    restatement check only applies to an action claim grounded AFTER the
    earliest isolation step, which this claim is not."""
    passages = [_passage(1, 1, "Disconnect power. Remove the cover. Replace the part. Reinstall the cover.")]
    raw = json.dumps({
        "is_no_answer": False,
        "claims": [{"text": "Disconnect power.", "cited_excerpt_numbers": [1]}],
        "steps": [
            {"text": "Remove the cover.", "cited_excerpt_numbers": [1]},
            {"text": "Replace the part.", "cited_excerpt_numbers": [1]},
            {"text": "Reinstall the cover.", "cited_excerpt_numbers": [1]},
        ],
        "warnings": [],
    })
    assert parse_and_validate(raw, passages, "test") is not None


def test_an_action_claim_after_an_unlisted_isolation_step_is_rejected():
    """Claims display ABOVE the steps list. A claim describing the action
    from right after the manual's isolation step, with the step itself
    only in STEPS (never restated in CLAIMS), would still display before
    "1. Disconnect power." to a technician reading top-to-bottom -- the
    same hazard as a reversed isolation step, just split across CLAIMS and
    STEPS instead of within one of them."""
    passages = [_passage(1, 1, "Disconnect power. Remove the cover.")]
    raw = json.dumps({
        "is_no_answer": False,
        "claims": [{"text": "Remove the cover.", "cited_excerpt_numbers": [1]}],
        "steps": [{"text": "Disconnect power.", "cited_excerpt_numbers": [1]}],
        "warnings": [],
    })
    assert parse_and_validate(raw, passages, "test") is None


def test_an_action_claim_between_two_isolation_steps_is_still_caught():
    """The isolation-step gate above must use the EARLIEST isolation step
    in the excerpt, not the latest -- claims display above the whole steps
    list starting from step one, so a claim grounded after only the FIRST
    isolation point is already displaying out of order, even if a second,
    later isolation step (disconnecting a wiring harness, say) exists
    further down the same procedure."""
    passages = [_passage(1, 1, (
        "Disconnect power. Remove the cover. Disconnect the wiring harness. Replace the board."
    ))]
    raw = json.dumps({
        "is_no_answer": False,
        "claims": [{"text": "Remove the cover.", "cited_excerpt_numbers": [1]}],
        "steps": [
            {"text": "Disconnect power.", "cited_excerpt_numbers": [1]},
            {"text": "Disconnect the wiring harness.", "cited_excerpt_numbers": [1]},
            {"text": "Replace the board.", "cited_excerpt_numbers": [1]},
        ],
        "warnings": [],
    })
    assert parse_and_validate(raw, passages, "test") is None


def test_disconnecting_a_component_that_is_not_the_hazard_is_not_an_isolation_step():
    """"Disconnect" alone does not make a claim a hazard-isolation action --
    only disconnecting power/electrical supply does. Two alternative
    remedies that each disconnect a different COMPONENT (not power), from
    different table rows, must still be freely orderable by relevance like
    any other remedy claim."""
    passages = [_passage(1, 1, (
        "E4 Thermistor open Disconnect the thermistor leads and check resistance.\n"
        "E5 Inlet clogged Disconnect the inlet hose and flush it."
    ))]
    raw = json.dumps({
        "is_no_answer": False,
        "claims": [
            {"text": "Disconnect the inlet hose and flush it.", "cited_excerpt_numbers": [1]},
            {"text": "Disconnect the thermistor leads and check resistance.", "cited_excerpt_numbers": [1]},
        ],
        "steps": [], "warnings": [],
    })
    assert parse_and_validate(raw, passages, "test") is not None


def test_a_timing_fact_after_an_isolation_step_is_not_mistaken_for_an_out_of_order_action():
    """_claim_is_action's broad verb set also matches an ordinary fact
    about an action's duration or result ("Drain time is..."), not an
    instruction to perform one -- a copula elsewhere in the clause ("is")
    marks it as a fact, exempting it from the isolation-ordering gate."""
    passages = [_passage(1, 1, "Disconnect power. Drain time is approximately 5 minutes. Remove the cover.")]
    raw = json.dumps({
        "is_no_answer": False,
        "claims": [{"text": "Drain time is approximately 5 minutes.", "cited_excerpt_numbers": [1]}],
        "steps": [{"text": "Disconnect power.", "cited_excerpt_numbers": [1]}],
        "warnings": [],
    })
    assert parse_and_validate(raw, passages, "test") is not None


def test_a_trimmed_restatement_of_a_step_still_counts_as_the_same_sentence():
    """The restatement check above matches by sentence (local_index), not
    the claim's exact end position -- a claim that trims the step's own
    wording down still grounds in the same sentence as the step, and must
    not be penalized for ending earlier in it than the full step does."""
    passages = [_passage(1, 1, (
        "Disconnect power before removing the cover. Remove the four screws and lift off the cover. "
        "Replace the sprayhead with a new one, part number 81-118-31. Reinstall the cover and reconnect power."
    ))]
    raw = json.dumps({
        "is_no_answer": False,
        "claims": [{"text": "Replace the sprayhead, part number 81-118-31.", "cited_excerpt_numbers": [1]}],
        "steps": [
            {"text": "Disconnect power before removing the cover.", "cited_excerpt_numbers": [1]},
            {"text": "Remove the four screws and lift off the cover.", "cited_excerpt_numbers": [1]},
            {"text": "Replace the sprayhead with a new one, part number 81-118-31.", "cited_excerpt_numbers": [1]},
            {"text": "Reinstall the cover and reconnect power.", "cited_excerpt_numbers": [1]},
        ],
        "warnings": [],
    })
    assert parse_and_validate(raw, passages, "test") is not None


@pytest.mark.parametrize("excerpt, first, second", [
    # An ordinary fact claim (an error code's meaning) must be freely
    # orderable, exactly as the system prompt tells the model (e.g.
    # most-likely-cause first), even against another fact claim from the
    # same excerpt that happens to ground earlier in it.
    (
        "E4 means thermistor open. E5 means fill timeout.",
        "E5 means fill timeout.", "E4 means thermistor open.",
    ),
    # An alternative remedy from a different table row is not a position in
    # a sequence -- listing the less-likely remedy first must not be
    # treated as "out of order" relative to the more-likely one.
    (
        "Replace the thermistor. Clean the drain pump.",
        "Clean the drain pump.", "Replace the thermistor.",
    ),
])
def test_non_isolation_claims_may_still_be_ordered_however_reads_clearest(excerpt, first, second):
    passages = [_passage(1, 1, excerpt)]
    raw = json.dumps({
        "is_no_answer": False,
        "claims": [
            {"text": first, "cited_excerpt_numbers": [1]},
            {"text": second, "cited_excerpt_numbers": [1]},
        ],
        "steps": [], "warnings": [],
    })
    assert parse_and_validate(raw, passages, "test") is not None


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
    # The warning's source passage (chunk_id=1) is the same one the step
    # already cited, so it reuses that citation's marker rather than
    # creating a second entry for the same chunk.
    citation_index = {c.chunk_id: i + 1 for i, c in enumerate(result.citations)}
    assert result.safety_warnings == [f"WARNING: Disconnect power before servicing. [{citation_index[1]}]"]


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
    # The warning's source passage (chunk_id=2) was never cited by anything
    # else, so a new citation is created for it, distinct from the step's.
    citation_index = {c.chunk_id: i + 1 for i, c in enumerate(result.citations)}
    assert result.safety_warnings == [f"WARNING: Disconnect power before servicing. [{citation_index[2]}]"]
    assert citation_index[2] != citation_index[1]


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
    citation_index = {c.chunk_id: i + 1 for i, c in enumerate(result.citations)}
    assert result.safety_warnings == [f"WARNING: Disconnect power before servicing. [{citation_index[1]}]"]


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


# A claim word missing from its matched excerpt sentence, whose registered
# opposite IS in that sentence, states the reverse of what the excerpt says
# ("above" for "below", "downstream" for "upstream") -- every other word can
# still match, in the excerpt's own order, so only an explicit opposite-pair
# check (not the generic unmatched-word allowance, which would otherwise
# treat the swapped word as just a reworded one) catches it. Parametrized
# across claim lengths because the generic allowance is zero below 5 content
# words, 1 from 5-11, and 2 at 12+ -- a reversal must be rejected at every
# tier, not just the one with no allowance to hide behind.
@pytest.mark.parametrize(("claim", "excerpt"), [
    ("Keep the tank above 150F.", "Keep the tank below 150F."),
    (
        "Keep the final rinse tank above 150F during the entire cycle.",
        "Keep the final rinse tank below 150F during the entire cycle.",
    ),
    (
        "Keep the final rinse tank temperature above 150F during the entire wash and rinse cycle for best results.",
        "Keep the final rinse tank temperature below 150F during the entire wash and rinse cycle for best results.",
    ),
    ("Water flows downstream of the filter.", "Water flows upstream of the filter."),
    ("Turn the inlet valve clockwise.", "Turn the inlet valve counterclockwise."),
])
def test_an_opposite_word_substitution_is_rejected_at_every_allowance_tier(claim, excerpt):
    assert _single_claim(claim, excerpt) is None


# bool("false"), bool(1) and bool(None) are all True/False in ways that
# don't reflect the JSON value's actual type -- a provider response must be
# rejected outright when is_no_answer or confidence carry a value the
# contract doesn't define, not have it silently coerced into some other
# meaning.
@pytest.mark.parametrize("bad_is_no_answer", ["false", "true", 1, 0, None, [], {}])
def test_a_non_boolean_is_no_answer_is_rejected(bad_is_no_answer):
    passages = [_passage(1, 1, "Disconnect power.")]
    raw = json.dumps({
        "is_no_answer": bad_is_no_answer,
        "claims": [{"text": "Disconnect power.", "cited_excerpt_numbers": [1]}],
        "steps": [], "warnings": [],
    })
    assert parse_and_validate(raw, passages, "test") is None


@pytest.mark.parametrize("bad_confidence", ["medium", "LOW", 1, True, [], {}])
def test_an_invalid_confidence_value_is_rejected(bad_confidence):
    passages = [_passage(1, 1, "Disconnect power.")]
    raw = json.dumps({
        "is_no_answer": False,
        "confidence": bad_confidence,
        "claims": [{"text": "Disconnect power.", "cited_excerpt_numbers": [1]}],
        "steps": [], "warnings": [],
    })
    assert parse_and_validate(raw, passages, "test") is None


def test_a_missing_confidence_field_still_passes_as_high_confidence():
    passages = [_passage(1, 1, "Disconnect power.")]
    raw = json.dumps({
        "is_no_answer": False,
        "claims": [{"text": "Disconnect power.", "cited_excerpt_numbers": [1]}],
        "steps": [], "warnings": [],
    })
    assert parse_and_validate(raw, passages, "test") is not None


@pytest.mark.parametrize("raw", [
    "not json at all",
    "null",
    "42",
    '["a", "list", "not", "an", "object"]',
    '{"is_no_answer": false, "claims": "not a list", "steps": [], "warnings": []}',
    '{"is_no_answer": false, "claims": [{"text": 5, "cited_excerpt_numbers": [1]}], "steps": [], "warnings": []}',
    '{"is_no_answer": false, "claims": [{"text": "x"}], "steps": [], "warnings": []}',  # missing cited_excerpt_numbers
    'Here is my answer: {"is_no_answer": false, "claims": [{"text": "Disconnect power.", '
    '"cited_excerpt_numbers": [1]}], "steps": [], "warnings": []} extra prose after',
])
def test_malformed_or_schema_violating_provider_responses_are_rejected(raw):
    passages = [_passage(1, 1, "Disconnect power.")]
    # The "extra prose" case above is deliberately not rejected by this
    # assertion alone -- parse_and_validate's regex extracts the embedded
    # {...} object first (see its own docstring), so that one case is
    # expected to parse successfully; every other row here has no valid
    # claims/steps and must fail the "at least one material claim or step"
    # check regardless.
    result = parse_and_validate(raw, passages, "test")
    if "extra prose" in raw:
        assert result is not None
    else:
        assert result is None


def test_a_passage_stating_both_sides_of_an_opposite_pair_is_not_a_false_positive():
    """The opposite-pair check must only fire when the claim's word is absent
    from the matched sentence and its opposite is present there instead -- a
    sentence that legitimately states both sides (a dual threshold) must not
    trip it just because the opposite word also appears somewhere in it."""
    assert _single_claim(
        "Keep the tank above 120F.",
        "Keep the tank above 120F and below 150F.",
    ) is not None


# Cross-item procedure ordering. Each of the two
# steps below is, on its own, exactly the excerpt's own wording in the
# excerpt's own order -- _claim_grounded's intra-item check has nothing to
# reject in either one. Only comparing where each SEPARATE item grounded,
# across items, catches a model emitting two individually-valid steps in the
# wrong relative order.
def test_two_separate_steps_citing_the_same_excerpt_cannot_be_reversed():
    passages = [_passage(1, 1, "Disconnect power. Remove the cover.")]

    def _steps(order):
        return json.dumps({
            "is_no_answer": False,
            "claims": [],
            "steps": [{"text": t, "cited_excerpt_numbers": [1]} for t in order],
            "warnings": [],
        })

    assert parse_and_validate(_steps(["Disconnect power.", "Remove the cover."]), passages, "test") is not None
    assert parse_and_validate(_steps(["Remove the cover.", "Disconnect power."]), passages, "test") is None


def test_steps_citing_genuinely_different_excerpts_impose_no_order_on_each_other():
    """There is no reliable signal for cross-excerpt sequence -- two steps
    that each cite their OWN excerpt, with no excerpt number shared between
    them, must not be rejected just because of the order they're listed in."""
    passages = [
        _passage(1, 1, "Remove the cover."),
        _passage(2, 1, "Disconnect power."),
    ]
    raw = json.dumps({
        "is_no_answer": False,
        "claims": [],
        "steps": [
            {"text": "Remove the cover.", "cited_excerpt_numbers": [1]},
            {"text": "Disconnect power.", "cited_excerpt_numbers": [2]},
        ],
        "warnings": [],
    })
    assert parse_and_validate(raw, passages, "test") is not None


def test_a_claim_restating_a_fact_from_later_in_the_steps_is_not_a_false_positive():
    """Cross-item ordering (see the test above) is scoped to steps only, not
    claims -- a claim is a standalone fact, not a position in a numbered
    sequence, and commonly restates something a LATER step also states (a
    replacement part number called out as its own claim as well as inside
    the step that uses it, exactly as the system prompt asks for). The claim
    here is grounded in the excerpt's THIRD sentence while step one is
    grounded in its FIRST -- this must not be treated as the claim and the
    first step being "out of order"."""
    passages = [_passage(1, 1, (
        "Disconnect power before removing the cover. Remove the four screws "
        "and lift off the cover. Replace the sprayhead with a new one, part "
        "number 81-118-31. Reinstall the cover and reconnect power."
    ))]
    raw = json.dumps({
        "is_no_answer": False,
        "claims": [
            {"text": "Replace the sprayhead with a new one, part number 81-118-31.",
             "cited_excerpt_numbers": [1]},
        ],
        "steps": [
            {"text": "Disconnect power before removing the cover.", "cited_excerpt_numbers": [1]},
            {"text": "Remove the four screws and lift off the cover.", "cited_excerpt_numbers": [1]},
            {"text": "Replace the sprayhead with a new one, part number 81-118-31.",
             "cited_excerpt_numbers": [1]},
            {"text": "Reinstall the cover and reconnect power.", "cited_excerpt_numbers": [1]},
        ],
        "warnings": [],
    })
    assert parse_and_validate(raw, passages, "test") is not None


def test_a_multiclause_steps_governing_condition_can_stay_in_its_first_clause():
    """_claim_clauses legitimately splits 'If the tank is empty, disconnect power and refill
    before continuing.' into two clauses ('If the tank is empty, disconnect
    power' and 'refill before continuing') because each side names its own
    action. The governing 'if' correctly stays in the first clause only --
    checking the SECOND clause's dropped-conditional words against just its
    own text (instead of the whole step's text) would wrongly flag it for
    'dropping' a word its sibling clause already accounts for."""
    assert _single_claim(
        "If the tank is empty, disconnect power and refill before continuing.",
        "If the tank is empty, disconnect power and refill before continuing.",
        as_step=True,
    ) is not None


# A dropped prerequisite/condition word. Both examples below are, word for
# word, a genuine substring of the excerpt in the excerpt's own order, so
# the order/wording check alone has nothing to reject in either one.
def test_a_before_prerequisite_naming_a_different_action_cannot_be_dropped():
    assert _single_claim(
        "Remove the cover.",
        "Disconnect power before removing the cover.",
        as_step=True,
    ) is None


def test_an_if_condition_cannot_be_dropped():
    assert _single_claim(
        "Disconnect power.",
        "If the tank is empty, disconnect power.",
        as_step=True,
    ) is None


@pytest.mark.parametrize(("excerpt", "claim"), [
    ("While the pump is running, do not open the drain valve.", "Do not open the drain valve."),
    ("When the display shows E4, replace the thermistor.", "Replace the thermistor."),
    ("Do not operate the unit except during a scheduled cycle.", "Do not operate the unit."),
])
def test_other_conditional_words_cannot_be_dropped(excerpt, claim):
    assert _single_claim(claim, excerpt) is None


def test_a_before_qualifier_on_a_single_action_is_not_a_false_positive():
    """Distinct from the dropped-prerequisite case above: 'before service' is
    an ordinary timing adverb on ONE action, not a word sequencing two
    different actions -- the excerpt names no other action the claim left
    out, so dropping 'before service' must still pass, the same as dropping
    any other reworded qualifier."""
    assert _single_claim(
        "Disconnect power.",
        "Before service, disconnect power.",
    ) is not None


def test_validate_with_report_classifies_malformed_json_as_schema():
    result, report = validate_with_report("not json at all", [_passage(1, 1, "Disconnect power.")], "test")
    assert result is None
    assert report.kind == REJECTION_SCHEMA
    assert report.item_text is None


def test_validate_with_report_classifies_a_fabricated_number_as_grounding():
    passages = [_passage(1, 1, "Replace the inlet fitting, part number 81-118-31, during service.")]
    raw = json.dumps({
        "is_no_answer": False,
        "claims": [{"text": "Replace the inlet fitting, part number 99-000-00.", "cited_excerpt_numbers": [1]}],
        "steps": [], "warnings": [],
    })
    result, report = validate_with_report(raw, passages, "test")
    assert result is None
    assert report.kind == REJECTION_GROUNDING
    assert report.item_text == "Replace the inlet fitting, part number 99-000-00."


def test_validate_with_report_classifies_a_reversed_step_as_ordering():
    passages = [_passage(1, 1, "1. Disconnect power. 2. Remove the cover.")]
    raw = json.dumps({
        "is_no_answer": False,
        "claims": [],
        "steps": [
            {"text": "Remove the cover.", "cited_excerpt_numbers": [1]},
            {"text": "Disconnect power.", "cited_excerpt_numbers": [1]},
        ],
        "warnings": [],
    })
    result, report = validate_with_report(raw, passages, "test")
    assert result is None
    assert report.kind == REJECTION_ORDERING
    assert report.item_text == "Disconnect power.", "the offending (regressing) item, not the one it regressed behind"


def test_validate_with_report_classifies_a_split_label_bypass_as_isolation_gate():
    passages = [_passage(1, 1, "Disconnect power. Remove the cover.")]
    raw = json.dumps({
        "is_no_answer": False,
        "claims": [{"text": "Remove the cover.", "cited_excerpt_numbers": [1]}],
        "steps": [{"text": "Disconnect power.", "cited_excerpt_numbers": [1]}],
        "warnings": [],
    })
    result, report = validate_with_report(raw, passages, "test")
    assert result is None
    assert report.kind == REJECTION_ISOLATION_GATE
    assert report.item_text == "Remove the cover."


def test_validate_with_report_returns_no_report_on_success():
    passages = [_passage(1, 1, "Disconnect power.")]
    raw = json.dumps({
        "is_no_answer": False,
        "claims": [{"text": "Disconnect power.", "cited_excerpt_numbers": [1]}],
        "steps": [], "warnings": [],
    })
    result, report = validate_with_report(raw, passages, "test")
    assert result is not None
    assert report is None


def test_parse_and_validate_matches_validate_with_reports_result():
    """parse_and_validate is a thin wrapper -- pins that it returns exactly
    the same answer validate_with_report does, dropping only the report."""
    passages = [_passage(1, 1, "Disconnect power.")]
    raw = json.dumps({
        "is_no_answer": False,
        "claims": [{"text": "Disconnect power.", "cited_excerpt_numbers": [1]}],
        "steps": [], "warnings": [],
    })
    result, _ = validate_with_report(raw, passages, "test")
    assert parse_and_validate(raw, passages, "test") == result

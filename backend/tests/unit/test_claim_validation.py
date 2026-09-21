"""P0-7 (independent follow-up review): parse_and_validate previously checked
only that cited excerpt *numbers* existed -- ID validation, not evidence
validation. A model could cite a real excerpt while inventing the number or
warning text it attributed to that excerpt. These tests reproduce the review's
adversarial diagnostic (fabricated part, fabricated voltage, invented safety
warning, invented conflict) directly against parse_and_validate, plus one
positive case proving a genuinely-supported answer still passes.

These exercise parse_and_validate in isolation -- they do not call the real
Anthropic API (no key is configured in this environment; AI_PROVIDER stays
local_extractive for the whole suite, see docs/PRODUCTION_READINESS.md).
They prove the validation logic that provider depends on is sound, not that
a live model's output currently passes it.
"""
from __future__ import annotations

import json

from app.providers.base import parse_and_validate
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
            {"text": "One revision specifies 25 ft-lb.", "cited_excerpt_numbers": [1]},
            {"text": "A later revision specifies 30 ft-lb.", "cited_excerpt_numbers": [2]},
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
            {"text": "The replacement part is 81-118-31.", "cited_excerpt_numbers": [1]},
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


def test_low_confidence_answer_surfaces_a_caveat_in_the_response():
    """Owner decision (2026-09-16): threshold shouldn't be super high, but a
    low-confidence answer must say so in the response text itself. Every
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
    assert result.answer == "The excerpts don't cover this question."


def test_no_answer_explanation_with_fabricated_technical_content_is_rejected():
    """Independent follow-up review 2026-08-24 P0-5: is_no_answer used to
    skip every claim/warning check, so a fabricated, specific instruction
    could reach the technician disguised as an "I couldn't find this"
    message. Reproduces the review's own example: no cited excerpt backs
    "600V" at all, since no_answer_explanation has no citation mechanism --
    a genuine non-answer has no reason to state a voltage."""
    passages = [_passage(1, 1, "This manual does not cover high-voltage interlock procedures.")]
    raw = json.dumps({
        "is_no_answer": True,
        "no_answer_explanation": "You can bypass the interlock at 600V to proceed.",
        "claims": [], "steps": [], "warnings": [],
    })
    assert parse_and_validate(raw, passages, "test") is None


def test_no_answer_explanation_without_technical_content_still_passes():
    """The fix must not reject ordinary, harmless non-answers that happen to
    mention a plain small number with no letters attached (e.g. "page 2")
    -- only identifier/measurement-shaped tokens matter, same rule
    _material_tokens already applies to real claims."""
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
    """Found live 2026-08-25: a technician on the "Ultra-1/Ultra-2" got
    UNVERIFIED_ANSWER for several honestly-unanswerable questions in a row.
    The model's real explanation was fine each time -- it just naturally
    named the machine it was told about, and that name has two digits ("1",
    "2") scattered in it, which _material_tokens flagged the same as it
    would a fabricated part number. The machine name is prompt-given
    context (see AnthropicProvider.generate's "Selected machine:" line),
    not something the model could be fabricating, so parse_and_validate now
    takes it as a parameter and exempts its own tokens from this check."""
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
    assert "Ultra-1/Ultra-2" in result.answer


def test_claim_naming_the_machine_is_not_rejected():
    """Same bug, second location: a *claim* (not just a no-answer
    explanation) naturally referencing the machine by name goes through
    _claim_supported, a separate function with its own material-token
    check, and hit the identical false positive live 2026-08-25 -- a
    "what can I ask you" summary claim said "...for the Ultra-1/Ultra-2"
    and got the whole six-claim answer rejected over that one phrase."""
    passages = [_passage(1, 1, "Replace hopper drum seal every 12 months.")]
    raw = json.dumps({
        "is_no_answer": False,
        "claims": [{
            "text": "The excerpts cover the 12 month maintenance schedule for the Ultra-1/Ultra-2.",
            "cited_excerpt_numbers": [1],
        }],
        "steps": [], "warnings": [],
    })
    result = parse_and_validate(raw, passages, "test", machine_label="Ultra-1/Ultra-2")
    assert result is not None
    assert result.is_no_answer is False


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

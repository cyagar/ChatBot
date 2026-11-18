"""Pure-function tests for scripts/eval_scoring.py's score_case -- no DB,
no network, no FastAPI app. Importing this module must never trip
eval_retrieval.py's own production-safety guard (that guard runs as a
side effect at import time in eval_retrieval.py itself, which is exactly
why the scoring logic lives in a separate module)."""
from __future__ import annotations

from scripts.eval_scoring import score_case


def _case(**kwargs) -> dict:
    base = {"id": "case-1", "category": "test", "question": "q?"}
    base.update(kwargs)
    return base


def _body(**kwargs) -> dict:
    base = {"answer_status": "completed", "is_no_answer": False, "citations": [], "answer": ""}
    base.update(kwargs)
    return base


def test_right_citation_and_excerpt_keyword_but_wrong_answer_value_fails_correctness_only():
    """The right document is cited, the expected keyword is present in
    that CITED EXCERPT, but the answer text itself states a different
    (wrong) value -- cited_support must pass while answer_correctness
    fails, proving the two are genuinely independent checks."""
    case = _case(
        expected_filename_contains="axiom.pdf", expected_keywords=["240V"],
        expected_answer_contains=["120V"],
    )
    body = _body(
        citations=[{"filename": "axiom.pdf", "chunk_id": 1, "excerpt": "Operates at 240V nominal."}],
        answer="- This model operates at 240V nominal. [1]",
    )
    result = score_case(case, body, retrieved_filenames=["axiom.pdf"], chunk_types={})
    assert result["cited_support"] is True
    assert result["answer_correctness"] is False
    assert result["ok"] is False


def test_retrieved_but_not_cited_is_a_citation_selection_failure_not_a_retrieval_failure():
    """A passage can be genuinely retrieved (present in message_sources)
    without the model choosing to cite it -- a single combined check
    cannot tell this apart from the passage never being retrieved at
    all."""
    case = _case(expected_filename_contains="axiom.pdf")
    body = _body(citations=[{"filename": "other.pdf", "chunk_id": 2, "excerpt": "unrelated"}])
    result = score_case(case, body, retrieved_filenames=["axiom.pdf", "other.pdf"], chunk_types={})
    assert result["retrieval_hit"] is True
    assert result["citation_selected"] is False
    assert result["ok"] is False


def test_never_retrieved_at_all_fails_retrieval_too():
    case = _case(expected_filename_contains="axiom.pdf")
    body = _body(citations=[{"filename": "other.pdf", "chunk_id": 2, "excerpt": "unrelated"}])
    result = score_case(case, body, retrieved_filenames=["other.pdf"], chunk_types={})
    assert result["retrieval_hit"] is False
    assert result["citation_selected"] is False
    assert result["ok"] is False


def test_steps_out_of_order_fails_procedure_safety():
    case = _case(expected_step_order=["Disconnect power", "Remove the cover"])
    body = _body(answer="**Steps:**\n1. Remove the cover. [1]\n2. Disconnect power. [1]")
    result = score_case(case, body, retrieved_filenames=[], chunk_types={})
    assert result["procedure_safety"] is False
    assert result["ok"] is False


def test_steps_in_order_passes_procedure_safety():
    case = _case(expected_step_order=["Disconnect power", "Remove the cover"])
    body = _body(answer="**Steps:**\n1. Disconnect power. [1]\n2. Remove the cover. [1]")
    result = score_case(case, body, retrieved_filenames=[], chunk_types={})
    assert result["procedure_safety"] is True
    assert result["ok"] is True


def test_a_claim_restating_a_later_step_above_the_steps_header_does_not_false_positive():
    """A real answer shape this system produces: a claim line restating
    part of a LATER step (e.g. a part number) renders ABOVE
    "**Steps:**", before the numbered steps themselves. The step-order
    check must only look inside the numbered STEPS section -- searching
    the whole answer would find a step's own wording in that claim line
    first, at a position earlier than an actual earlier step, and wrongly
    flag a correctly-ordered answer as out of order."""
    case = _case(expected_step_order=["Disconnect power", "Replace the thermistor"])
    body = _body(
        answer=(
            "- Replace the thermistor, part number 81-118-31. [1]\n\n"
            "**Steps:**\n"
            "1. Disconnect power to the unit. [1]\n"
            "2. Remove the rear access panel (4 screws). [1]\n"
            "3. Replace the thermistor, part number 81-118-31. [1]\n"
            "4. Reinstall the panel and restore power. [1]"
        )
    )
    result = score_case(case, body, retrieved_filenames=[], chunk_types={})
    assert result["procedure_safety"] is True
    assert result["ok"] is True


def test_no_steps_header_at_all_fails_explicitly():
    case = _case(expected_step_order=["Disconnect power"])
    body = _body(answer="- Disconnect power before servicing. [1]")
    result = score_case(case, body, retrieved_filenames=[], chunk_types={})
    assert result["procedure_safety"] is False
    assert any("no numbered steps" in n.lower() or "no '**steps:**'" in n.lower() for n in result["notes"])


def test_missing_required_warning_fails_procedure_safety():
    case = _case(required_warnings=["Disconnect power before servicing"])
    body = _body(answer="1. Remove the cover.", safety_warnings=["Hot surface."])
    result = score_case(case, body, retrieved_filenames=[], chunk_types={})
    assert result["procedure_safety"] is False
    assert result["ok"] is False


def test_present_required_warning_passes_procedure_safety():
    case = _case(required_warnings=["Disconnect power before servicing"])
    body = _body(answer="1. Remove the cover.", safety_warnings=["Disconnect power before servicing."])
    result = score_case(case, body, retrieved_filenames=[], chunk_types={})
    assert result["procedure_safety"] is True


def test_a_forbidden_answer_value_fails_correctness():
    """answer_must_not_contain catches the sibling-model-value leak
    directly in the answer text, not just in a forbidden citation."""
    case = _case(answer_must_not_contain=["Ultra NX"])
    body = _body(answer="This procedure is documented for the Ultra NX.")
    result = score_case(case, body, retrieved_filenames=[], chunk_types={})
    assert result["answer_correctness"] is False


def test_a_case_defining_no_optional_fields_leaves_those_dimensions_unchecked():
    """A case without expected_answer_contains/answer_must_not_contain/
    expected_step_order/required_warnings must not have those keys in the
    result at all -- the caller distinguishes "not checked" from "checked
    and passed", so a case that defines nothing never silently inflates a
    100% answer-correctness or procedure-safety rate."""
    case = _case(expected_filename_contains="axiom.pdf", expected_keywords=["240V"])
    body = _body(
        citations=[{"filename": "axiom.pdf", "chunk_id": 1, "excerpt": "Operates at 240V."}],
        answer="- Operates at 240V. [1]",
    )
    result = score_case(case, body, retrieved_filenames=["axiom.pdf"], chunk_types={})
    assert "answer_correctness" not in result
    assert "procedure_safety" not in result
    assert result["ok"] is True


def test_expect_no_answer_short_circuits_before_any_other_dimension():
    case = _case(expect_no_answer=True)
    body = _body(is_no_answer=True, answer_status="completed")
    result = score_case(case, body, retrieved_filenames=[], chunk_types={})
    assert result["ok"] is True
    assert "retrieval_hit" not in result


def test_a_system_failure_is_never_scored_as_a_retrieval_or_citation_miss():
    case = _case(expected_filename_contains="axiom.pdf")
    body = _body(answer_status="failed")
    result = score_case(case, body, retrieved_filenames=["axiom.pdf"], chunk_types={})
    assert result["ok"] is False
    assert "retrieval_hit" not in result
    assert any("system" in n.lower() for n in result["notes"])

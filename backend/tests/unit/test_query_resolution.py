"""Follow-ups must be resolved into a stored standalone retrieval query
before hybrid_search: hybrid_search has no conversational reasoning, so a
follow-up like "What about replacing it?" would retrieve on the literal
words alone and never find passages about the actual antecedent.

Pure-function tests: no DB, no provider, no FastAPI app.
"""
from __future__ import annotations

from app.providers.base import HistoryTurn
from app.retrieval.query_resolution import resolve_follow_up_query


def test_standalone_question_is_returned_unchanged():
    history = [
        HistoryTurn(role="user", content="Why is the Axiom brewer not heating?"),
        HistoryTurn(role="assistant", content="Check the tank heater and the thermistor for failure."),
    ]
    q = "How do I reset the error codes on the CMA-180 dishmachine?"
    assert resolve_follow_up_query(q, history) == q


def test_no_history_returns_question_unchanged():
    assert resolve_follow_up_query("What about replacing it?", []) == "What about replacing it?"


def test_pronoun_follow_up_pulls_context_from_the_assistant_turn():
    """The review's own three-turn example, first hop: 'it' in the follow-up
    refers to what the ASSISTANT said (the heating element), not the prior
    user question's own wording."""
    history = [
        HistoryTurn(role="user", content="Why is it not heating?"),
        HistoryTurn(role="assistant", content="Check the tank heater and thermistor for failure."),
    ]
    resolved = resolve_follow_up_query("What about replacing it?", history)
    assert resolved != "What about replacing it?"
    assert resolved.startswith("What about replacing it?")
    assert "heater" in resolved.lower() or "thermistor" in resolved.lower()


def test_three_turn_conversation_resolves_each_follow_up():
    """Turn 1 standalone -> turn 2 follow-up resolved using turn 1's answer ->
    turn 3 follow-up resolved using turn 2's answer, chaining correctly."""
    history_at_turn2 = [
        HistoryTurn(role="user", content="Why is it not heating?"),
        HistoryTurn(role="assistant", content="Check the tank heater and thermistor for failure."),
    ]
    resolved_turn2 = resolve_follow_up_query("What about replacing it?", history_at_turn2)
    assert "heater" in resolved_turn2.lower() or "thermistor" in resolved_turn2.lower()

    history_at_turn3 = history_at_turn2 + [
        HistoryTurn(role="user", content="What about replacing it?"),
        HistoryTurn(role="assistant", content="Replace the tank heater, part number 81-118-31, using a 1/2 inch wrench."),
    ]
    resolved_turn3 = resolve_follow_up_query("Which connector?", history_at_turn3)
    assert resolved_turn3 != "Which connector?"
    assert "wrench" in resolved_turn3.lower() or "81-118-31" in resolved_turn3 or "heater" in resolved_turn3.lower()


def test_short_question_with_no_pronoun_still_counts_as_a_follow_up():
    """'Which connector?' has no pronoun but is too short to stand alone --
    the word-count fallback must still trigger resolution."""
    history = [
        HistoryTurn(role="user", content="What about replacing it?"),
        HistoryTurn(role="assistant", content="Replace the tank heater using a 1/2 inch wrench."),
    ]
    resolved = resolve_follow_up_query("Which connector?", history)
    assert resolved != "Which connector?"


def test_leading_cue_phrase_triggers_resolution_even_with_enough_words():
    history = [
        HistoryTurn(role="user", content="Why is the brewer not heating?"),
        HistoryTurn(role="assistant", content="Check the tank heater and thermistor for a wiring fault."),
    ]
    resolved = resolve_follow_up_query("And what about the wiring harness assembly itself?", history)
    assert resolved != "And what about the wiring harness assembly itself?"


def test_original_wording_is_a_prefix_of_the_resolved_query():
    """Resolution only ever appends context -- it must never rewrite or drop
    the technician's own words."""
    history = [
        HistoryTurn(role="user", content="Why is it not heating?"),
        HistoryTurn(role="assistant", content="Check the tank heater and thermistor for failure."),
    ]
    q = "What about replacing it?"
    resolved = resolve_follow_up_query(q, history)
    assert resolved.startswith(q)


# --- Negatives --------------------------------------------------------
# The prior test above only covers the successful-resolution path. These
# cover the resolver's confidence model -- when it must NOT guess.


def test_no_answer_assistant_turn_is_not_scraped_for_antecedent_words():
    """The immediately-prior assistant turn was a failure/no-answer message
    (real boilerplate this codebase actually sends, see routes_chat.py). Its
    prose must never be treated as an antecedent -- none of its own words may
    appear in the resolved query."""
    history = [
        HistoryTurn(role="user", content="Why is it not heating?"),
        HistoryTurn(
            role="assistant",
            content="I couldn't reach the AI provider (timed out). Please try again in a moment.",
            is_no_answer=True,
        ),
    ]
    resolved = resolve_follow_up_query("What about replacing it?", history)
    for junk in ("provider", "timed", "moment", "please"):
        assert junk not in resolved.lower()


def test_no_extractable_content_anywhere_returns_question_unchanged():
    """The only assistant turn failed (skipped) and the prior user turn has
    no extractable content words of its own either -- there is nothing
    confident to resolve against, so the question must come back unchanged
    rather than being padded with failure-message noise."""
    history = [
        HistoryTurn(role="user", content="What is it?"),
        HistoryTurn(
            role="assistant",
            content="I couldn't search the manuals right now due to a temporary technical problem.",
            is_no_answer=True,
        ),
    ]
    q = "What about it?"
    assert resolve_follow_up_query(q, history) == q


def test_a_short_error_code_question_after_an_unrelated_turn_is_unchanged():
    """'Error E4' has only one real content word ('Error' -- 'E4' is below
    _content_words' own minimum length) and would otherwise be misjudged
    as too short to stand alone, inheriting an unrelated prior topic's
    words into its retrieval query and diluting the ranking signal for the
    one exact code that actually matters."""
    history = [
        HistoryTurn(role="user", content="How do I fix an inlet valve leak?"),
        HistoryTurn(role="assistant", content="Check the inlet valve seals on the water supply line for leaking."),
    ]
    q = "Error E4"
    assert resolve_follow_up_query(q, history) == q


def test_a_cue_phrase_naming_a_different_code_is_still_unchanged():
    """The identifier check overrides a leading cue phrase too: 'What about
    E5?' after an E4 discussion names its OWN target code and must not
    inherit E4's context words just because it opens with 'what about'."""
    history = [
        HistoryTurn(role="user", content="What does error E4 mean?"),
        HistoryTurn(role="assistant", content="E4 indicates a thermistor failure; replace the thermistor."),
    ]
    q = "What about E5?"
    assert resolve_follow_up_query(q, history) == q


def test_a_part_number_question_is_unchanged_even_with_a_pronoun():
    history = [
        HistoryTurn(role="user", content="How do I replace the tank heater?"),
        HistoryTurn(role="assistant", content="Disconnect power, then replace the tank heater."),
    ]
    q = "Is this part number 81-118-31?"
    assert resolve_follow_up_query(q, history) == q


def test_a_bare_number_is_not_mistaken_for_an_identifier():
    """A plain number ('12') must not count as the question 'naming its own
    identifier' -- only a letter-bearing token (E4) or hyphenated digit
    groups (a part number) should. Without this, an ordinary pronoun
    follow-up that happens to contain a number would wrongly skip
    resolution."""
    history = [
        HistoryTurn(role="user", content="Why is it not heating?"),
        HistoryTurn(role="assistant", content="Check the tank heater and thermistor for failure."),
    ]
    resolved = resolve_follow_up_query("Can I set it to 12?", history)
    assert resolved != "Can I set it to 12?"
    assert "heater" in resolved.lower() or "thermistor" in resolved.lower()


def test_a_two_group_digit_range_is_not_mistaken_for_a_part_number():
    """A two-group, all-digit range ('2-3') is shaped like a short part
    number but is at least as likely an ordinary duration/spec -- must not
    skip resolution for a genuine pronoun follow-up any more than a bare
    number does. A real multi-segment part number (3+ groups, e.g.
    '81-118-31') is unaffected -- see
    test_a_part_number_question_is_unchanged_even_with_a_pronoun."""
    history = [
        HistoryTurn(role="user", content="Why is it not heating?"),
        HistoryTurn(role="assistant", content="Check the tank heater and thermistor for failure."),
    ]
    resolved = resolve_follow_up_query("Should I run it for 2-3 minutes?", history)
    assert resolved != "Should I run it for 2-3 minutes?"
    assert "heater" in resolved.lower() or "thermistor" in resolved.lower()


def test_an_ordinal_is_not_mistaken_for_an_identifier():
    """'2nd' has a letter+digit mix the same shape as 'E4' -- but it's a
    word-position marker, not an identifier, and must not stop a genuine
    pronoun follow-up from resolving."""
    history = [
        HistoryTurn(role="user", content="Why is it not heating?"),
        HistoryTurn(role="assistant", content="Check the tank heater and thermistor for failure."),
    ]
    resolved = resolve_follow_up_query("What about the 2nd one?", history)
    assert resolved != "What about the 2nd one?"
    assert "heater" in resolved.lower() or "thermistor" in resolved.lower()


def test_a_letter_prefixed_hyphenated_code_is_unchanged():
    """'E-04' is the hyphenated counterpart of 'E4' -- just as specific an
    identifier, and must be recognized the same unconditional way."""
    history = [
        HistoryTurn(role="user", content="How do I fix an inlet valve leak?"),
        HistoryTurn(role="assistant", content="Check the inlet valve seals on the water supply line for leaking."),
    ]
    q = "Error E-04"
    assert resolve_follow_up_query(q, history) == q


def test_an_asymmetric_two_group_part_number_is_unchanged():
    """'082-6302' (short first group, long second group) is this corpus's
    dominant two-group part-number shape -- a bare mention after an
    unrelated turn must stand on its own, the same as a 3+-group part
    number, not get diluted with the prior topic's words."""
    history = [
        HistoryTurn(role="user", content="How do I fix an inlet valve leak?"),
        HistoryTurn(role="assistant", content="Check the inlet valve seals on the water supply line for leaking."),
    ]
    q = "082-6302"
    assert resolve_follow_up_query(q, history) == q


def test_a_true_pronoun_follow_up_with_no_identifier_still_resolves():
    """Confirms the identifier check is narrow, not a blanket exemption for
    anything short -- a genuine pronoun follow-up with no code/part number
    of its own must still resolve exactly as before."""
    history = [
        HistoryTurn(role="user", content="Why is it not heating?"),
        HistoryTurn(role="assistant", content="Check the tank heater and thermistor for failure."),
    ]
    resolved = resolve_follow_up_query("Where is it?", history)
    assert resolved != "Where is it?"
    assert "heater" in resolved.lower() or "thermistor" in resolved.lower()


def test_resolution_walks_back_past_a_failed_retry_to_a_real_prior_answer():
    """Turn 1 answered for real; turn 2 was a failed retry attempt (same
    conversation, e.g. a transient provider error); turn 3 is a follow-up.
    Resolution must skip the failed turn 2 and use turn 1's real content,
    not fall back to unresolved just because the MOST RECENT assistant turn
    happened to fail."""
    history = [
        HistoryTurn(role="user", content="Why is it not heating?"),
        HistoryTurn(
            role="assistant", content="Check the tank heater and thermistor for failure.",
            is_no_answer=False,
        ),
        HistoryTurn(role="user", content="What about replacing it?"),
        HistoryTurn(
            role="assistant", content="I couldn't reach the AI provider (timed out).",
            is_no_answer=True,
        ),
    ]
    resolved = resolve_follow_up_query("Which connector?", history)
    assert resolved != "Which connector?"
    assert "heater" in resolved.lower() or "thermistor" in resolved.lower()
    assert "provider" not in resolved.lower() and "timed" not in resolved.lower()

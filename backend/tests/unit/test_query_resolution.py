"""P1-8 (independent follow-up review): "Resolve follow-ups into a stored
standalone retrieval query before hybrid_search." hybrid_search has no
conversational reasoning -- a follow-up like "What about replacing it?"
retrieves on the literal words alone and never finds passages about the
actual antecedent.

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


# --- P1-2 (independent follow-up review, 2026-08-24): negatives -----------
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

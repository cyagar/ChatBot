"""Resolve a conversational follow-up into a standalone retrieval query,
stored before hybrid_search runs; the default provider uses that resolved
query. hybrid_search has no conversational reasoning -- it is Postgres
full-text ranking (ts_rank, not BM25) + cosine similarity over the literal
query text -- so a follow-up like "What about
replacing it?" retrieves on the word "replacing" alone and never finds
passages about the actual antecedent (e.g. "heating element").

This is deterministic and has no model dependency, so it applies identically
to every provider including the no-API-key default (local_extractive). The
providers themselves still receive the ORIGINAL question text plus full
history -- an LLM can resolve "it" from conversational context the same way
a human would -- this module exists only because retrieval cannot.

The resolved query is a heuristic, not a semantic parse: it targets a
three-turn example ("Why is it not heating?" -> "What about replacing it?" ->
"Which connector?") by pulling content words from the most
recent ANSWERED assistant turn (since a pronoun like "it" in a follow-up
typically refers to something the assistant just said, e.g. "replace the
heating element" -- not to whatever the user asked two turns ago) and the
most recent USER turn, then appending them to the original question. It will
not resolve every possible anaphora and is not intended to.

A prior assistant turn that was itself a no-answer/failure message ("I
couldn't find...", "I couldn't reach the AI provider...") is skipped when
looking for the "most recent assistant turn" -- its prose is boilerplate, not
domain content, and scraping it would inject retrieval-irrelevant words (e.g.
"technical", "administrator") into the resolved query. Resolution walks back
through history for the most recent assistant turn that actually answered
something; if none exists, that is treated as low confidence and the question
is returned unchanged rather than guessed at -- retrieving on the
technician's own words is safer than retrieving on noise. This is the
module's whole confidence model: "resolved" (an antecedent was found) vs
"unresolved" (returned unchanged), observable by the caller via
`!= question`. A third case is also "unresolved": a question that already
names its own error code or part number (see _has_identifier) is never
treated as a follow-up at all, even with a pronoun or cue phrase present --
it is specific enough to retrieve on by itself, and appending a prior
turn's words would only dilute Postgres's OR-ranking across more terms.
What this does NOT do: extract or persist structured fields
for "referenced component/procedure" or "unresolved pronoun" as their own DB
columns, or surface a clarification prompt to the technician when resolution
fails -- those would need a genuine NLP/entity-extraction step or new UI.
"""
from __future__ import annotations

import re

from app.identifiers import is_identifier_token

_FOLLOWUP_CUE_RE = re.compile(
    r"^(what about|and what about|and|also|what if|how about|ok(ay)?,?\s+what about)\b",
    re.IGNORECASE,
)
_PRONOUN_RE = re.compile(r"\b(it|this|that|these|those|them|its)\b", re.IGNORECASE)
_STOPWORDS = {
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "what", "which", "who", "how", "why", "when", "where", "do", "does",
    "did", "can", "could", "should", "would", "will", "shall", "may",
    "might", "must", "about", "for", "and", "or", "but", "if", "then",
    "than", "with", "without", "from", "into", "onto", "this", "that",
    "these", "those", "it", "its", "them", "they", "you", "your", "i",
    "not", "no", "yes", "please", "on", "of", "to", "in", "at", "as",
}
_SHORT_QUESTION_WORD_THRESHOLD = 4

# A bare number ("12", "2026") or an ordinary two-group range/duration/spec
# ("2-3", "120-240") must NOT count as this question "naming its own
# identifier" and skip resolution for what's actually an ordinary pronoun
# follow-up ("Can I set it to 12?", "Should I run it for 2-3 minutes?") --
# unlike extractive.py's code-token rescue, this module has no surrounding
# "error/part-number intent" concept to conditionally allow either shape,
# so is_identifier_token's unconditional check (a letter+digit mix, or 3+
# hyphenated groups) is used as-is, never the two ambiguous shapes it
# deliberately excludes. A token like "E4" is only 2 characters, below
# _content_words' own minimum length, so without this check a question
# like "Error E4" has ONE content word ("Error") and is misjudged as too
# short to stand alone.
def _has_identifier(text: str) -> bool:
    words = re.findall(r"[A-Za-z0-9-]+", text)
    return any(len(w) >= 2 and is_identifier_token(w) for w in words)


def _content_words(text: str, limit: int) -> list[str]:
    words = re.findall(r"[A-Za-z][A-Za-z0-9/-]{2,}", text)
    out: list[str] = []
    seen: set[str] = set()
    for w in words:
        lw = w.lower()
        if lw in _STOPWORDS or lw in seen:
            continue
        seen.add(lw)
        out.append(w)
        if len(out) >= limit:
            break
    return out


def _looks_like_follow_up(question: str) -> bool:
    stripped = question.strip()
    # A question naming its own error code or part number is already
    # specific enough to retrieve on -- appending a prior turn's words
    # would only dilute Postgres's OR-ranking across more terms, pulling
    # the ranking away from the one exact identifier that actually matters.
    # This overrides a pronoun or cue phrase too: "What about E5?" after an
    # E4 discussion names its OWN target and must not inherit E4's context.
    if _has_identifier(stripped):
        return False
    if _FOLLOWUP_CUE_RE.match(stripped):
        return True
    if _PRONOUN_RE.search(stripped):
        return True
    return len(_content_words(stripped, limit=999)) <= _SHORT_QUESTION_WORD_THRESHOLD


def resolve_follow_up_query(question: str, history: list) -> str:
    """Return a standalone retrieval query. `history` is the same bounded
    list of HistoryTurn passed to the provider (role in {"user","assistant"}).

    A question that doesn't look like a follow-up (has enough of its own
    content words and no continuation cue/pronoun) is returned unchanged --
    resolution only ever adds context, never rewrites a question that
    already stands on its own."""
    if not history or not _looks_like_follow_up(question):
        return question

    last_assistant = next(
        (h.content for h in reversed(history) if h.role == "assistant" and not h.is_no_answer), ""
    )
    last_user = next((h.content for h in reversed(history) if h.role == "user"), "")

    context_words = _content_words(last_assistant, limit=15) + _content_words(last_user, limit=10)
    if not context_words:
        return question

    seen: set[str] = set()
    deduped = []
    for w in context_words:
        lw = w.lower()
        if lw in seen:
            continue
        seen.add(lw)
        deduped.append(w)

    return question.rstrip() + " " + " ".join(deduped)

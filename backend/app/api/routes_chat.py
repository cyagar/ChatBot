from __future__ import annotations

import json
import logging
import re
from datetime import datetime

import psycopg
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field, field_serializer
from rapidfuzz import fuzz

from app.api.common import iso_utc
from app.api.pagination import decode_cursor, paginate, set_pagination_headers
from app.auth.deps import CurrentUser, get_current_user
from app.db import get_conn
from app.providers.base import GeneratedAnswer, HistoryTurn, ProviderError
from app.providers.factory import get_provider
from app.rate_limit import default_limit_string, limiter
from app.retrieval.query_resolution import resolve_follow_up_query
from app.retrieval.search import hybrid_search

router = APIRouter(prefix="/api", tags=["chat"])
logger = logging.getLogger(__name__)

MAX_QUESTION_LEN = 2000
FUZZY_MACHINE_MATCH_THRESHOLD = 85
MAX_HISTORY_TURNS = 8          # prior messages (user+assistant) sent as context
MAX_HISTORY_TURN_CHARS = 800   # bound per-turn size so history can't dominate the prompt


class ConversationOut(BaseModel):
    id: int
    machine_id: int | None
    machine_label: str | None
    title: str | None
    # Postgres TIMESTAMPTZ columns come back from psycopg as real datetimes,
    # not strings -- iso_utc() (app/api/common.py) renders the wire string.
    started_at: datetime
    updated_at: datetime

    @field_serializer("started_at", "updated_at")
    def _ser_ts(self, v: datetime) -> str:
        return iso_utc(v)


class CreateConversationRequest(BaseModel):
    machine_id: int | None = None


class MessageIn(BaseModel):
    content: str = Field(min_length=1, max_length=MAX_QUESTION_LEN)


class CitationOut(BaseModel):
    chunk_id: int
    document_id: int
    filename: str
    title: str | None
    page_number: int | None
    section_heading: str | None
    revision: str | None
    excerpt: str


class MessageOut(BaseModel):
    id: int
    role: str
    content: str
    is_clarifying_question: bool
    is_no_answer: bool
    answer_status: str = "completed"  # pending | completed | failed | retrying
    citations: list[CitationOut] = []
    safety_warnings: list[str] = []
    conflict_note: str | None = None
    clarifying_options: list[dict] = []
    retry_count: int = 0
    created_at: datetime
    # The requesting user's own current feedback/save state, so a client that
    # reloads a conversation (app restart, rotation recreating a ViewModel,
    # just navigating away and back) can show "already marked" instead of
    # resetting to blank buttons and inviting a redundant re-tap. feedback
    # rows are intentionally not deduplicated (see feedback table comment --
    # a technician reconsidering is a real, allowed case), so this reports
    # the MOST RECENT rating, not "whether any feedback exists".
    feedback_rating: str | None = None
    is_saved: bool = False

    @field_serializer("created_at")
    def _ser_ts(self, v: datetime) -> str:
        return iso_utc(v)


def _machine_label(conn, machine_id: int | None) -> str | None:
    if machine_id is None:
        return None
    row = conn.execute(
        "SELECT m.model_name, mf.name AS manufacturer FROM machines m "
        "JOIN manufacturers mf ON mf.id = m.manufacturer_id WHERE m.id = %s",
        (machine_id,),
    ).fetchone()
    return f"{row['manufacturer']} {row['model_name']}" if row else None


def _machine_name_variants(model_name: str) -> list[str]:
    """'AJ/AJX Series' -> ['AJ/AJX Series', 'AJ', 'AJX Series'] so a mention of
    just 'AJ' (a real model designator, not a random substring) still resolves
    unambiguously -- the exact-substring-only match this replaced couldn't
    handle a phrase like 'the AJ machine' (independent review concern #6)."""
    parts = re.split(r"[/,]", model_name)
    variants = [model_name] + [p.strip() for p in parts]
    return [v for v in variants if len(v) >= 2]


def _resolve_machine_mention(question: str) -> tuple[int | None, list[dict]]:
    """Used only when a conversation has no machine selected yet. Never called
    again once a machine is set for a conversation -- a machine change must go
    through the explicit /machine endpoint below, never be inferred from a
    later message (independent review concern #5/#6: no silent machine switch).

    Tries an exact, word-bounded match first (on the full model name or any
    '/'-separated component, plus any curated alias); falls back to
    deterministic fuzzy matching only if nothing matched exactly, so a typo or
    slightly different phrasing ('TF-DBC' vs 'TF DBC') still resolves without
    making bare-substring matching (e.g. a 2-letter code) looser than it
    already is."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT m.id, m.model_name, m.aliases, mf.name AS manufacturer FROM machines m "
            "JOIN manufacturers mf ON mf.id = m.manufacturer_id"
        ).fetchall()
    q_lower = question.lower()

    exact_matches = []
    fuzzy_matches = []
    for r in rows:
        try:
            aliases = [a for a in json.loads(r["aliases"] or "[]") if isinstance(a, str)]
        except (TypeError, ValueError):
            aliases = []
        names = _machine_name_variants(r["model_name"]) + aliases

        if any(re.search(rf"\b{re.escape(n.lower())}\b", q_lower) for n in names):
            exact_matches.append(r)
            continue

        best = max((fuzz.token_set_ratio(q_lower, n.lower()) for n in names), default=0)
        if best >= FUZZY_MACHINE_MATCH_THRESHOLD:
            fuzzy_matches.append(r)

    matches = exact_matches or fuzzy_matches
    if len(matches) == 1:
        return matches[0]["id"], []
    if len(matches) > 1:
        return None, [
            {"id": r["id"], "label": f"{r['manufacturer']} {r['model_name']}"} for r in matches[:5]
        ]
    return None, []


@router.post("/conversations", response_model=ConversationOut, status_code=status.HTTP_201_CREATED)
def create_conversation(payload: CreateConversationRequest, user: CurrentUser = Depends(get_current_user)):
    with get_conn() as conn:
        if payload.machine_id is not None:
            exists = conn.execute("SELECT id FROM machines WHERE id = %s", (payload.machine_id,)).fetchone()
            if not exists:
                raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Machine not found.")
        cur = conn.execute(
            "INSERT INTO conversations (user_id, machine_id) VALUES (%s, %s) RETURNING id",
            (user.id, payload.machine_id),
        )
        conv_id = cur.fetchone()["id"]
        row = conn.execute(
            "SELECT id, machine_id, title, started_at, updated_at FROM conversations WHERE id = %s",
            (conv_id,),
        ).fetchone()
        label = _machine_label(conn, row["machine_id"])
    return ConversationOut(
        id=row["id"], machine_id=row["machine_id"], machine_label=label,
        title=row["title"], started_at=row["started_at"], updated_at=row["updated_at"],
    )


def _conversation_title(conn, conversation_id: int, stored_title: str | None) -> str | None:
    """P1-3 (2026-08-24 independent follow-up review): nothing writes
    conversations.title -- it has always been NULL for every conversation
    that exists, which would make a history list unusable (every row blank).
    Derive one from the first user message when no stored title exists,
    rather than building a title-generation feature that isn't what this
    item asked for."""
    if stored_title:
        return stored_title
    row = conn.execute(
        "SELECT content FROM messages WHERE conversation_id = %s AND role = 'user' "
        "ORDER BY id ASC LIMIT 1",
        (conversation_id,),
    ).fetchone()
    if not row:
        return None
    content = row["content"].strip()
    return content if len(content) <= 80 else content[:79].rstrip() + "…"


@router.get("/conversations", response_model=list[ConversationOut])
def list_conversations(
    response: Response,
    user: CurrentUser = Depends(get_current_user),
    limit: int = Query(default=20, ge=1, le=100),
    cursor: str | None = None,
):
    # Cursor pagination (Phase 1, narrowed scope): (updated_at, id) rather
    # than updated_at alone, since two conversations can share an
    # updated_at (same-second activity) -- id as a tiebreaker is what makes
    # this "stable" (a page boundary can't land mid-tie and skip/repeat a
    # row) rather than plain LIMIT/OFFSET, which also shifts under
    # concurrent inserts.
    before_updated_at, before_id = decode_cursor(cursor, 2) if cursor else (None, None)
    with get_conn() as conn:
        # create_conversation runs the moment a technician taps a machine (or
        # "Not sure which machine?") -- before any question is typed, so the
        # conversation row exists even if they back out without asking
        # anything. Every real question always inserts the user's message
        # first (ask_question, above), so "has at least one message" is
        # exactly "a question was actually asked" -- excluding conversations
        # with none keeps abandoned/empty ones out of History (reported live
        # on the tablet, 2026-08-25).
        rows = conn.execute(
            "SELECT id, machine_id, title, started_at, updated_at FROM conversations "
            "WHERE user_id = %s AND EXISTS ("
            "    SELECT 1 FROM messages WHERE messages.conversation_id = conversations.id"
            ") AND (%s::timestamptz IS NULL OR updated_at < %s OR (updated_at = %s AND id < %s)) "
            "ORDER BY updated_at DESC, id DESC LIMIT %s",
            (user.id, before_updated_at, before_updated_at, before_updated_at, before_id, limit + 1),
        ).fetchall()
        rows, next_cursor = paginate(rows, limit, lambda r: (r["updated_at"], r["id"]))
        set_pagination_headers(response, next_cursor)
        out = []
        for r in rows:
            label = _machine_label(conn, r["machine_id"])
            out.append(ConversationOut(
                id=r["id"], machine_id=r["machine_id"], machine_label=label,
                title=_conversation_title(conn, r["id"], r["title"]),
                started_at=r["started_at"], updated_at=r["updated_at"],
            ))
    return out


def _require_own_conversation(conn, conversation_id: int, user_id: int):
    row = conn.execute(
        "SELECT id, user_id, machine_id, pending_message_id FROM conversations WHERE id = %s",
        (conversation_id,),
    ).fetchone()
    if not row or row["user_id"] != user_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Conversation not found.")
    return row


def _claim_conversation_processing(conn, conversation_id: int) -> bool:
    """Owner decision (2026-09-16): concurrent questions in one conversation
    are not supported -- a technician must wait for the in-flight question to
    finish (or the retry it, since a retry also calls the provider) before
    sending another, and this must be enforced server-side rather than only
    by disabling a client button. Same claim-UPDATE pattern used throughout
    this file (pending_message_id, retry's answer_status, idempotency keys):
    only the request that flips is_processing false->true may proceed."""
    result = conn.execute(
        "UPDATE conversations SET is_processing = true WHERE id = %s AND is_processing = false",
        (conversation_id,),
    )
    return result.rowcount > 0


def _release_conversation_processing(conversation_id: int) -> None:
    """Always called in a finally, on its own connection, AFTER the claiming
    `with get_conn()` block has already exited (and so already committed) --
    ask_question/retry_answer/set_conversation_machine all do their provider
    call outside that block, and this must run even when
    _generate_and_persist_answer raises, or the conversation would be stuck
    rejecting every future question with 409 forever.

    NEVER call this from inside a still-open `with get_conn()` block that
    claimed the lock (an early-return branch that hasn't reached the end of
    its own `with get_conn()` yet): that connection's transaction is still
    open and still holds the row lock this function's own fresh connection
    would need, and since nothing else will ever come release it (it's the
    same thread, waiting on itself), the second connection blocks forever.
    Release with `conn.execute("UPDATE conversations SET is_processing = "
    "false WHERE id = %s", (conversation_id,))` on the SAME `conn` instead in
    that situation -- see ask_question's clarifying-question branch for the
    pattern."""
    with get_conn() as conn:
        conn.execute("UPDATE conversations SET is_processing = false WHERE id = %s", (conversation_id,))


def _fetch_history(conn, conversation_id: int, *, before_message_id: int | None = None) -> list[HistoryTurn]:
    """Bounded prior turns, oldest first. Clarifying-question prompts are
    excluded -- they're navigation, not content a provider should reason
    about. `before_message_id` lets the pending-message resumption path
    (set_conversation_machine) compute exactly the history that existed at
    the moment the pending question was originally asked, the same way the
    normal ask_question path computes history before inserting its new
    question (concern #5, P1-8)."""
    if before_message_id is not None:
        rows = conn.execute(
            "SELECT role, content, is_no_answer FROM messages WHERE conversation_id = %s AND id < %s "
            "AND is_clarifying_question = false ORDER BY id DESC LIMIT %s",
            (conversation_id, before_message_id, MAX_HISTORY_TURNS),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT role, content, is_no_answer FROM messages WHERE conversation_id = %s "
            "AND is_clarifying_question = false ORDER BY id DESC LIMIT %s",
            (conversation_id, MAX_HISTORY_TURNS),
        ).fetchall()
    return [
        HistoryTurn(
            role=r["role"], content=r["content"][:MAX_HISTORY_TURN_CHARS],
            is_no_answer=bool(r["is_no_answer"]),
        )
        for r in reversed(rows)
    ]


def _generate_and_persist_answer(
    conversation_id: int,
    user_message_id: int,
    question: str,
    machine_id: int,
    history: list[HistoryTurn],
    *,
    retry_message_id: int | None = None,
) -> MessageOut:
    """Shared by ask_question (a freshly-asked question), set_conversation_machine's
    pending-message resumption (P1-8: "confirming a machine must resume the
    existing pending message" rather than the caller re-submitting the same
    question as a new user turn), and retry_answer (P1-1, 2026-08-24 independent
    follow-up review: "retry must not resend the question as a new message").
    Retrieval uses the resolved standalone query; the provider still sees the
    question's original wording plus `history` -- an LLM can resolve a
    pronoun like "it" from conversational context the same way a human
    would, so only retrieval (which has no such reasoning) needs the
    resolved query.

    retry_message_id: when set, this is a retry -- the existing assistant
    message at that id is UPDATED in place (its old message_sources rows
    replaced) instead of a new message being INSERTed, so a retry never adds
    a second assistant turn or a duplicate user turn to the conversation."""
    with get_conn() as conn:
        machine_label = _machine_label(conn, machine_id)

    resolved_query = resolve_follow_up_query(question, history)
    if resolved_query != question:
        with get_conn() as conn:
            conn.execute(
                "UPDATE messages SET resolved_query = %s WHERE id = %s", (resolved_query, user_message_id)
            )

    # Retrieval itself can fail independently of the provider call below.
    # vector_search() already skips embed_query() entirely when there are no
    # eligible chunks (P1-5's first half), and separately swallows an
    # embed_query() failure (e.g. the embedding model not loading -- a
    # misconfigured or offline deployment; see get_model()'s docstring) to
    # degrade to lexical-only results rather than raising, so that specific
    # case never reaches here at all. This is the backstop for retrieval
    # failing more fundamentally than that (e.g. the FTS index itself, or an
    # unexpected bug in fusion/hydration) -- it must not become an unhandled
    # 500 that leaves the technician's question answered by nothing. It gets
    # the same honest, no-answer treatment as a provider failure below, not a
    # misleading "no relevant passages were found" (that specific wording
    # would claim a search concluded when one never ran) and not a stack
    # trace (concern #9).
    try:
        passages = hybrid_search(resolved_query, machine_id=machine_id, top_k=6)
    except Exception:
        logger.exception("Retrieval failed for conversation %s", conversation_id)
        passages = []
        answer_status = "failed"
        result = GeneratedAnswer(
            answer="I couldn't search the manuals right now due to a temporary technical "
            "problem. Please try again in a moment, or contact an administrator if this "
            "keeps happening.",
            is_no_answer=True, provider="none",
        )
    else:
        provider = get_provider()
        answer_status = "completed"
        try:
            result = provider.generate(question, machine_label, passages, history=history)
        except ProviderError as e:
            logger.warning("Provider call failed for conversation %s: %s", conversation_id, e)
            answer_status = "failed"
            result = GeneratedAnswer(
                answer=f"I couldn't reach the AI provider ({e}). Please try again in a moment.",
                is_no_answer=True, provider=getattr(provider, "name", "unknown"),
            )
        except Exception:
            # Never leak internals (concern #9) -- but do log server-side so an
            # admin can actually diagnose what happened.
            logger.exception("Unexpected error generating an answer for conversation %s", conversation_id)
            answer_status = "failed"
            result = GeneratedAnswer(
                answer="Something went wrong while generating an answer. Please try again.",
                is_no_answer=True, provider=getattr(provider, "name", "unknown"),
            )

    # Order-preservingly deduplicate citations before BOTH the response and
    # persistence (P1-7). The built-in providers already dedupe, but a
    # duplicate chunk_id from any provider would otherwise collapse silently
    # on the persistence side (dict keyed by chunk_id) while still appearing
    # twice in the live response -- i.e. live and reload would disagree.
    seen_citation_chunks: set[int] = set()
    deduped_citations = []
    for c in result.citations:
        if c.chunk_id in seen_citation_chunks:
            continue
        seen_citation_chunks.add(c.chunk_id)
        deduped_citations.append(c)
    result.citations = deduped_citations

    with get_conn() as conn:
        if retry_message_id is not None:
            # Replace this message's own prior sources -- a retry's new
            # passages/citations must not be appended alongside the failed
            # attempt's, which could otherwise resurrect a source the new
            # attempt never actually cited.
            conn.execute("DELETE FROM message_sources WHERE message_id = %s", (retry_message_id,))
            conn.execute(
                "UPDATE messages SET content = %s, is_no_answer = %s, machine_id = %s, "
                "safety_warnings = %s, conflict_note = %s, provider = %s, answer_status = %s, "
                "retry_count = retry_count + 1 WHERE id = %s",
                (
                    result.answer, result.is_no_answer, machine_id,
                    json.dumps(result.safety_warnings) if result.safety_warnings else None,
                    result.conflict_note, result.provider, answer_status, retry_message_id,
                ),
            )
            msg_id = retry_message_id
        else:
            cur = conn.execute(
                "INSERT INTO messages (conversation_id, role, content, is_no_answer, machine_id, "
                "safety_warnings, conflict_note, provider, answer_status) "
                "VALUES (%s, 'assistant', %s, %s, %s, %s, %s, %s, %s) RETURNING id",
                (
                    conversation_id, result.answer, result.is_no_answer, machine_id,
                    json.dumps(result.safety_warnings) if result.safety_warnings else None,
                    result.conflict_note, result.provider, answer_status,
                ),
            )
            msg_id = cur.fetchone()["id"]
        citation_excerpt_by_chunk = {c.chunk_id: c.excerpt for c in result.citations}
        # Provider citation order, not retrieval order. `rank` keeps meaning
        # retrieval rank (for retrieval-quality auditing); citation_ordinal
        # records the order the provider actually cited them so a reloaded
        # conversation reproduces exactly what was displayed live -- these two
        # orders differ, which is what P1-7 flagged.
        citation_ordinal_by_chunk = {c.chunk_id: i for i, c in enumerate(result.citations)}
        for rank, p in enumerate(passages):
            is_citation = p.chunk_id in citation_excerpt_by_chunk
            conn.execute(
                "INSERT INTO message_sources (message_id, chunk_id, rank, lexical_score, vector_score, "
                "combined_score, is_citation, excerpt, citation_ordinal) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    msg_id, p.chunk_id, rank, p.lexical_score, p.vector_score, p.combined_score,
                    is_citation, citation_excerpt_by_chunk.get(p.chunk_id),
                    citation_ordinal_by_chunk.get(p.chunk_id),
                ),
            )
        conn.execute("UPDATE conversations SET updated_at = now() WHERE id = %s", (conversation_id,))
        # pending_message_id is already cleared by the caller before this runs
        # -- ask_question clears it unconditionally on any new user turn, and
        # set_conversation_machine claims it atomically before resuming (P1-8)
        # -- so there is nothing left to clear here.
        row = conn.execute(
            "SELECT created_at, retry_count FROM messages WHERE id=%s", (msg_id,)
        ).fetchone()

    return MessageOut(
        id=msg_id, role="assistant", content=result.answer,
        is_clarifying_question=False, is_no_answer=result.is_no_answer,
        answer_status=answer_status,
        citations=[
            CitationOut(chunk_id=c.chunk_id, document_id=c.document_id, filename=c.filename,
                        title=c.title, page_number=c.page_number, section_heading=c.section_heading,
                        revision=c.revision, excerpt=c.excerpt)
            for c in result.citations
        ],
        safety_warnings=result.safety_warnings,
        conflict_note=result.conflict_note,
        retry_count=row["retry_count"],
        created_at=row["created_at"],
    )


class SetMachineRequest(BaseModel):
    machine_id: int


@router.post("/conversations/{conversation_id}/machine", response_model=ConversationOut)
def set_conversation_machine(
    conversation_id: int, payload: SetMachineRequest, user: CurrentUser = Depends(get_current_user)
):
    """The ONLY way a conversation's machine is set once clarification is
    needed, or changed later ("Change machine"). This is always an explicit,
    confirmed technician action -- never inferred from a later message body,
    which is what let a conversation's machine silently drift in the reviewed
    version (concern #5/#6).

    If a clarifying question is pending (the technician asked something
    before the machine was known), confirming the machine here resumes and
    answers that ORIGINAL stored question -- it does not require the caller
    to resubmit it as a new user turn (P1-8). The resumed answer is
    generated and persisted as usual; this endpoint's own response stays
    ConversationOut either way, so the caller reloads
    GET /conversations/{id}/messages to see it, the same as after any other
    answer."""
    with get_conn() as conn:
        conv = _require_own_conversation(conn, conversation_id, user.id)
        machine = conn.execute("SELECT id FROM machines WHERE id = %s", (payload.machine_id,)).fetchone()
        if not machine:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Machine not found.")
        conn.execute(
            "UPDATE conversations SET machine_id = %s, updated_at = now() WHERE id = %s",
            (payload.machine_id, conversation_id),
        )

        pending_id = conv["pending_message_id"]
        pending_question = None
        pending_history: list[HistoryTurn] | None = None
        if pending_id is not None:
            # Atomically claim the pending message before generating anything.
            # A double-tap on a clarify button (easy on a tablet) fires two
            # concurrent requests that would otherwise both read the same
            # pending_id here and both call the provider -- this UPDATE's WHERE
            # clause is an atomic compare-and-swap (Postgres row-level locking
            # during the UPDATE serializes concurrent writers on this same
            # row), so only one of these UPDATEs can match the row while
            # pending_message_id still equals pending_id; the loser sees
            # rowcount 0 and skips generation entirely instead of producing a
            # second duplicate answer.
            claim = conn.execute(
                "UPDATE conversations SET pending_message_id = NULL "
                "WHERE id = %s AND pending_message_id = %s",
                (conversation_id, pending_id),
            )
            if claim.rowcount == 1:
                # Owner decision (2026-09-16): this resume also does
                # retrieval/provider work, so it must hold the same
                # conversation-level processing lock ask_question does -- an
                # ask_question racing in at exactly this moment must not be
                # able to start a second concurrent provider call. If the
                # lock is already held (shouldn't happen in practice, but
                # would otherwise silently drop this pending question),
                # restore the pending claim so a later request can retry it
                # instead of orphaning it.
                if _claim_conversation_processing(conn, conversation_id):
                    pending_row = conn.execute("SELECT content FROM messages WHERE id = %s", (pending_id,)).fetchone()
                    if pending_row is not None:
                        pending_question = pending_row["content"]
                        pending_history = _fetch_history(conn, conversation_id, before_message_id=pending_id)
                    else:
                        # Same-connection release -- see the matching comment
                        # in ask_question's clarifying-question branch.
                        conn.execute(
                            "UPDATE conversations SET is_processing = false WHERE id = %s", (conversation_id,)
                        )
                else:
                    conn.execute(
                        "UPDATE conversations SET pending_message_id = %s WHERE id = %s",
                        (pending_id, conversation_id),
                    )

        row = conn.execute(
            "SELECT id, machine_id, title, started_at, updated_at FROM conversations WHERE id = %s",
            (conversation_id,),
        ).fetchone()
        label = _machine_label(conn, row["machine_id"])

    if pending_question is not None:
        try:
            _generate_and_persist_answer(
                conversation_id, pending_id, pending_question, payload.machine_id, pending_history or []
            )
        finally:
            _release_conversation_processing(conversation_id)

    return ConversationOut(
        id=row["id"], machine_id=row["machine_id"], machine_label=label,
        title=row["title"], started_at=row["started_at"], updated_at=row["updated_at"],
    )


def _hydrate_message(conn, row, user_id: int) -> MessageOut:
    # Only rows the provider actually selected (is_citation=1) -- every
    # retrieved passage is still kept in message_sources for retrieval-quality
    # auditing, but reload must reproduce exactly what the technician saw, not
    # every candidate that was merely retrieved (concern #7).
    citations = []
    src_rows = conn.execute(
        "SELECT ms.chunk_id, ms.excerpt, c.document_id, d.original_filename, d.title, "
        "c.page_number, c.section_heading, d.revision "
        "FROM message_sources ms "
        "JOIN chunks c ON c.id = ms.chunk_id "
        "JOIN documents d ON d.id = c.document_id "
        # Provider citation order (P1-7), NOT retrieval rank -- reload must
        # reproduce exactly the order the technician originally saw, so the
        # citation numbering still lines up with the answer's own claims.
        # COALESCE keeps pre-0004 rows (citation_ordinal NULL) ordering by
        # rank, their historical behavior, rather than arbitrarily.
        "WHERE ms.message_id = %s AND ms.is_citation = true "
        "ORDER BY COALESCE(ms.citation_ordinal, ms.rank), ms.rank",
        (row["id"],),
    ).fetchall()
    for s in src_rows:
        citations.append(CitationOut(
            chunk_id=s["chunk_id"], document_id=s["document_id"], filename=s["original_filename"],
            title=s["title"], page_number=s["page_number"], section_heading=s["section_heading"],
            revision=s["revision"], excerpt=s["excerpt"] or "",
        ))

    try:
        safety_warnings = json.loads(row["safety_warnings"]) if row["safety_warnings"] else []
    except (TypeError, ValueError):
        safety_warnings = []

    clarifying_options = []
    if "clarifying_options" in row.keys() and row["clarifying_options"]:
        try:
            clarifying_options = json.loads(row["clarifying_options"])
        except (TypeError, ValueError):
            clarifying_options = []

    feedback_row = conn.execute(
        "SELECT rating FROM feedback WHERE message_id = %s AND user_id = %s "
        "ORDER BY created_at DESC, id DESC LIMIT 1",
        (row["id"], user_id),
    ).fetchone()
    is_saved = conn.execute(
        "SELECT 1 FROM saved_answers WHERE message_id = %s AND user_id = %s LIMIT 1",
        (row["id"], user_id),
    ).fetchone() is not None

    return MessageOut(
        id=row["id"], role=row["role"], content=row["content"],
        is_clarifying_question=bool(row["is_clarifying_question"]),
        is_no_answer=bool(row["is_no_answer"]),
        answer_status=row["answer_status"] if "answer_status" in row.keys() else "completed",
        citations=citations,
        safety_warnings=safety_warnings,
        conflict_note=row["conflict_note"] if "conflict_note" in row.keys() else None,
        clarifying_options=clarifying_options,
        retry_count=row["retry_count"] if "retry_count" in row.keys() else 0,
        created_at=row["created_at"],
        feedback_rating=feedback_row["rating"] if feedback_row else None,
        is_saved=is_saved,
    )


@router.get("/conversations/{conversation_id}/messages", response_model=list[MessageOut])
def get_messages(
    conversation_id: int,
    response: Response,
    user: CurrentUser = Depends(get_current_user),
    limit: int = Query(default=500, ge=1, le=2000),
    cursor: str | None = None,
):
    # Default limit is generous (real conversations in this app today are
    # nowhere near 500 messages) so an existing caller that never passes
    # limit/cursor keeps getting exactly what it always did -- a full
    # conversation in one response. Oldest-first (id ASC), so the cursor
    # pages forward: "id" alone is a stable, already-unique sort key here,
    # no tiebreaker column needed the way updated_at needed one above.
    (after_id,) = decode_cursor(cursor, 1) if cursor else (None,)
    with get_conn() as conn:
        _require_own_conversation(conn, conversation_id, user.id)
        rows = conn.execute(
            "SELECT id, role, content, is_clarifying_question, is_no_answer, "
            "safety_warnings, conflict_note, answer_status, clarifying_options, retry_count, created_at "
            "FROM messages WHERE conversation_id = %s AND (%s::integer IS NULL OR id > %s) ORDER BY id LIMIT %s",
            (conversation_id, after_id, after_id, limit + 1),
        ).fetchall()
        rows, next_cursor = paginate(rows, limit, lambda r: (r["id"],))
        set_pagination_headers(response, next_cursor)
        return [_hydrate_message(conn, r, user.id) for r in rows]


def _message_by_idempotency_key(conn, conversation_id: int, idempotency_key: str):
    return conn.execute(
        "SELECT id FROM messages WHERE conversation_id = %s AND idempotency_key = %s AND role = 'user'",
        (conversation_id, idempotency_key),
    ).fetchone()


def _reply_to_user_message(conn, conversation_id: int, user_message_id: int):
    """The assistant (or clarifying-question) message immediately following a
    given user turn, if one has been persisted yet. There's no explicit
    reply-to column -- ordering is the same contract _fetch_history and
    retry_answer's "preceding user message" lookup already rely on."""
    return conn.execute(
        "SELECT id, role, content, is_clarifying_question, is_no_answer, "
        "safety_warnings, conflict_note, answer_status, clarifying_options, retry_count, created_at "
        "FROM messages WHERE conversation_id = %s AND role = 'assistant' AND id > %s "
        "ORDER BY id ASC LIMIT 1",
        (conversation_id, user_message_id),
    ).fetchone()


def _idempotent_replay(conn, conversation_id: int, user_message_id: int, user_id: int) -> MessageOut:
    """Called once a duplicate Idempotency-Key has been identified (either by
    the pre-check or by losing the UNIQUE-index race on insert). Plan sec 9:
    "A duplicate key ... returns the original result, not another user
    message." If the original attempt hasn't produced a reply yet -- still
    generating, or the process died mid-attempt -- there is nothing to
    replay; 409 rather than silently starting a second provider call for the
    same question (that second call is exactly the hazard this exists to
    prevent). This is a known, accepted gap versus the plan's full durable
    -attempt design (sec 5.1/9), which would let the client resume the
    original attempt instead of dead-ending here -- that needs the
    Postgres/queue migration and is out of scope for this change."""
    reply = _reply_to_user_message(conn, conversation_id, user_message_id)
    if reply is not None:
        return _hydrate_message(conn, reply, user_id)
    raise HTTPException(
        status.HTTP_409_CONFLICT,
        detail="A request with this idempotency key is already being processed.",
    )


@router.post("/conversations/{conversation_id}/messages", response_model=MessageOut)
@limiter.limit(default_limit_string)
def ask_question(
    conversation_id: int,
    payload: MessageIn,
    request: Request,
    user: CurrentUser = Depends(get_current_user),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    question = payload.content.strip()
    if not question:
        raise HTTPException(422, detail="Question cannot be empty.")
    # Absurdly long values aren't a real key from any client we control --
    # treat them as absent rather than storing them.
    if idempotency_key is not None and (not idempotency_key.strip() or len(idempotency_key) > 128):
        idempotency_key = None

    with get_conn() as conn:
        conv = _require_own_conversation(conn, conversation_id, user.id)
        machine_id = conv["machine_id"]

        if idempotency_key is not None:
            existing = _message_by_idempotency_key(conn, conversation_id, idempotency_key)
            if existing is not None:
                return _idempotent_replay(conn, conversation_id, existing["id"], user.id)

        # Owner decision (2026-09-16): concurrent questions in one
        # conversation are not supported -- a technician must wait for (or
        # stop) an in-flight question before asking another, and this must be
        # enforced server-side, not only by a disabled client button. Claimed
        # before the user message is even inserted, so a rejected second
        # question never creates a turn.
        if not _claim_conversation_processing(conn, conversation_id):
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail="Another question is still being answered in this conversation. "
                "Wait for it to finish, or stop it, before asking another.",
            )

        # Bounded prior turns, captured before this question is inserted, so
        # follow-ups like "what about replacing it?" have real context instead
        # of only ever seeing the latest question in isolation (concern #5).
        history = _fetch_history(conn, conversation_id)

        try:
            # A nested transaction (SAVEPOINT under the connection's already
            # -open outer transaction) -- unlike sqlite3, a Postgres
            # constraint violation aborts the whole transaction until a
            # ROLLBACK, so without this savepoint the idempotency-key lookup
            # in the except block below would itself fail with
            # InFailedSqlTransaction instead of running.
            with conn.transaction():
                cur = conn.execute(
                    "INSERT INTO messages (conversation_id, role, content, idempotency_key) "
                    "VALUES (%s, 'user', %s, %s) RETURNING id",
                    (conversation_id, question, idempotency_key),
                )
                user_message_id = cur.fetchone()["id"]
        except psycopg.errors.UniqueViolation:
            # Lost a race against a concurrent request carrying the same key
            # -- the pre-check above is a fast path, not the safety
            # mechanism; the UNIQUE index on (conversation_id,
            # idempotency_key) is. The winner's user message is now visible.
            # (In practice the processing-lock claim above already serializes
            # same-conversation requests, so this branch is now mostly a
            # defensive fallback rather than the primary safety net it used
            # to be.)
            # Same-connection release (not _release_conversation_processing --
            # see the comment on that helper): this except block runs inside
            # the still-open outer transaction that claimed the lock, which
            # hasn't committed yet, so a second connection would block
            # forever waiting on a lock this one hasn't released.
            existing = _message_by_idempotency_key(conn, conversation_id, idempotency_key)
            if existing is None:
                conn.execute("UPDATE conversations SET is_processing = false WHERE id = %s", (conversation_id,))
                raise  # not actually a key collision -- some other integrity error
            conn.execute("UPDATE conversations SET is_processing = false WHERE id = %s", (conversation_id,))
            return _idempotent_replay(conn, conversation_id, existing["id"], user.id)

        # A new user turn always supersedes any earlier pending clarification
        # (P1-8): if the technician typed a fresh question instead of picking
        # a machine from the clarifying options, the old pending question is
        # abandoned, not silently resumed later. If THIS question also fails
        # to resolve a machine, the branch below sets pending_message_id to
        # this new message instead.
        conn.execute(
            "UPDATE conversations SET pending_message_id = NULL WHERE id = %s", (conversation_id,)
        )

        # --- Clarify instead of guessing when the machine is unclear ---
        if machine_id is None:
            resolved_id, candidates = _resolve_machine_mention(question)
            if resolved_id is not None:
                machine_id = resolved_id
                conn.execute("UPDATE conversations SET machine_id = %s WHERE id = %s", (machine_id, conversation_id))
            else:
                clarifying_text = (
                    "Which machine are you working on? "
                    + (
                        "I found a few possible matches: " + ", ".join(c["label"] for c in candidates) + "."
                        if candidates
                        else "Please select a manufacturer and model before I search the manuals."
                    )
                )
                cur = conn.execute(
                    "INSERT INTO messages (conversation_id, role, content, is_clarifying_question, "
                    "clarifying_options) VALUES (%s, 'assistant', %s, true, %s) RETURNING id",
                    (conversation_id, clarifying_text, json.dumps(candidates)),
                )
                msg_id = cur.fetchone()["id"]
                conn.execute(
                    "UPDATE conversations SET updated_at = now(), pending_message_id = %s WHERE id = %s",
                    (user_message_id, conversation_id),
                )
                # Waiting on the technician to pick a machine, not on the
                # provider -- must not hold the lock indefinitely (would block
                # the already-supported "ask something else instead" case;
                # see test_asking_a_new_question_clears_a_stale_pending_clarification).
                # Released on THIS still-open connection, not via
                # _release_conversation_processing -- that helper opens a
                # separate connection, which would block forever waiting on
                # the row lock this transaction hasn't committed (and won't,
                # until this function returns) yet.
                conn.execute("UPDATE conversations SET is_processing = false WHERE id = %s", (conversation_id,))
                return MessageOut(
                    id=msg_id, role="assistant", content=clarifying_text,
                    is_clarifying_question=True, is_no_answer=False,
                    clarifying_options=candidates,
                    created_at=conn.execute("SELECT created_at FROM messages WHERE id=%s", (msg_id,)).fetchone()["created_at"],
                )

    try:
        return _generate_and_persist_answer(conversation_id, user_message_id, question, machine_id, history)
    finally:
        _release_conversation_processing(conversation_id)


@router.post("/conversations/{conversation_id}/messages/{message_id}/retry", response_model=MessageOut)
@limiter.limit(default_limit_string)
def retry_answer(
    conversation_id: int,
    message_id: int,
    request: Request,
    user: CurrentUser = Depends(get_current_user),
):
    """Independent follow-up review 2026-08-24 P1-1: "Retry still resends the
    previous user question as a new message... creating another user turn
    and provider/retrieval attempt." app.js's retry button used to call
    sendQuestion() with the original question text, which is exactly that --
    a second user turn plus a second, unrelated assistant message, doubling
    both the visible history and the billable provider call for what the
    technician experiences as one logical retry.

    This regenerates and updates the SAME failed assistant message in place
    (no new user turn, no new assistant message) -- the original question is
    looked up server-side from the preceding user message, never resent by
    the client, the same "resume the stored question" pattern P1-8 already
    uses for pending-clarification resumption. Idempotent via the same
    claim-UPDATE pattern as conversations.pending_message_id: only a request
    that successfully flips answer_status from 'failed' to 'retrying'
    proceeds to call the provider, so a double-tap on the retry button can
    trigger at most one provider call, not two concurrent ones."""
    with get_conn() as conn:
        conv = _require_own_conversation(conn, conversation_id, user.id)
        row = conn.execute(
            "SELECT id, role, answer_status FROM messages WHERE id = %s AND conversation_id = %s",
            (message_id, conversation_id),
        ).fetchone()
        if not row or row["role"] != "assistant":
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Message not found.")

        claim = conn.execute(
            "UPDATE messages SET answer_status = 'retrying' WHERE id = %s AND answer_status = 'failed'",
            (message_id,),
        )
        if claim.rowcount == 0:
            if row["answer_status"] == "retrying":
                raise HTTPException(status.HTTP_409_CONFLICT,
                                     detail="A retry is already in progress for this answer.")
            raise HTTPException(status.HTTP_409_CONFLICT, detail="Only a failed answer can be retried.")

        # Owner decision (2026-09-16): a retry also calls the provider, so it
        # shares ask_question's conversation-level processing lock -- a fresh
        # question must not be askable while a retry is in flight either.
        if not _claim_conversation_processing(conn, conversation_id):
            conn.execute("UPDATE messages SET answer_status = 'failed' WHERE id = %s", (message_id,))
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail="Another question is still being answered in this conversation. "
                "Wait for it to finish, or stop it, before retrying.",
            )

        user_row = conn.execute(
            "SELECT id, content FROM messages WHERE conversation_id = %s AND role = 'user' AND id < %s "
            "ORDER BY id DESC LIMIT 1",
            (conversation_id, message_id),
        ).fetchone()
        machine_id = conv["machine_id"]
        if user_row is None or machine_id is None:
            # Restore rather than leave the claim stuck at 'retrying' forever
            # -- this should not happen in practice (a failed answer always
            # has a preceding user question and a resolved machine at
            # generation time), but a stuck claim would make every future
            # retry attempt 409 with "already in progress" permanently.
            conn.execute("UPDATE messages SET answer_status = 'failed' WHERE id = %s", (message_id,))
            # Same-connection release -- see the matching comment in
            # ask_question's clarifying-question branch.
            conn.execute("UPDATE conversations SET is_processing = false WHERE id = %s", (conversation_id,))
            raise HTTPException(status.HTTP_409_CONFLICT, detail="This answer cannot be retried.")

        history = _fetch_history(conn, conversation_id, before_message_id=user_row["id"])

    try:
        return _generate_and_persist_answer(
            conversation_id, user_row["id"], user_row["content"], machine_id, history,
            retry_message_id=message_id,
        )
    finally:
        _release_conversation_processing(conversation_id)


class FeedbackRequest(BaseModel):
    rating: str = Field(pattern="^(helpful|incorrect|missing_info)$")
    comment: str | None = Field(default=None, max_length=1000)


_ELIGIBLE_FOR_FEEDBACK_SQL = (
    "SELECT m.id FROM messages m JOIN conversations c ON c.id = m.conversation_id "
    "WHERE m.id = %s AND c.user_id = %s AND m.role = 'assistant' "
    "AND m.answer_status = 'completed' AND m.is_clarifying_question = false AND m.is_no_answer = false"
)


@router.post("/messages/{message_id}/feedback", status_code=status.HTTP_201_CREATED)
def submit_feedback(message_id: int, payload: FeedbackRequest, user: CurrentUser = Depends(get_current_user)):
    """P1-11 (independent follow-up review): used to accept feedback against
    any owned message row -- the user's own question, a clarifying prompt, a
    failed/retrying answer, or a no-answer response -- none of which is a
    real "was this answer helpful" target. Restricted to completed,
    substantive assistant answers."""
    with get_conn() as conn:
        msg = conn.execute(_ELIGIBLE_FOR_FEEDBACK_SQL, (message_id, user.id)).fetchone()
        if not msg:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Message not found.")
        conn.execute(
            "INSERT INTO feedback (message_id, user_id, rating, comment) VALUES (%s, %s, %s, %s)",
            (message_id, user.id, payload.rating, payload.comment),
        )
    return {"ok": True}


@router.post("/messages/{message_id}/save", status_code=status.HTTP_201_CREATED)
def save_answer(message_id: int, user: CurrentUser = Depends(get_current_user)):
    """P1-11: same eligibility restriction as submit_feedback above -- only a
    completed, substantive assistant answer is a meaningful "saved answer";
    a clarifying question, failed attempt, or no-answer row is not."""
    with get_conn() as conn:
        msg = conn.execute(_ELIGIBLE_FOR_FEEDBACK_SQL, (message_id, user.id)).fetchone()
        if not msg:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Message not found.")
        # Unlike feedback (a rating a technician might deliberately resubmit
        # after reconsidering), saving the same answer twice carries no new
        # information -- it's always either a genuine repeat click or a
        # client that lost track of already-saved state (e.g. a rehydrated
        # ChatViewModel that hasn't loaded is_saved yet). ON CONFLICT DO
        # NOTHING against the UNIQUE(user_id, message_id) index (SQLite's
        # INSERT OR IGNORE, ported) makes a duplicate save a no-op instead of
        # a second saved_answers row.
        conn.execute(
            "INSERT INTO saved_answers (user_id, message_id) VALUES (%s, %s) "
            "ON CONFLICT (user_id, message_id) DO NOTHING",
            (user.id, message_id),
        )
    return {"ok": True}


class SavedAnswerOut(BaseModel):
    conversation_id: int
    machine_label: str | None
    question: str | None
    answer: MessageOut


@router.get("/saved-answers", response_model=list[SavedAnswerOut])
def list_saved_answers(
    response: Response,
    user: CurrentUser = Depends(get_current_user),
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = None,
):
    """P1-3 (2026-08-24 independent follow-up review): a saved-answer view is
    useless without knowing which conversation/machine/question it came from
    -- MessageOut alone (the old response shape) carries none of that. Each
    entry now also names which conversation it can be resumed from, so the
    UI can offer "Open conversation" rather than showing an orphaned answer."""
    before_saved_at, before_id = decode_cursor(cursor, 2) if cursor else (None, None)
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT m.id, m.role, m.content, m.is_clarifying_question, m.is_no_answer, "
            "m.safety_warnings, m.conflict_note, m.answer_status, m.retry_count, m.created_at, "
            "m.conversation_id, c.machine_id, sa.saved_at "
            "FROM saved_answers sa "
            "JOIN messages m ON m.id = sa.message_id "
            "JOIN conversations c ON c.id = m.conversation_id "
            "WHERE sa.user_id = %s "
            "AND (%s::timestamptz IS NULL OR sa.saved_at < %s OR (sa.saved_at = %s AND m.id < %s)) "
            "ORDER BY sa.saved_at DESC, m.id DESC LIMIT %s",
            (user.id, before_saved_at, before_saved_at, before_saved_at, before_id, limit + 1),
        ).fetchall()
        rows, next_cursor = paginate(rows, limit, lambda r: (r["saved_at"], r["id"]))
        set_pagination_headers(response, next_cursor)
        out = []
        for r in rows:
            question_row = conn.execute(
                "SELECT content FROM messages WHERE conversation_id = %s AND role = 'user' AND id < %s "
                "ORDER BY id DESC LIMIT 1",
                (r["conversation_id"], r["id"]),
            ).fetchone()
            out.append(SavedAnswerOut(
                conversation_id=r["conversation_id"],
                machine_label=_machine_label(conn, r["machine_id"]),
                question=question_row["content"] if question_row else None,
                answer=_hydrate_message(conn, r, user.id),
            ))
        return out

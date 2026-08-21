from __future__ import annotations

import json
import logging
import re

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from rapidfuzz import fuzz

from app.auth.deps import CurrentUser, get_current_user
from app.db import get_conn
from app.providers.base import GeneratedAnswer, HistoryTurn, ProviderError
from app.providers.factory import get_provider
from app.rate_limit import default_limit_string, limiter
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
    started_at: str
    updated_at: str


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
    answer_status: str = "completed"  # pending | completed | failed
    citations: list[CitationOut] = []
    safety_warnings: list[str] = []
    conflict_note: str | None = None
    clarifying_options: list[dict] = []
    created_at: str


def _machine_label(conn, machine_id: int | None) -> str | None:
    if machine_id is None:
        return None
    row = conn.execute(
        "SELECT m.model_name, mf.name AS manufacturer FROM machines m "
        "JOIN manufacturers mf ON mf.id = m.manufacturer_id WHERE m.id = ?",
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
            exists = conn.execute("SELECT id FROM machines WHERE id = ?", (payload.machine_id,)).fetchone()
            if not exists:
                raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Machine not found.")
        cur = conn.execute(
            "INSERT INTO conversations (user_id, machine_id) VALUES (?, ?)",
            (user.id, payload.machine_id),
        )
        conv_id = cur.lastrowid
        row = conn.execute(
            "SELECT id, machine_id, title, started_at, updated_at FROM conversations WHERE id = ?",
            (conv_id,),
        ).fetchone()
        label = _machine_label(conn, row["machine_id"])
    return ConversationOut(
        id=row["id"], machine_id=row["machine_id"], machine_label=label,
        title=row["title"], started_at=row["started_at"], updated_at=row["updated_at"],
    )


@router.get("/conversations", response_model=list[ConversationOut])
def list_conversations(user: CurrentUser = Depends(get_current_user), limit: int = 20):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, machine_id, title, started_at, updated_at FROM conversations "
            "WHERE user_id = ? ORDER BY updated_at DESC LIMIT ?",
            (user.id, limit),
        ).fetchall()
        out = []
        for r in rows:
            label = _machine_label(conn, r["machine_id"])
            out.append(ConversationOut(
                id=r["id"], machine_id=r["machine_id"], machine_label=label,
                title=r["title"], started_at=r["started_at"], updated_at=r["updated_at"],
            ))
    return out


def _require_own_conversation(conn, conversation_id: int, user_id: int):
    row = conn.execute(
        "SELECT id, user_id, machine_id FROM conversations WHERE id = ?", (conversation_id,)
    ).fetchone()
    if not row or row["user_id"] != user_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Conversation not found.")
    return row


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
    version (concern #5/#6)."""
    with get_conn() as conn:
        _require_own_conversation(conn, conversation_id, user.id)
        machine = conn.execute("SELECT id FROM machines WHERE id = ?", (payload.machine_id,)).fetchone()
        if not machine:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Machine not found.")
        conn.execute(
            "UPDATE conversations SET machine_id = ?, updated_at = datetime('now') WHERE id = ?",
            (payload.machine_id, conversation_id),
        )
        row = conn.execute(
            "SELECT id, machine_id, title, started_at, updated_at FROM conversations WHERE id = ?",
            (conversation_id,),
        ).fetchone()
        label = _machine_label(conn, row["machine_id"])
    return ConversationOut(
        id=row["id"], machine_id=row["machine_id"], machine_label=label,
        title=row["title"], started_at=row["started_at"], updated_at=row["updated_at"],
    )


def _hydrate_message(conn, row) -> MessageOut:
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
        "WHERE ms.message_id = ? AND ms.is_citation = 1 "
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

    return MessageOut(
        id=row["id"], role=row["role"], content=row["content"],
        is_clarifying_question=bool(row["is_clarifying_question"]),
        is_no_answer=bool(row["is_no_answer"]),
        answer_status=row["answer_status"] if "answer_status" in row.keys() else "completed",
        citations=citations,
        safety_warnings=safety_warnings,
        conflict_note=row["conflict_note"] if "conflict_note" in row.keys() else None,
        created_at=row["created_at"],
    )


@router.get("/conversations/{conversation_id}/messages", response_model=list[MessageOut])
def get_messages(conversation_id: int, user: CurrentUser = Depends(get_current_user)):
    with get_conn() as conn:
        _require_own_conversation(conn, conversation_id, user.id)
        rows = conn.execute(
            "SELECT id, role, content, is_clarifying_question, is_no_answer, "
            "safety_warnings, conflict_note, answer_status, created_at "
            "FROM messages WHERE conversation_id = ? ORDER BY id",
            (conversation_id,),
        ).fetchall()
        return [_hydrate_message(conn, r) for r in rows]


@router.post("/conversations/{conversation_id}/messages", response_model=MessageOut)
@limiter.limit(default_limit_string)
def ask_question(
    conversation_id: int,
    payload: MessageIn,
    request: Request,
    user: CurrentUser = Depends(get_current_user),
):
    question = payload.content.strip()
    if not question:
        raise HTTPException(422, detail="Question cannot be empty.")

    with get_conn() as conn:
        conv = _require_own_conversation(conn, conversation_id, user.id)
        machine_id = conv["machine_id"]

        # Bounded prior turns, captured before this question is inserted, so
        # follow-ups like "what about replacing it?" have real context instead
        # of only ever seeing the latest question in isolation (concern #5).
        # Clarifying-question prompts are excluded -- they're navigation, not
        # content a provider should reason about.
        history_rows = conn.execute(
            "SELECT role, content FROM messages WHERE conversation_id = ? "
            "AND is_clarifying_question = 0 ORDER BY id DESC LIMIT ?",
            (conversation_id, MAX_HISTORY_TURNS),
        ).fetchall()
        history = [
            HistoryTurn(role=r["role"], content=r["content"][:MAX_HISTORY_TURN_CHARS])
            for r in reversed(history_rows)
        ]

        conn.execute(
            "INSERT INTO messages (conversation_id, role, content) VALUES (?, 'user', ?)",
            (conversation_id, question),
        )

        # --- Clarify instead of guessing when the machine is unclear ---
        if machine_id is None:
            resolved_id, candidates = _resolve_machine_mention(question)
            if resolved_id is not None:
                machine_id = resolved_id
                conn.execute("UPDATE conversations SET machine_id = ? WHERE id = ?", (machine_id, conversation_id))
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
                    "INSERT INTO messages (conversation_id, role, content, is_clarifying_question) "
                    "VALUES (?, 'assistant', ?, 1)",
                    (conversation_id, clarifying_text),
                )
                msg_id = cur.lastrowid
                conn.execute("UPDATE conversations SET updated_at = datetime('now') WHERE id = ?", (conversation_id,))
                return MessageOut(
                    id=msg_id, role="assistant", content=clarifying_text,
                    is_clarifying_question=True, is_no_answer=False,
                    clarifying_options=candidates,
                    created_at=conn.execute("SELECT created_at FROM messages WHERE id=?", (msg_id,)).fetchone()["created_at"],
                )

        machine_label = _machine_label(conn, machine_id)

    passages = hybrid_search(question, machine_id=machine_id, top_k=6)
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
    _seen_citation_chunks: set[int] = set()
    _deduped_citations = []
    for _c in result.citations:
        if _c.chunk_id in _seen_citation_chunks:
            continue
        _seen_citation_chunks.add(_c.chunk_id)
        _deduped_citations.append(_c)
    result.citations = _deduped_citations

    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO messages (conversation_id, role, content, is_no_answer, machine_id, "
            "safety_warnings, conflict_note, provider, answer_status) "
            "VALUES (?, 'assistant', ?, ?, ?, ?, ?, ?, ?)",
            (
                conversation_id, result.answer, int(result.is_no_answer), machine_id,
                json.dumps(result.safety_warnings) if result.safety_warnings else None,
                result.conflict_note, result.provider, answer_status,
            ),
        )
        msg_id = cur.lastrowid
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
                "combined_score, is_citation, excerpt, citation_ordinal) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    msg_id, p.chunk_id, rank, p.lexical_score, p.vector_score, p.combined_score,
                    int(is_citation), citation_excerpt_by_chunk.get(p.chunk_id),
                    citation_ordinal_by_chunk.get(p.chunk_id),
                ),
            )
        conn.execute("UPDATE conversations SET updated_at = datetime('now') WHERE id = ?", (conversation_id,))
        created_at = conn.execute("SELECT created_at FROM messages WHERE id=?", (msg_id,)).fetchone()["created_at"]

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
        created_at=created_at,
    )


class FeedbackRequest(BaseModel):
    rating: str = Field(pattern="^(helpful|incorrect|missing_info)$")
    comment: str | None = Field(default=None, max_length=1000)


@router.post("/messages/{message_id}/feedback", status_code=status.HTTP_201_CREATED)
def submit_feedback(message_id: int, payload: FeedbackRequest, user: CurrentUser = Depends(get_current_user)):
    with get_conn() as conn:
        msg = conn.execute(
            "SELECT m.id FROM messages m JOIN conversations c ON c.id = m.conversation_id "
            "WHERE m.id = ? AND c.user_id = ?",
            (message_id, user.id),
        ).fetchone()
        if not msg:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Message not found.")
        conn.execute(
            "INSERT INTO feedback (message_id, user_id, rating, comment) VALUES (?, ?, ?, ?)",
            (message_id, user.id, payload.rating, payload.comment),
        )
    return {"ok": True}


@router.post("/messages/{message_id}/save", status_code=status.HTTP_201_CREATED)
def save_answer(message_id: int, user: CurrentUser = Depends(get_current_user)):
    with get_conn() as conn:
        msg = conn.execute(
            "SELECT m.id FROM messages m JOIN conversations c ON c.id = m.conversation_id "
            "WHERE m.id = ? AND c.user_id = ?",
            (message_id, user.id),
        ).fetchone()
        if not msg:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Message not found.")
        conn.execute(
            "INSERT INTO saved_answers (user_id, message_id) VALUES (?, ?)",
            (user.id, message_id),
        )
    return {"ok": True}


@router.get("/saved-answers", response_model=list[MessageOut])
def list_saved_answers(user: CurrentUser = Depends(get_current_user)):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT m.id, m.role, m.content, m.is_clarifying_question, m.is_no_answer, "
            "m.safety_warnings, m.conflict_note, m.answer_status, m.created_at "
            "FROM saved_answers sa JOIN messages m ON m.id = sa.message_id "
            "WHERE sa.user_id = ? ORDER BY sa.saved_at DESC",
            (user.id,),
        ).fetchall()
        return [_hydrate_message(conn, r) for r in rows]

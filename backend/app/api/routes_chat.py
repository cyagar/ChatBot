from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.auth.deps import CurrentUser, get_current_user
from app.db import get_conn
from app.providers.factory import get_provider
from app.rate_limit import default_limit_string, limiter
from app.retrieval.search import hybrid_search

router = APIRouter(prefix="/api", tags=["chat"])

MAX_QUESTION_LEN = 2000


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


def _resolve_machine_mention(question: str) -> tuple[int | None, list[dict]]:
    """Used only when a conversation has no machine selected yet. Looks for an
    unambiguous model-name mention in the question; if multiple machines match,
    returns candidates so the caller can ask a clarifying question instead of
    guessing (plan requirement)."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT m.id, m.model_name, mf.name AS manufacturer FROM machines m "
            "JOIN manufacturers mf ON mf.id = m.manufacturer_id"
        ).fetchall()
    q_lower = question.lower()
    matches = [r for r in rows if r["model_name"].lower() in q_lower]
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


def _hydrate_message(conn, row) -> MessageOut:
    citations = []
    src_rows = conn.execute(
        "SELECT ms.chunk_id, c.document_id, d.original_filename, d.title, c.page_number, "
        "c.section_heading, d.revision "
        "FROM message_sources ms "
        "JOIN chunks c ON c.id = ms.chunk_id "
        "JOIN documents d ON d.id = c.document_id "
        "WHERE ms.message_id = ? ORDER BY ms.rank",
        (row["id"],),
    ).fetchall()
    for s in src_rows:
        excerpt_row = conn.execute("SELECT content FROM chunks WHERE id = ?", (s["chunk_id"],)).fetchone()
        citations.append(CitationOut(
            chunk_id=s["chunk_id"], document_id=s["document_id"], filename=s["original_filename"],
            title=s["title"], page_number=s["page_number"], section_heading=s["section_heading"],
            revision=s["revision"], excerpt=(excerpt_row["content"][:500] if excerpt_row else ""),
        ))
    return MessageOut(
        id=row["id"], role=row["role"], content=row["content"],
        is_clarifying_question=bool(row["is_clarifying_question"]),
        is_no_answer=bool(row["is_no_answer"]),
        citations=citations, created_at=row["created_at"],
    )


@router.get("/conversations/{conversation_id}/messages", response_model=list[MessageOut])
def get_messages(conversation_id: int, user: CurrentUser = Depends(get_current_user)):
    with get_conn() as conn:
        _require_own_conversation(conn, conversation_id, user.id)
        rows = conn.execute(
            "SELECT id, role, content, is_clarifying_question, is_no_answer, created_at "
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
    result = provider.generate(question, machine_label, passages)

    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO messages (conversation_id, role, content, is_no_answer) VALUES (?, 'assistant', ?, ?)",
            (conversation_id, result.answer, int(result.is_no_answer)),
        )
        msg_id = cur.lastrowid
        for rank, p in enumerate(passages):
            conn.execute(
                "INSERT INTO message_sources (message_id, chunk_id, rank, lexical_score, vector_score, combined_score) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (msg_id, p.chunk_id, rank, p.lexical_score, p.vector_score, p.combined_score),
            )
        conn.execute("UPDATE conversations SET updated_at = datetime('now') WHERE id = ?", (conversation_id,))
        created_at = conn.execute("SELECT created_at FROM messages WHERE id=?", (msg_id,)).fetchone()["created_at"]

    return MessageOut(
        id=msg_id, role="assistant", content=result.answer,
        is_clarifying_question=False, is_no_answer=result.is_no_answer,
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
            "SELECT m.id, m.role, m.content, m.is_clarifying_question, m.is_no_answer, m.created_at "
            "FROM saved_answers sa JOIN messages m ON m.id = sa.message_id "
            "WHERE sa.user_id = ? ORDER BY sa.saved_at DESC",
            (user.id,),
        ).fetchall()
        return [_hydrate_message(conn, r) for r in rows]

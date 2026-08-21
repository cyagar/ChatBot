from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr, Field

from app.auth.audit import log_audit_event
from app.auth.deps import CurrentUser, require_admin
from app.auth.security import generate_invitation_token
from app.config import get_settings
from app.db import get_conn
from app.ingestion.pipeline import _INGEST_LOCK, ingest_all
from app.retrieval.search import hybrid_search

router = APIRouter(prefix="/api/admin", tags=["admin"])


# ---------------------------------------------------------------------------
# Documents / metadata correction
# ---------------------------------------------------------------------------

class DocumentOut(BaseModel):
    id: int
    original_filename: str
    file_type: str
    status: str
    status_reason: str | None
    manufacturer: str | None
    doc_type: str | None
    title: str | None
    revision: str | None
    doc_number: str | None
    page_count: int | None
    is_current_revision: bool
    machines: list[str]
    machine_ids: list[int]
    ingested_at: str | None
    review_status: str
    reviewed_at: str | None


def _row_to_document(conn, row) -> DocumentOut:
    machines = conn.execute(
        "SELECT m.id, m.model_name, dm.confidence FROM document_machines dm "
        "JOIN machines m ON m.id = dm.machine_id WHERE dm.document_id = ? ORDER BY m.model_name",
        (row["id"],),
    ).fetchall()
    return DocumentOut(
        id=row["id"], original_filename=row["original_filename"], file_type=row["file_type"],
        status=row["status"], status_reason=row["status_reason"],
        manufacturer=row["manufacturer"], doc_type=row["doc_type"], title=row["title"],
        revision=row["revision"], doc_number=row["doc_number"], page_count=row["page_count"],
        is_current_revision=bool(row["is_current_revision"]),
        machines=[f"{m['model_name']} ({m['confidence']:.2f})" for m in machines],
        machine_ids=[m["id"] for m in machines],
        ingested_at=row["ingested_at"],
        review_status=row["review_status"],
        reviewed_at=row["reviewed_at"],
    )


# ---------------------------------------------------------------------------
# Machine catalog (for the association editor)
# ---------------------------------------------------------------------------

class MachineOut(BaseModel):
    id: int
    manufacturer: str
    model_name: str
    family: str | None


@router.get("/machines", response_model=list[MachineOut])
def list_all_machines(admin: CurrentUser = Depends(require_admin)):
    """Every machine in the catalog, unlike GET /api/machines which only
    returns machines that already have a document linked — an admin fixing a
    document's association needs to be able to pick a machine that has zero
    (or wrong) links today."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT m.id, mf.name AS manufacturer, m.model_name, m.family "
            "FROM machines m JOIN manufacturers mf ON mf.id = m.manufacturer_id "
            "ORDER BY mf.name, m.model_name"
        ).fetchall()
    return [MachineOut(id=r["id"], manufacturer=r["manufacturer"], model_name=r["model_name"], family=r["family"]) for r in rows]


@router.get("/documents", response_model=list[DocumentOut])
def list_documents(
    status_filter: str | None = None,
    q: str = "",
    include_deactivated: bool = False,
    admin: CurrentUser = Depends(require_admin),
):
    sql = (
        "SELECT d.*, mf.name AS manufacturer FROM documents d "
        "LEFT JOIN manufacturers mf ON mf.id = d.manufacturer_id WHERE 1=1"
    )
    params: list = []
    if not include_deactivated:
        sql += " AND d.deactivated_at IS NULL"
    if status_filter:
        sql += " AND d.status = ?"
        params.append(status_filter)
    if q:
        sql += " AND d.original_filename LIKE ?"
        params.append(f"%{q}%")
    sql += " ORDER BY d.created_at DESC"
    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
        return [_row_to_document(conn, r) for r in rows]


class MetadataCorrection(BaseModel):
    manufacturer_name: str | None = None
    doc_type: str | None = None
    title: str | None = None
    revision: str | None = None
    is_current_revision: bool | None = None
    machine_ids: list[int] | None = None
    reason: str = Field(min_length=1, max_length=500)


@router.patch("/documents/{document_id}", response_model=DocumentOut)
def correct_metadata(document_id: int, payload: MetadataCorrection, admin: CurrentUser = Depends(require_admin)):
    with get_conn() as conn:
        doc = conn.execute("SELECT * FROM documents WHERE id = ?", (document_id,)).fetchone()
        if not doc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Document not found.")

        def _log(field: str, previous, new_value):
            conn.execute(
                "INSERT INTO metadata_overrides (document_id, field, previous_value, corrected_value, "
                "corrected_by, reason) VALUES (?, ?, ?, ?, ?, ?)",
                (document_id, field, str(previous) if previous is not None else None, str(new_value),
                 admin.email, payload.reason),
            )

        if payload.manufacturer_name is not None:
            manu = conn.execute("SELECT id FROM manufacturers WHERE name = ?", (payload.manufacturer_name,)).fetchone()
            manu_id = manu["id"] if manu else conn.execute(
                "INSERT INTO manufacturers (name) VALUES (?)", (payload.manufacturer_name,)
            ).lastrowid
            _log("manufacturer", doc["manufacturer_id"], manu_id)
            conn.execute("UPDATE documents SET manufacturer_id = ? WHERE id = ?", (manu_id, document_id))

        for field in ("doc_type", "title", "revision"):
            new_value = getattr(payload, field)
            if new_value is not None:
                _log(field, doc[field], new_value)
                conn.execute(f"UPDATE documents SET {field} = ? WHERE id = ?", (new_value, document_id))

        if payload.is_current_revision is not None:
            _log("is_current_revision", doc["is_current_revision"], payload.is_current_revision)
            conn.execute(
                "UPDATE documents SET is_current_revision = ? WHERE id = ?",
                (int(payload.is_current_revision), document_id),
            )

        if payload.machine_ids is not None:
            _log("machine_links", None, payload.machine_ids)
            # An admin setting links here IS the human review those links get
            # (independent follow-up review P0-6) -- insert them pre-approved
            # rather than 'pending', or this endpoint would silently remove a
            # document from retrieval every time an admin corrected it.
            # Deliberately clears any prior 'rejected' rows for this document
            # too: the admin is explicitly overriding whatever review state
            # existed before, not appending to it.
            conn.execute("DELETE FROM document_machines WHERE document_id = ?", (document_id,))
            for mid in payload.machine_ids:
                conn.execute(
                    "INSERT OR IGNORE INTO document_machines "
                    "(document_id, machine_id, confidence, review_status, reviewed_by, reviewed_at) "
                    "VALUES (?, ?, 1.0, 'approved', ?, datetime('now'))",
                    (document_id, mid, admin.id),
                )

        row = conn.execute(
            "SELECT d.*, mf.name AS manufacturer FROM documents d "
            "LEFT JOIN manufacturers mf ON mf.id = d.manufacturer_id WHERE d.id = ?",
            (document_id,),
        ).fetchone()
        return _row_to_document(conn, row)


@router.post("/documents/{document_id}/deactivate")
def deactivate_document(document_id: int, reason: str = "Deactivated by administrator.",
                         admin: CurrentUser = Depends(require_admin)):
    with get_conn() as conn:
        result = conn.execute(
            "UPDATE documents SET deactivated_at = datetime('now'), "
            "status_reason = COALESCE(status_reason || ' | ', '') || ? WHERE id = ? AND deactivated_at IS NULL",
            (reason, document_id),
        )
        if result.rowcount == 0:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Document not found or already deactivated.")
    return {"ok": True}


# ---------------------------------------------------------------------------
# Document/link review queue (independent follow-up review P0-6: heuristic
# metadata proposes machine associations, but a Drive edit alone must never be
# enough to make a document retrievable -- retrieval only ever uses documents
# and document_machines links with review_status='approved', enforced in
# app/retrieval/search.py, not just displayed here.)
# ---------------------------------------------------------------------------

class PendingLinkOut(BaseModel):
    machine_id: int
    model_name: str
    manufacturer: str
    confidence: float
    review_status: str


class ReviewQueueDocumentOut(BaseModel):
    id: int
    original_filename: str
    doc_type: str | None
    manufacturer: str | None
    title: str | None
    status: str
    review_status: str
    ingested_at: str | None
    links: list[PendingLinkOut]


@router.get("/review-queue", response_model=list[ReviewQueueDocumentOut])
def review_queue(admin: CurrentUser = Depends(require_admin)):
    """Every active document that either isn't approved itself, or has at
    least one non-approved (pending/rejected) machine link -- the second half
    matters even for an already-approved document, since a re-index can
    propose a *new* link on an existing approved document at any time."""
    with get_conn() as conn:
        docs = conn.execute(
            "SELECT DISTINCT d.id, d.original_filename, d.doc_type, mf.name AS manufacturer, "
            "d.title, d.status, d.review_status, d.ingested_at "
            "FROM documents d "
            "LEFT JOIN manufacturers mf ON mf.id = d.manufacturer_id "
            "LEFT JOIN document_machines dm ON dm.document_id = d.id "
            "WHERE d.deactivated_at IS NULL "
            "AND (d.review_status != 'approved' OR dm.review_status != 'approved') "
            "ORDER BY d.ingested_at DESC"
        ).fetchall()
        out = []
        for d in docs:
            links = conn.execute(
                "SELECT m.id AS machine_id, m.model_name, mf.name AS manufacturer, "
                "dm.confidence, dm.review_status "
                "FROM document_machines dm "
                "JOIN machines m ON m.id = dm.machine_id "
                "JOIN manufacturers mf ON mf.id = m.manufacturer_id "
                "WHERE dm.document_id = ? ORDER BY dm.review_status, m.model_name",
                (d["id"],),
            ).fetchall()
            out.append(ReviewQueueDocumentOut(
                id=d["id"], original_filename=d["original_filename"], doc_type=d["doc_type"],
                manufacturer=d["manufacturer"], title=d["title"], status=d["status"],
                review_status=d["review_status"], ingested_at=d["ingested_at"],
                links=[PendingLinkOut(machine_id=l["machine_id"], model_name=l["model_name"],
                                       manufacturer=l["manufacturer"], confidence=l["confidence"],
                                       review_status=l["review_status"]) for l in links],
            ))
    return out


class DocumentReviewRequest(BaseModel):
    decision: str = Field(pattern="^(approved|rejected)$")
    note: str | None = Field(default=None, max_length=500)


@router.post("/documents/{document_id}/review")
def review_document(document_id: int, payload: DocumentReviewRequest, admin: CurrentUser = Depends(require_admin)):
    with get_conn() as conn:
        doc = conn.execute("SELECT id FROM documents WHERE id = ? AND deactivated_at IS NULL", (document_id,)).fetchone()
        if not doc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Document not found or deactivated.")
        conn.execute(
            "UPDATE documents SET review_status = ?, reviewed_by = ?, reviewed_at = datetime('now'), "
            "review_note = ? WHERE id = ?",
            (payload.decision, admin.id, payload.note, document_id),
        )
        log_audit_event(conn, "document_reviewed", actor_user_id=admin.id, target_type="document",
                         target_id=document_id, detail=f"{payload.decision}" + (f": {payload.note}" if payload.note else ""))
    return {"ok": True}


class LinkReviewRequest(BaseModel):
    decision: str = Field(pattern="^(approved|rejected)$")


@router.post("/documents/{document_id}/machines/{machine_id}/review")
def review_document_machine_link(document_id: int, machine_id: int, payload: LinkReviewRequest,
                                  admin: CurrentUser = Depends(require_admin)):
    with get_conn() as conn:
        result = conn.execute(
            "UPDATE document_machines SET review_status = ?, reviewed_by = ?, reviewed_at = datetime('now') "
            "WHERE document_id = ? AND machine_id = ?",
            (payload.decision, admin.id, document_id, machine_id),
        )
        if result.rowcount == 0:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Document-machine link not found.")
        log_audit_event(conn, "document_machine_reviewed", actor_user_id=admin.id, target_type="document_machine",
                         target_id=document_id, detail=f"machine_id={machine_id}: {payload.decision}")
    return {"ok": True}


# ---------------------------------------------------------------------------
# Invitations + account management (independent follow-up review P0-5: public
# self-registration is closed -- an account can only be created by consuming
# an admin-issued invitation. See app/auth/routes.py:register and
# scripts/bootstrap_admin.py for the very first administrator.)
# ---------------------------------------------------------------------------

DEFAULT_INVITE_TTL_HOURS = 72


class InvitationCreate(BaseModel):
    email: EmailStr
    role: str = Field(default="technician", pattern="^(technician|administrator)$")
    expires_in_hours: int = Field(default=DEFAULT_INVITE_TTL_HOURS, ge=1, le=24 * 30)


class InvitationOut(BaseModel):
    id: int
    email: str
    role: str
    created_at: str
    expires_at: str
    used_at: str | None
    revoked_at: str | None
    token: str | None = None  # only populated once, in the create response


def _check_invite_domain_allowed(email: str) -> None:
    allowed = [d.strip().lower() for d in get_settings().allowed_registration_domains.split(",") if d.strip()]
    if not allowed:
        return
    domain = email.rsplit("@", 1)[-1].lower()
    if domain not in allowed:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=f"{domain!r} is not in ALLOWED_REGISTRATION_DOMAINS.",
        )


@router.post("/invitations", response_model=InvitationOut, status_code=status.HTTP_201_CREATED)
def create_invitation(payload: InvitationCreate, admin: CurrentUser = Depends(require_admin)):
    _check_invite_domain_allowed(payload.email)
    raw_token, token_hash = generate_invitation_token()
    expires_at = (datetime.now(timezone.utc) + timedelta(hours=payload.expires_in_hours)).isoformat()

    with get_conn() as conn:
        existing_user = conn.execute("SELECT id FROM users WHERE email = ?", (payload.email,)).fetchone()
        if existing_user:
            raise HTTPException(status.HTTP_409_CONFLICT, detail="An account with this email already exists.")
        cur = conn.execute(
            "INSERT INTO invitations (token_hash, email, role, created_by, expires_at) VALUES (?, ?, ?, ?, ?)",
            (token_hash, payload.email, payload.role, admin.id, expires_at),
        )
        invite_id = cur.lastrowid
        log_audit_event(conn, "invite_created", actor_user_id=admin.id, target_type="invitation",
                         target_id=invite_id, detail=f"role={payload.role} email={payload.email}")
        row = conn.execute("SELECT * FROM invitations WHERE id = ?", (invite_id,)).fetchone()

    return InvitationOut(
        id=row["id"], email=row["email"], role=row["role"], created_at=row["created_at"],
        expires_at=row["expires_at"], used_at=row["used_at"], revoked_at=row["revoked_at"],
        token=raw_token,
    )


@router.get("/invitations", response_model=list[InvitationOut])
def list_invitations(admin: CurrentUser = Depends(require_admin), limit: int = 50):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM invitations ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [
        InvitationOut(id=r["id"], email=r["email"], role=r["role"], created_at=r["created_at"],
                      expires_at=r["expires_at"], used_at=r["used_at"], revoked_at=r["revoked_at"])
        for r in rows
    ]


@router.post("/invitations/{invitation_id}/revoke")
def revoke_invitation(invitation_id: int, admin: CurrentUser = Depends(require_admin)):
    with get_conn() as conn:
        result = conn.execute(
            "UPDATE invitations SET revoked_at = datetime('now') "
            "WHERE id = ? AND used_at IS NULL AND revoked_at IS NULL",
            (invitation_id,),
        )
        if result.rowcount == 0:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Invitation not found, already used, or already revoked.")
    return {"ok": True}


class UserOut(BaseModel):
    id: int
    email: str
    role: str
    display_name: str | None
    is_disabled: bool
    created_at: str
    last_login_at: str | None


@router.get("/users", response_model=list[UserOut])
def list_users(admin: CurrentUser = Depends(require_admin)):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, email, role, display_name, is_disabled, created_at, last_login_at "
            "FROM users ORDER BY created_at"
        ).fetchall()
    return [UserOut(id=r["id"], email=r["email"], role=r["role"], display_name=r["display_name"],
                     is_disabled=bool(r["is_disabled"]), created_at=r["created_at"],
                     last_login_at=r["last_login_at"]) for r in rows]


@router.post("/users/{user_id}/disable")
def disable_user(user_id: int, admin: CurrentUser = Depends(require_admin)):
    if user_id == admin.id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="You cannot disable your own account.")
    with get_conn() as conn:
        # Bumping token_version invalidates every session token already issued
        # to this user, even ones that haven't expired yet (P0-5's "session
        # revocation" requirement) -- see app/auth/deps.py's tv check.
        result = conn.execute(
            "UPDATE users SET is_disabled = 1, disabled_at = datetime('now'), "
            "token_version = token_version + 1 WHERE id = ? AND is_disabled = 0",
            (user_id,),
        )
        if result.rowcount == 0:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="User not found or already disabled.")
        log_audit_event(conn, "user_disabled", actor_user_id=admin.id, target_type="user", target_id=user_id)
    return {"ok": True}


@router.post("/users/{user_id}/enable")
def enable_user(user_id: int, admin: CurrentUser = Depends(require_admin)):
    with get_conn() as conn:
        result = conn.execute(
            "UPDATE users SET is_disabled = 0, disabled_at = NULL WHERE id = ? AND is_disabled = 1",
            (user_id,),
        )
        if result.rowcount == 0:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="User not found or not disabled.")
        log_audit_event(conn, "user_enabled", actor_user_id=admin.id, target_type="user", target_id=user_id)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Re-index
# ---------------------------------------------------------------------------
# Direct file upload was removed: ingestion is Drive-only now (add a manual to
# the shared Drive folder, then trigger a re-index below) so there is exactly
# one place manuals live, not a local upload folder that could drift out of
# sync with Drive.

@router.post("/ingestion/reindex", status_code=status.HTTP_202_ACCEPTED)
def trigger_reindex(background_tasks: BackgroundTasks, admin: CurrentUser = Depends(require_admin)):
    if _INGEST_LOCK.locked():
        raise HTTPException(status.HTTP_409_CONFLICT, detail="An ingestion run is already in progress.")
    background_tasks.add_task(ingest_all)
    return {"ok": True, "detail": "Re-index started in the background."}


@router.get("/ingestion/runs")
def list_ingestion_runs(admin: CurrentUser = Depends(require_admin), limit: int = 10):
    with get_conn() as conn:
        runs = conn.execute(
            "SELECT id, started_at, finished_at, status FROM ingestion_runs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        out = []
        for r in runs:
            counts = conn.execute(
                "SELECT event, COUNT(*) c FROM ingestion_events WHERE run_id = ? GROUP BY event", (r["id"],)
            ).fetchall()
            out.append({
                "id": r["id"], "started_at": r["started_at"], "finished_at": r["finished_at"],
                "status": r["status"], "counts": {c["event"]: c["c"] for c in counts},
            })
    return out


@router.get("/ingestion/runs/{run_id}/report")
def get_ingestion_report(run_id: int, admin: CurrentUser = Depends(require_admin)):
    """The plan's required ingestion report: every source file as indexed,
    duplicate, partially processed, failed, or unsupported, with a reason."""
    with get_conn() as conn:
        run = conn.execute("SELECT * FROM ingestion_runs WHERE id = ?", (run_id,)).fetchone()
        if not run:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Run not found.")
        events = conn.execute(
            "SELECT original_filename, event, detail, document_id FROM ingestion_events "
            "WHERE run_id = ? ORDER BY id",
            (run_id,),
        ).fetchall()
    return {
        "run_id": run_id,
        "started_at": run["started_at"],
        "finished_at": run["finished_at"],
        "status": run["status"],
        "files": [dict(e) for e in events],
    }


# ---------------------------------------------------------------------------
# Duplicates
# ---------------------------------------------------------------------------

@router.get("/duplicates")
def list_duplicates(admin: CurrentUser = Depends(require_admin)):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT dm.id, dm.match_type, dm.similarity, dm.detected_at, "
            "k.id AS kept_id, k.original_filename AS kept_name, "
            "d.id AS dup_id, d.original_filename AS dup_name "
            "FROM duplicate_matches dm "
            "JOIN documents k ON k.id = dm.kept_document_id "
            "JOIN documents d ON d.id = dm.duplicate_document_id "
            "ORDER BY dm.detected_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Query tester — inspect exact retrieved passages before answer generation
# ---------------------------------------------------------------------------

class QueryTestRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    machine_id: int | None = None
    top_k: int = Field(default=6, ge=1, le=20)
    # Superseded revisions are excluded from technician retrieval entirely
    # (P1-11). This is the "explicit admin/audit flow" that can still see
    # them -- off by default so the tester shows what a technician would
    # actually get unless an admin deliberately asks to look wider.
    include_superseded: bool = False


@router.post("/query-test")
def query_test(payload: QueryTestRequest, admin: CurrentUser = Depends(require_admin)):
    passages = hybrid_search(payload.question, machine_id=payload.machine_id, top_k=payload.top_k,
                              include_superseded=payload.include_superseded)
    return {
        "passages": [
            {
                "chunk_id": p.chunk_id, "document_id": p.document_id, "filename": p.original_filename,
                "page_number": p.page_number, "section_heading": p.section_heading,
                "chunk_type": p.chunk_type, "content": p.content,
                "lexical_score": p.lexical_score, "vector_score": p.vector_score,
                "combined_score": p.combined_score, "is_current_revision": p.is_current_revision,
            }
            for p in passages
        ]
    }


# ---------------------------------------------------------------------------
# Feedback + frequently unanswered questions
# ---------------------------------------------------------------------------

@router.get("/feedback")
def list_feedback(admin: CurrentUser = Depends(require_admin), limit: int = 100):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT f.id, f.rating, f.comment, f.created_at, u.email AS user_email, "
            "m.content AS question_or_answer, m.conversation_id "
            "FROM feedback f JOIN users u ON u.id = f.user_id JOIN messages m ON m.id = f.message_id "
            "ORDER BY f.created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


@router.get("/unanswered")
def frequently_unanswered(admin: CurrentUser = Depends(require_admin), limit: int = 50):
    """Questions the system explicitly could not answer (is_no_answer=1 on the
    assistant's reply), most recent first — surfaces gaps in manual coverage."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT prev.content AS question, m.created_at, m.conversation_id "
            "FROM messages m "
            "JOIN messages prev ON prev.conversation_id = m.conversation_id AND prev.id = ("
            "  SELECT MAX(id) FROM messages WHERE conversation_id = m.conversation_id AND id < m.id AND role='user'"
            ") "
            "WHERE m.role = 'assistant' AND m.is_no_answer = 1 "
            "ORDER BY m.created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]

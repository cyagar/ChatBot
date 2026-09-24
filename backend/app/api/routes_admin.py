from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from pydantic import BaseModel, EmailStr, Field, field_serializer

from app.api.common import iso_utc
from app.auth.audit import log_audit_event
from app.auth.deps import CurrentUser, require_admin
from app.auth.security import generate_invitation_token, normalize_email
from app.config import get_settings
from app.db import get_conn
from app.ingestion.chunking import CURRENT_CHUNKING_VERSION
from app.ingestion.extractors import CURRENT_EXTRACTION_VERSION
from app.ingestion.pipeline import _INGEST_LOCK, ingest_all
from app.ingestion.scheduler import is_enabled as scheduler_is_enabled
from app.retrieval.embeddings import embedding_fingerprint
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
    # Distinguishes each existing machine link's review_status
    # (approved/pending/rejected), so the admin editor's machine picker can
    # show that state rather than pre-checking every link identically -- an
    # admin needs to be able to tell a rejected link apart from an approved
    # one before deciding whether to touch it. Keyed by machine_id (Pydantic
    # serializes int dict keys as JSON strings automatically) so an admin
    # re-affirming a rejected link is an informed choice, not an accident.
    machine_link_review_status: dict[int, str] = {}
    # Postgres TIMESTAMPTZ columns come back from psycopg as real datetimes,
    # not strings -- iso_utc() (app/api/common.py) accepts either.
    ingested_at: datetime | None
    review_status: str
    reviewed_at: datetime | None
    needs_reprocessing: bool
    deactivated_at: datetime | None

    @field_serializer("ingested_at", "reviewed_at", "deactivated_at")
    def _ser_ts(self, v: datetime | None) -> str | None:
        return iso_utc(v)


def _row_to_document(conn, row) -> DocumentOut:
    machines = conn.execute(
        "SELECT m.id, m.model_name, dm.confidence, dm.review_status FROM document_machines dm "
        "JOIN machines m ON m.id = dm.machine_id WHERE dm.document_id = %s ORDER BY m.model_name",
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
        machine_link_review_status={m["id"]: m["review_status"] for m in machines},
        ingested_at=row["ingested_at"],
        review_status=row["review_status"],
        reviewed_at=row["reviewed_at"],
        # Surfaces documents whose content is unchanged but were
        # extracted/chunked at an older pipeline version -- see the
        # needs_reprocessing outcome in pipeline.py._ingest_one for how this
        # is detected at ingest time.
        needs_reprocessing=(
            row["status"] in ("indexed", "partial")
            and (row["extraction_version"] != CURRENT_EXTRACTION_VERSION
                 or row["chunking_version"] != CURRENT_CHUNKING_VERSION)
        ),
        deactivated_at=row["deactivated_at"],
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
        sql += " AND d.status = %s"
        params.append(status_filter)
    if q:
        # ILIKE, not LIKE -- see routes_machines.py's search_machines() for
        # why (SQLite's LIKE is case-insensitive by default, Postgres's isn't).
        sql += " AND d.original_filename ILIKE %s"
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
        doc = conn.execute("SELECT * FROM documents WHERE id = %s", (document_id,)).fetchone()
        if not doc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Document not found.")

        def _log(field: str, previous, new_value):
            conn.execute(
                "INSERT INTO metadata_overrides (document_id, field, previous_value, corrected_value, "
                "corrected_by, reason) VALUES (%s, %s, %s, %s, %s, %s)",
                (document_id, field, str(previous) if previous is not None else None, str(new_value),
                 admin.email, payload.reason),
            )

        if payload.manufacturer_name is not None:
            manu = conn.execute("SELECT id FROM manufacturers WHERE name = %s", (payload.manufacturer_name,)).fetchone()
            if manu:
                manu_id = manu["id"]
            else:
                manu_id = conn.execute(
                    "INSERT INTO manufacturers (name) VALUES (%s) RETURNING id", (payload.manufacturer_name,)
                ).fetchone()["id"]
            _log("manufacturer", doc["manufacturer_id"], manu_id)
            conn.execute("UPDATE documents SET manufacturer_id = %s WHERE id = %s", (manu_id, document_id))

        for field in ("doc_type", "title", "revision"):
            new_value = getattr(payload, field)
            if new_value is not None:
                _log(field, doc[field], new_value)
                conn.execute(f"UPDATE documents SET {field} = %s WHERE id = %s", (new_value, document_id))

        if payload.is_current_revision is not None:
            _log("is_current_revision", doc["is_current_revision"], payload.is_current_revision)
            conn.execute(
                "UPDATE documents SET is_current_revision = %s WHERE id = %s",
                (payload.is_current_revision, document_id),
            )

        if payload.machine_ids is not None:
            # machine_ids must be validated before document_machines' INSERT
            # -- an admin typo/stale ID would otherwise hit the foreign key
            # and surface as an unhandled 500 instead of a clean 4xx naming
            # the bad id.
            if payload.machine_ids:
                found = {
                    r["id"] for r in conn.execute(
                        "SELECT id FROM machines WHERE id = ANY(%s)", (payload.machine_ids,)
                    ).fetchall()
                }
                missing = sorted(set(payload.machine_ids) - found)
                if missing:
                    raise HTTPException(
                        status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail=f"Unknown machine_ids: {missing}",
                    )
            _log("machine_links", None, payload.machine_ids)
            # An admin setting links here IS the human review those links get
            # -- insert them pre-approved rather than 'pending', or this
            # endpoint would silently remove a document from retrieval every
            # time an admin corrected it.
            # Deliberately clears any prior 'rejected' rows for this document
            # too: the admin is explicitly overriding whatever review state
            # existed before, not appending to it.
            conn.execute("DELETE FROM document_machines WHERE document_id = %s", (document_id,))
            for mid in payload.machine_ids:
                conn.execute(
                    "INSERT INTO document_machines "
                    "(document_id, machine_id, confidence, review_status, reviewed_by, reviewed_at) "
                    "VALUES (%s, %s, 1.0, 'approved', %s, now()) "
                    "ON CONFLICT (document_id, machine_id) DO NOTHING",
                    (document_id, mid, admin.id),
                )

        row = conn.execute(
            "SELECT d.*, mf.name AS manufacturer FROM documents d "
            "LEFT JOIN manufacturers mf ON mf.id = d.manufacturer_id WHERE d.id = %s",
            (document_id,),
        ).fetchone()
        return _row_to_document(conn, row)


@router.post("/documents/{document_id}/deactivate")
def deactivate_document(document_id: int, reason: str = "Deactivated by administrator.",
                         admin: CurrentUser = Depends(require_admin)):
    with get_conn() as conn:
        result = conn.execute(
            "UPDATE documents SET deactivated_at = now(), "
            "status_reason = COALESCE(status_reason || ' | ', '') || %s WHERE id = %s AND deactivated_at IS NULL",
            (reason, document_id),
        )
        if result.rowcount == 0:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Document not found or already deactivated.")
        log_audit_event(conn, "document_deactivated", actor_user_id=admin.id,
                         target_type="document", target_id=document_id, detail=reason)
    return {"ok": True}


@router.post("/documents/{document_id}/reactivate")
def reactivate_document(document_id: int, reason: str = "Reactivated by administrator.",
                         admin: CurrentUser = Depends(require_admin)):
    """Undoes deactivate_document. Only clears deactivated_at -- does not touch
    review_status or is_current_revision, since a document deactivated for a
    reason other than "it was superseded" (e.g. deactivated by mistake, or a
    withdrawn manual reinstated after correction) may need either left exactly
    as they were. If this document was superseded by another that is now the
    active one for the same source_ref, an administrator must resolve that
    overlap explicitly afterward (PATCH .../documents/{id} to correct
    is_current_revision, or deactivate the other one) -- reactivation alone
    does not infer which of two active documents should win."""
    with get_conn() as conn:
        result = conn.execute(
            "UPDATE documents SET deactivated_at = NULL WHERE id = %s AND deactivated_at IS NOT NULL",
            (document_id,),
        )
        if result.rowcount == 0:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Document not found or not deactivated.")
        log_audit_event(conn, "document_reactivated", actor_user_id=admin.id,
                         target_type="document", target_id=document_id, detail=reason)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Document/link review queue: heuristic metadata proposes machine
# associations, but a Drive edit alone must never be enough to make a
# document retrievable -- retrieval only ever uses documents and
# document_machines links with review_status='approved', enforced in
# app/retrieval/search.py, not just displayed here.
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
    ingested_at: datetime | None
    links: list[PendingLinkOut]

    @field_serializer("ingested_at")
    def _ser_ts(self, v: datetime | None) -> str | None:
        return iso_utc(v)


@router.get("/review-queue", response_model=list[ReviewQueueDocumentOut])
def review_queue(admin: CurrentUser = Depends(require_admin)):
    """Every active document that either isn't approved itself, or has at
    least one non-approved (pending/rejected) machine link -- the second half
    matters even for an already-approved document, since a re-index can
    propose a *new* link on an existing approved document at any time.

    An approved document with ZERO machine links is unretrievable (retrieval
    requires an approved link too) but must still surface in this queue. The
    LEFT JOIN leaves dm.review_status NULL for such a document, and plain
    `dm.review_status != 'approved'` evaluates to NULL (not TRUE) against a
    NULL, so a bare `d.review_status != 'approved' OR dm.review_status !=
    'approved'` would evaluate to FALSE OR NULL = NULL, which WHERE treats as
    excluded. IS DISTINCT FROM is NULL-safe: NULL IS DISTINCT FROM 'approved'
    is TRUE, so a zero-link approved document is correctly included."""
    with get_conn() as conn:
        docs = conn.execute(
            "SELECT DISTINCT d.id, d.original_filename, d.doc_type, mf.name AS manufacturer, "
            "d.title, d.status, d.review_status, d.ingested_at "
            "FROM documents d "
            "LEFT JOIN manufacturers mf ON mf.id = d.manufacturer_id "
            "LEFT JOIN document_machines dm ON dm.document_id = d.id "
            "WHERE d.deactivated_at IS NULL "
            "AND (d.review_status != 'approved' OR dm.review_status IS DISTINCT FROM 'approved') "
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
                "WHERE dm.document_id = %s ORDER BY dm.review_status, m.model_name",
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
    """Approving a replacement is the cutover point, not ingestion.
    `_ingest_one` deliberately leaves the document a replacement is
    superseding active (deactivated_at IS NULL) so a pending or rejected
    replacement never takes a manual away from technicians. Approving the
    replacement here is what atomically retires whatever else is still
    active at the same source_ref -- one transaction, so there's never a
    moment with either zero or two approved documents live at that
    source_ref.

    The review_status UPDATE below carries a WHERE-clause guard against a
    concurrent supersession -- it re-checks `deactivated_at IS NULL` as part
    of the same atomic write, rather than relying only on the SELECT above (a
    separate, unguarded read) to have already confirmed it. Without that
    guard, two admins approving two DIFFERENT pending replacement candidates
    at the SAME source_ref concurrently could both pass that initial SELECT
    before either committed, and then both writes would proceed: the second
    admin's UPDATE would set review_status='approved' on a document the FIRST
    admin's supersede step had just deactivated, producing an "approved but
    deactivated" row, and that second admin's own supersede step would then
    deactivate the FIRST admin's candidate too -- leaving ZERO active
    approved documents at that source_ref. The same claim-UPDATE pattern used
    everywhere else in this codebase closes that gap: the losing concurrent
    approval sees rowcount 0 and gets a clean 404, exactly as if it had raced
    the SELECT and lost there."""
    with get_conn() as conn:
        doc = conn.execute(
            "SELECT id, source_ref, status FROM documents WHERE id = %s AND deactivated_at IS NULL", (document_id,)
        ).fetchone()
        if not doc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Document not found or deactivated.")

        if payload.decision == "approved":
            # Lock every active document at this source_ref up front, in a
            # single fixed global order (ascending id). The readiness checks
            # just below delay how soon this transaction reaches the claim
            # UPDATE relative to a concurrent approval of a DIFFERENT
            # candidate at the same source_ref -- without this upfront lock,
            # two concurrent approvals can each hold their own row (from the
            # claim UPDATE further down) while waiting on a row the other
            # holds: a real lock-ordering cycle (Postgres reports it as
            # DeadlockDetected), not just one request blocking behind the
            # other. Acquiring every row's lock here, in the same order every
            # transaction uses, makes that cycle impossible: whichever
            # transaction gets here first locks the lowest id first and the
            # other simply queues behind it.
            conn.execute(
                "SELECT id FROM documents WHERE source_ref = %s AND deactivated_at IS NULL ORDER BY id FOR UPDATE",
                (doc["source_ref"],),
            )

            # "Approve document" and "Approve link" are two independent
            # buttons on the same review-queue card (admin.js
            # renderReviewQueue) -- nothing stops an admin clicking the
            # former first. Approving the document alone must not be enough
            # to retire the prior working revision below when this document
            # failed ingestion, extracted zero chunks, or has no approved
            # machine link yet -- that would leave technicians with nothing
            # retrievable at this source_ref until the admin came back and
            # separately approved a link. Promotion must be a single atomic
            # transition: verify the replacement is actually ready before
            # retiring the revision that still works.
            chunk_count = conn.execute(
                "SELECT COUNT(*) AS n FROM chunks WHERE document_id = %s", (document_id,)
            ).fetchone()["n"]
            has_approved_link = conn.execute(
                "SELECT 1 FROM document_machines WHERE document_id = %s AND review_status = 'approved' LIMIT 1",
                (document_id,),
            ).fetchone()
            problems = []
            if doc["status"] not in ("indexed", "partial"):
                problems.append(f"ingestion status is {doc['status']!r}, not indexed/partial")
            if chunk_count == 0:
                problems.append("it has no extracted content (0 chunks)")
            if not has_approved_link:
                problems.append("it has no approved machine link yet")
            if problems:
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    detail="Cannot approve and promote this document yet: " + "; ".join(problems) +
                           ". Approve at least one machine link first -- the previously active "
                           "revision at this source_ref keeps serving until this document is ready.",
                )

        claim = conn.execute(
            "UPDATE documents SET review_status = %s, reviewed_by = %s, reviewed_at = now(), "
            "review_note = %s WHERE id = %s AND deactivated_at IS NULL",
            (payload.decision, admin.id, payload.note, document_id),
        )
        if claim.rowcount == 0:
            # Deactivated by a concurrent approval of a competing replacement
            # at the same source_ref between the SELECT above and this UPDATE.
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Document not found or deactivated.")
        log_audit_event(conn, "document_reviewed", actor_user_id=admin.id, target_type="document",
                         target_id=document_id, detail=f"{payload.decision}" + (f": {payload.note}" if payload.note else ""))

        if payload.decision == "approved":
            superseded = conn.execute(
                "UPDATE documents SET deactivated_at = now(), "
                "status_reason = COALESCE(status_reason || ' | ', '') "
                "|| 'Superseded: document ' || %s::text || ' was approved at this source path.' "
                "WHERE source_ref = %s AND id != %s AND deactivated_at IS NULL",
                (document_id, doc["source_ref"], document_id),
            )
            if superseded.rowcount:
                log_audit_event(conn, "document_superseded", actor_user_id=admin.id, target_type="document",
                                 target_id=document_id,
                                 detail=f"Retired {superseded.rowcount} prior active document(s) at "
                                        f"source_ref={doc['source_ref']!r} on approval.")
    return {"ok": True}


class LinkReviewRequest(BaseModel):
    decision: str = Field(pattern="^(approved|rejected)$")


@router.post("/documents/{document_id}/machines/{machine_id}/review")
def review_document_machine_link(document_id: int, machine_id: int, payload: LinkReviewRequest,
                                  admin: CurrentUser = Depends(require_admin)):
    with get_conn() as conn:
        result = conn.execute(
            "UPDATE document_machines SET review_status = %s, reviewed_by = %s, reviewed_at = now() "
            "WHERE document_id = %s AND machine_id = %s",
            (payload.decision, admin.id, document_id, machine_id),
        )
        if result.rowcount == 0:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Document-machine link not found.")
        log_audit_event(conn, "document_machine_reviewed", actor_user_id=admin.id, target_type="document_machine",
                         target_id=document_id, detail=f"machine_id={machine_id}: {payload.decision}")
    return {"ok": True}


# ---------------------------------------------------------------------------
# Invitations + account management: public self-registration is closed -- an
# account can only be created by consuming an admin-issued invitation. See
# app/auth/routes.py:register and scripts/bootstrap_admin.py for the very
# first administrator.
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
    # Postgres TIMESTAMPTZ columns come back from psycopg as real datetimes,
    # not strings -- iso_utc() (app/api/common.py) accepts either and always
    # renders a string, which is what actually goes over the wire.
    created_at: datetime
    expires_at: datetime
    used_at: datetime | None
    revoked_at: datetime | None
    token: str | None = None  # only populated once, in the create response

    @field_serializer("created_at", "expires_at", "used_at", "revoked_at")
    def _ser_ts(self, v: datetime | None) -> str | None:
        return iso_utc(v)


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
    # Must be normalized before storing/comparing -- see normalize_email's
    # docstring. Two invitations differing only in case could otherwise each
    # pass the "no existing account" check below and later mint two separate
    # user rows for what a human would consider the same address.
    email = normalize_email(payload.email)
    raw_token, token_hash = generate_invitation_token()
    # A real datetime, not .isoformat() -- psycopg adapts TIMESTAMPTZ params
    # natively, and iso_utc() (app/api/common.py) now accepts either.
    expires_at = datetime.now(timezone.utc) + timedelta(hours=payload.expires_in_hours)

    with get_conn() as conn:
        existing_user = conn.execute("SELECT id FROM users WHERE email = %s", (email,)).fetchone()
        if existing_user:
            raise HTTPException(status.HTTP_409_CONFLICT, detail="An account with this email already exists.")
        cur = conn.execute(
            "INSERT INTO invitations (token_hash, email, role, created_by, expires_at) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (token_hash, email, payload.role, admin.id, expires_at),
        )
        invite_id = cur.fetchone()["id"]
        log_audit_event(conn, "invite_created", actor_user_id=admin.id, target_type="invitation",
                         target_id=invite_id, detail=f"role={payload.role} email={email}")
        row = conn.execute("SELECT * FROM invitations WHERE id = %s", (invite_id,)).fetchone()

    return InvitationOut(
        id=row["id"], email=row["email"], role=row["role"], created_at=row["created_at"],
        expires_at=row["expires_at"], used_at=row["used_at"], revoked_at=row["revoked_at"],
        token=raw_token,
    )


@router.get("/invitations", response_model=list[InvitationOut])
def list_invitations(admin: CurrentUser = Depends(require_admin), limit: int = Query(default=50, ge=1, le=200)):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM invitations ORDER BY id DESC LIMIT %s", (limit,)
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
            "UPDATE invitations SET revoked_at = now() "
            "WHERE id = %s AND used_at IS NULL AND revoked_at IS NULL",
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
    created_at: datetime
    last_login_at: datetime | None

    @field_serializer("created_at", "last_login_at")
    def _ser_ts(self, v: datetime | None) -> str | None:
        return iso_utc(v)


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
        # to this user, even ones that haven't expired yet -- see
        # app/auth/deps.py's tv check.
        result = conn.execute(
            "UPDATE users SET is_disabled = true, disabled_at = now(), "
            "token_version = token_version + 1 WHERE id = %s AND is_disabled = false",
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
            "UPDATE users SET is_disabled = false, disabled_at = NULL WHERE id = %s AND is_disabled = true",
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
    """The ingestion_runs row must be created here, synchronously, before the
    202 goes out -- ingest_all() itself only executes once the BackgroundTask
    actually runs, after this response is already sent. Creating the row
    inside ingest_all() instead would mean that if the process restarted in
    that gap, an admin who was told a run started would see no evidence one
    ever was."""
    if _INGEST_LOCK.locked():
        raise HTTPException(status.HTTP_409_CONFLICT, detail="An ingestion run is already in progress.")
    with get_conn() as conn:
        cur = conn.execute("INSERT INTO ingestion_runs (status, trigger) VALUES ('running', 'manual') RETURNING id")
        run_id = cur.fetchone()["id"]
    background_tasks.add_task(ingest_all, run_id=run_id)
    return {"ok": True, "detail": "Re-index started in the background.", "run_id": run_id}


@router.get("/ingestion/runs")
def list_ingestion_runs(admin: CurrentUser = Depends(require_admin), limit: int = Query(default=10, ge=1, le=200)):
    with get_conn() as conn:
        runs = conn.execute(
            "SELECT id, started_at, finished_at, status, trigger FROM ingestion_runs ORDER BY id DESC LIMIT %s",
            (limit,),
        ).fetchall()
        out = []
        for r in runs:
            counts = conn.execute(
                "SELECT event, COUNT(*) c FROM ingestion_events WHERE run_id = %s GROUP BY event", (r["id"],)
            ).fetchall()
            out.append({
                "id": r["id"], "started_at": iso_utc(r["started_at"]), "finished_at": iso_utc(r["finished_at"]),
                "status": r["status"], "trigger": r["trigger"], "counts": {c["event"]: c["c"] for c in counts},
            })
    return out


@router.get("/ingestion/status")
def get_ingestion_status(admin: CurrentUser = Depends(require_admin)):
    """A visible last-success timestamp/source snapshot and a stale-corpus
    alert against the configured operational SLA, so freshness doesn't
    depend on an admin remembering to check the run list and do the
    staleness math themselves."""
    settings = get_settings()
    with get_conn() as conn:
        # completed_with_errors still means Drive was successfully listed and
        # reconciled -- individual file failures don't mean the sync itself
        # failed. Only a run that never finished (status='failed', e.g. an
        # auth/quota error before any file was even seen) is not a success.
        last_success = conn.execute(
            "SELECT id, finished_at, trigger, status FROM ingestion_runs "
            "WHERE status IN ('completed', 'completed_with_errors') "
            "ORDER BY finished_at DESC LIMIT 1"
        ).fetchone()
        last_attempt = conn.execute(
            "SELECT id, started_at, finished_at, status, trigger FROM ingestion_runs "
            "ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        active_document_count = conn.execute(
            "SELECT COUNT(*) c FROM documents WHERE deactivated_at IS NULL"
        ).fetchone()["c"]
        # A model/revision change makes every existing embedding stale (see
        # embedding_fingerprint's docstring) with no error anywhere -- search
        # just quietly falls back to lexical-only for the affected chunks.
        # Surfaced here so an admin who bumps EMBEDDING_MODEL_REVISION has a
        # way to notice a re-index is needed, instead of only ever finding
        # out by search quality silently degrading.
        chunks_needing_reembedding = conn.execute(
            "SELECT COUNT(*) c FROM chunks c "
            "LEFT JOIN embeddings e ON e.chunk_id = c.id AND e.model_name = %s "
            "WHERE e.chunk_id IS NULL",
            (embedding_fingerprint(),),
        ).fetchone()["c"]

    hours_since_last_success = None
    is_stale = True
    if last_success is not None:
        # finished_at is already a tz-aware datetime -- Postgres TIMESTAMPTZ,
        # not SQLite's naive TEXT timestamp that needed fromisoformat() plus
        # an explicit UTC tzinfo attached by hand.
        finished = last_success["finished_at"]
        hours_since_last_success = (datetime.now(timezone.utc) - finished).total_seconds() / 3600
        is_stale = hours_since_last_success > settings.ingestion_staleness_threshold_hours

    return {
        "last_success_run_id": last_success["id"] if last_success else None,
        "last_success_at": iso_utc(last_success["finished_at"]) if last_success else None,
        "last_success_trigger": last_success["trigger"] if last_success else None,
        # completed_with_errors counts as a success for staleness purposes
        # (see test_completed_with_errors_still_counts_as_a_successful_sync),
        # but this response must still give a way to tell a clean success
        # from one where individual files failed, rather than requiring a
        # second call to /ingestion/runs.
        "last_success_status": last_success["status"] if last_success else None,
        "hours_since_last_success": hours_since_last_success,
        "last_attempt_run_id": last_attempt["id"] if last_attempt else None,
        "last_attempt_started_at": iso_utc(last_attempt["started_at"]) if last_attempt else None,
        "last_attempt_status": last_attempt["status"] if last_attempt else None,
        "active_document_count": active_document_count,
        "sync_interval_minutes": settings.ingestion_sync_interval_minutes,
        "scheduler_enabled": scheduler_is_enabled(settings),
        "staleness_threshold_hours": settings.ingestion_staleness_threshold_hours,
        "is_stale": is_stale,
        "chunks_needing_reembedding": chunks_needing_reembedding,
    }


@router.get("/ingestion/runs/{run_id}/report")
def get_ingestion_report(run_id: int, admin: CurrentUser = Depends(require_admin)):
    """The ingestion report: every source file as indexed, duplicate,
    partially processed, failed, or unsupported, with a reason."""
    with get_conn() as conn:
        run = conn.execute("SELECT * FROM ingestion_runs WHERE id = %s", (run_id,)).fetchone()
        if not run:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Run not found.")
        events = conn.execute(
            "SELECT original_filename, event, detail, document_id FROM ingestion_events "
            "WHERE run_id = %s ORDER BY id",
            (run_id,),
        ).fetchall()
    return {
        "run_id": run_id,
        "started_at": iso_utc(run["started_at"]),
        "finished_at": iso_utc(run["finished_at"]),
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
    return [{**dict(r), "detected_at": iso_utc(r["detected_at"])} for r in rows]


# ---------------------------------------------------------------------------
# Query tester — inspect exact retrieved passages before answer generation
# ---------------------------------------------------------------------------

class QueryTestRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    machine_id: int | None = None
    top_k: int = Field(default=6, ge=1, le=20)
    # Superseded revisions are excluded from technician retrieval entirely.
    # This is the "explicit admin/audit flow" that can still see them -- off
    # by default so the tester shows what a technician would actually get
    # unless an admin deliberately asks to look wider.
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
def list_feedback(admin: CurrentUser = Depends(require_admin), limit: int = Query(default=100, ge=1, le=500)):
    """An admin triaging an "incorrect" report needs machine, model, and
    citation context, not just rating/comment/user/conversation_id, or they'd
    have to separately open the conversation (if they could even find it) to
    see what the technician was actually asking about. Reports message_id
    (feedback is per-answer, not per-conversation), the machine the answer
    was generated for, and every citation the answer actually used, batched
    in one extra query rather than N+1 per row."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT f.id, f.message_id, f.rating, f.comment, f.created_at, u.email AS user_email, "
            "m.content AS answer_content, m.conversation_id, m.provider, "
            "mf.name AS manufacturer, mach.model_name "
            "FROM feedback f "
            "JOIN users u ON u.id = f.user_id "
            "JOIN messages m ON m.id = f.message_id "
            "JOIN conversations c ON c.id = m.conversation_id "
            "LEFT JOIN machines mach ON mach.id = c.machine_id "
            "LEFT JOIN manufacturers mf ON mf.id = mach.manufacturer_id "
            "ORDER BY f.created_at DESC LIMIT %s",
            (limit,),
        ).fetchall()
        message_ids = [r["message_id"] for r in rows]
        citations_by_message: dict[int, list[str]] = {mid: [] for mid in message_ids}
        if message_ids:
            citation_rows = conn.execute(
                "SELECT ms.message_id, d.original_filename FROM message_sources ms "
                "JOIN chunks c ON c.id = ms.chunk_id JOIN documents d ON d.id = c.document_id "
                "WHERE ms.message_id = ANY(%s) AND ms.is_citation = true "
                "ORDER BY COALESCE(ms.citation_ordinal, ms.rank)",
                (message_ids,),
            ).fetchall()
            for cr in citation_rows:
                citations_by_message[cr["message_id"]].append(cr["original_filename"])
    return [
        {
            **dict(r),
            "machine_label": f"{r['manufacturer']} {r['model_name']}" if r["manufacturer"] else None,
            "citations": citations_by_message.get(r["message_id"], []),
        }
        for r in rows
    ]


@router.get("/unanswered")
def frequently_unanswered(admin: CurrentUser = Depends(require_admin), limit: int = Query(default=50, ge=1, le=200)):
    """Questions the system explicitly could not answer (is_no_answer=1 on the
    assistant's reply), most recent first — surfaces gaps in manual coverage."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT prev.content AS question, m.created_at, m.conversation_id "
            "FROM messages m "
            "JOIN messages prev ON prev.conversation_id = m.conversation_id AND prev.id = ("
            "  SELECT MAX(id) FROM messages WHERE conversation_id = m.conversation_id AND id < m.id AND role='user'"
            ") "
            "WHERE m.role = 'assistant' AND m.is_no_answer = true "
            "ORDER BY m.created_at DESC LIMIT %s",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.auth.deps import CurrentUser, require_admin
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
            conn.execute("DELETE FROM document_machines WHERE document_id = ?", (document_id,))
            for mid in payload.machine_ids:
                conn.execute(
                    "INSERT OR IGNORE INTO document_machines (document_id, machine_id, confidence) VALUES (?, ?, 1.0)",
                    (document_id, mid),
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


@router.post("/query-test")
def query_test(payload: QueryTestRequest, admin: CurrentUser = Depends(require_admin)):
    passages = hybrid_search(payload.question, machine_id=payload.machine_id, top_k=payload.top_k)
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

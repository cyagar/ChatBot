from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.auth.deps import CurrentUser, get_current_user
from app.db import get_conn

router = APIRouter(prefix="/api/machines", tags=["machines"])


class MachineOut(BaseModel):
    id: int
    manufacturer: str
    model_name: str
    family: str | None
    machine_type: str | None
    document_count: int
    is_favorite: bool = False
    last_used_at: str | None = None


def _row_to_machine(row) -> MachineOut:
    return MachineOut(
        id=row["id"],
        manufacturer=row["manufacturer"],
        model_name=row["model_name"],
        family=row["family"],
        machine_type=row["machine_type"],
        document_count=row["document_count"],
        is_favorite=bool(row["is_favorite"]) if "is_favorite" in row.keys() else False,
        last_used_at=row["last_used_at"] if "last_used_at" in row.keys() else None,
    )


@router.get("", response_model=list[MachineOut])
def search_machines(q: str = "", limit: int = 25):
    """Autocomplete search across model name, family, and manufacturer. Only
    machines that actually have at least one indexed document are returned, so
    the picker never dead-ends into a machine with no manual coverage."""
    sql = """
        SELECT m.id, mf.name AS manufacturer, m.model_name, m.family, m.machine_type,
               COUNT(DISTINCT dm.document_id) AS document_count
        FROM machines m
        JOIN manufacturers mf ON mf.id = m.manufacturer_id
        LEFT JOIN document_machines dm ON dm.machine_id = m.id
        LEFT JOIN documents d ON d.id = dm.document_id AND d.status IN ('indexed','partial') AND d.deactivated_at IS NULL
        WHERE (? = '' OR m.model_name LIKE ? OR m.family LIKE ? OR mf.name LIKE ?)
        GROUP BY m.id
        HAVING document_count > 0
        ORDER BY mf.name, m.model_name
        LIMIT ?
    """
    like = f"%{q}%"
    with get_conn() as conn:
        rows = conn.execute(sql, [q, like, like, like, limit]).fetchall()
    return [_row_to_machine(r) for r in rows]


@router.get("/recent", response_model=list[MachineOut])
def recent_machines(user: CurrentUser = Depends(get_current_user), limit: int = 10):
    sql = """
        SELECT m.id, mf.name AS manufacturer, m.model_name, m.family, m.machine_type,
               COUNT(DISTINCT dm.document_id) AS document_count,
               r.is_favorite, r.last_used_at
        FROM recent_machines r
        JOIN machines m ON m.id = r.machine_id
        JOIN manufacturers mf ON mf.id = m.manufacturer_id
        LEFT JOIN document_machines dm ON dm.machine_id = m.id
        WHERE r.user_id = ?
        GROUP BY m.id
        ORDER BY r.is_favorite DESC, r.last_used_at DESC
        LIMIT ?
    """
    with get_conn() as conn:
        rows = conn.execute(sql, [user.id, limit]).fetchall()
    return [_row_to_machine(r) for r in rows]


@router.post("/{machine_id}/touch")
def touch_recent(machine_id: int, user: CurrentUser = Depends(get_current_user)):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO recent_machines (user_id, machine_id, last_used_at) VALUES (?, ?, datetime('now')) "
            "ON CONFLICT(user_id, machine_id) DO UPDATE SET last_used_at = datetime('now')",
            (user.id, machine_id),
        )
    return {"ok": True}


@router.post("/{machine_id}/favorite")
def set_favorite(machine_id: int, favorite: bool = True, user: CurrentUser = Depends(get_current_user)):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO recent_machines (user_id, machine_id, last_used_at, is_favorite) "
            "VALUES (?, ?, datetime('now'), ?) "
            "ON CONFLICT(user_id, machine_id) DO UPDATE SET is_favorite = ?",
            (user.id, machine_id, int(favorite), int(favorite)),
        )
    return {"ok": True}

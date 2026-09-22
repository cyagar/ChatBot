from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, field_serializer

from app.api.common import iso_utc
from app.api.pagination import decode_cursor, paginate, set_pagination_headers
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
    # Postgres TIMESTAMPTZ comes back from psycopg as a real datetime, not a
    # string -- iso_utc() (app/api/common.py) renders the wire string.
    last_used_at: datetime | None = None

    @field_serializer("last_used_at")
    def _ser_ts(self, v: datetime | None) -> str | None:
        return iso_utc(v)


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
def search_machines(
    response: Response,
    q: str = "",
    limit: int = Query(default=25, ge=1, le=100),
    cursor: str | None = None,
    user: CurrentUser = Depends(get_current_user),
):
    """Autocomplete search across model name, family, and manufacturer. Only
    machines that actually have at least one indexed, approved, current
    document (with an approved link) are returned -- otherwise the picker
    would offer a machine that then dead-ends into "no manuals" the moment
    retrieval applies its own approval/revision filters (independent
    follow-up review P0-6, P1-6). The eligibility rules here are deliberately
    the same set retrieval enforces, including the current-revision rule
    (P1-11): a machine whose only manual is superseded has nothing
    retrievable and must not appear.

    Requires auth: the equipment catalog is proprietary to the deployment
    (concern #20 -- this endpoint leaked it to unauthenticated requests)."""
    # (manufacturer, model_name, id) -- id as the tiebreaker for stable
    # cursor pagination (Phase 1, narrowed scope), same reasoning as
    # routes_chat.py's list_conversations.
    before_mf, before_model, before_id = decode_cursor(cursor, 3) if cursor else (None, None, None)
    sql = """
        SELECT m.id, mf.name AS manufacturer, m.model_name, m.family, m.machine_type,
               COUNT(DISTINCT d.id) AS document_count,
               -- P1-12 (external review, 2026-09-21): this query had no join
               -- to recent_machines at all, so _row_to_machine's
               -- "is_favorite" key was always absent and defaulted to False
               -- -- a favorited machine always drew an empty star in search
               -- results, and the Android client's optimistic toggle (which
               -- flips !machine.is_favorite) would then send the WRONG
               -- direction on first tap. bool_or (not a plain column) so no
               -- new GROUP BY column is needed -- the r.user_id join
               -- condition already guarantees at most one recent_machines
               -- row per machine.
               COALESCE(bool_or(r.is_favorite), false) AS is_favorite
        FROM machines m
        JOIN manufacturers mf ON mf.id = m.manufacturer_id
        LEFT JOIN document_machines dm ON dm.machine_id = m.id AND dm.review_status = 'approved'
        LEFT JOIN documents d ON d.id = dm.document_id AND d.status IN ('indexed','partial')
            AND d.deactivated_at IS NULL AND d.review_status = 'approved'
            AND d.is_current_revision = true
        LEFT JOIN recent_machines r ON r.machine_id = m.id AND r.user_id = %s
        WHERE (%s = '' OR m.model_name ILIKE %s OR m.family ILIKE %s OR mf.name ILIKE %s)
        -- mf.name has to be in GROUP BY too -- Postgres's functional
        -- -dependency exception (grouping by a table's PK lets you select
        -- that table's other columns ungrouped) only covers m.id's own
        -- table (machines), not a joined table's columns. SQLite never
        -- enforced this at all.
        GROUP BY m.id, mf.name
        -- Postgres (unlike SQLite) evaluates HAVING before the SELECT list,
        -- so it can't see the "document_count" alias -- repeat the aggregate.
        HAVING COUNT(DISTINCT d.id) > 0
            AND (
                %s::text IS NULL
                OR mf.name > %s
                OR (mf.name = %s AND m.model_name > %s)
                OR (mf.name = %s AND m.model_name = %s AND m.id > %s)
            )
        ORDER BY mf.name, m.model_name, m.id
        LIMIT %s
    """
    # ILIKE, not LIKE -- SQLite's LIKE is case-insensitive by default for
    # ASCII, Postgres's is case-sensitive. Using plain LIKE here would have
    # silently broken this autocomplete search for any query not matching
    # the stored casing exactly (e.g. "axiom" no longer finding "Axiom").
    like = f"%{q}%"
    with get_conn() as conn:
        rows = conn.execute(sql, [
            user.id,
            q, like, like, like,
            before_mf, before_mf, before_mf, before_model, before_mf, before_model, before_id,
            limit + 1,
        ]).fetchall()
    rows, next_cursor = paginate(rows, limit, lambda r: (r["manufacturer"], r["model_name"], r["id"]))
    set_pagination_headers(response, next_cursor)
    return [_row_to_machine(r) for r in rows]


@router.get("/recent", response_model=list[MachineOut])
def recent_machines(
    response: Response,
    user: CurrentUser = Depends(get_current_user),
    limit: int = Query(default=10, ge=1, le=100),
    cursor: str | None = None,
):
    """Applies the exact same eligibility rules and `HAVING document_count > 0`
    as `search_machines()` above -- a machine a technician favorited or
    recently used, whose only manual has since been deactivated/unapproved/
    superseded, is dropped rather than shown with `document_count: 0`
    (independent follow-up review P1-6's "apply the rules consistently to
    search and recent machines": found, during this pass, that this endpoint
    was missing the `HAVING` clause `search_machines()` already had, so a
    dead manual could resurface here even though the picker correctly hid
    it). The tradeoff is explicit: a favorited-but-now-empty machine
    disappears from recents instead of dead-ending into "no manuals" --
    consistent with what the picker already does, not a new UX decision."""
    before_fav, before_last_used, before_id = decode_cursor(cursor, 3) if cursor else (None, None, None)
    sql = """
        SELECT m.id, mf.name AS manufacturer, m.model_name, m.family, m.machine_type,
               COUNT(DISTINCT d.id) AS document_count,
               r.is_favorite, r.last_used_at
        FROM recent_machines r
        JOIN machines m ON m.id = r.machine_id
        JOIN manufacturers mf ON mf.id = m.manufacturer_id
        LEFT JOIN document_machines dm ON dm.machine_id = m.id AND dm.review_status = 'approved'
        LEFT JOIN documents d ON d.id = dm.document_id AND d.status IN ('indexed','partial')
            AND d.deactivated_at IS NULL AND d.review_status = 'approved'
            AND d.is_current_revision = true
        WHERE r.user_id = %s
        -- mf.name, r.is_favorite, r.last_used_at all need to be in GROUP BY
        -- too -- see search_machines()'s comment above on why m.id alone
        -- isn't enough for Postgres once other tables' columns are selected.
        -- Doesn't change the grouping in practice: recent_machines' real PK
        -- is (user_id, machine_id), and user_id is fixed by the WHERE
        -- clause, so each m.id still gets exactly one row/group.
        GROUP BY m.id, mf.name, r.is_favorite, r.last_used_at
        -- Postgres (unlike SQLite) evaluates HAVING before the SELECT list,
        -- so it can't see the "document_count" alias -- repeat the aggregate.
        HAVING COUNT(DISTINCT d.id) > 0
            AND (
                %s::boolean IS NULL
                OR r.is_favorite < %s
                OR (r.is_favorite = %s AND r.last_used_at < %s)
                OR (r.is_favorite = %s AND r.last_used_at = %s AND m.id < %s)
            )
        ORDER BY r.is_favorite DESC, r.last_used_at DESC, m.id DESC
        LIMIT %s
    """
    with get_conn() as conn:
        rows = conn.execute(sql, [
            user.id,
            before_fav, before_fav, before_fav, before_last_used, before_fav, before_last_used, before_id,
            limit + 1,
        ]).fetchall()
    rows, next_cursor = paginate(rows, limit, lambda r: (r["is_favorite"], r["last_used_at"], r["id"]))
    set_pagination_headers(response, next_cursor)
    return [_row_to_machine(r) for r in rows]


def _require_machine(conn, machine_id: int) -> None:
    """P1-11 (independent follow-up review): touching recent/favorite state
    for a nonexistent machine_id used to hit recent_machines' foreign key
    directly and surface as an unhandled 500 -- validate up front and return
    a normal 404 instead."""
    if conn.execute("SELECT 1 FROM machines WHERE id = %s", (machine_id,)).fetchone() is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Machine not found.")


@router.post("/{machine_id}/touch")
def touch_recent(machine_id: int, user: CurrentUser = Depends(get_current_user)):
    with get_conn() as conn:
        _require_machine(conn, machine_id)
        conn.execute(
            "INSERT INTO recent_machines (user_id, machine_id, last_used_at) VALUES (%s, %s, now()) "
            "ON CONFLICT(user_id, machine_id) DO UPDATE SET last_used_at = now()",
            (user.id, machine_id),
        )
    return {"ok": True}


@router.post("/{machine_id}/favorite")
def set_favorite(machine_id: int, favorite: bool = True, user: CurrentUser = Depends(get_current_user)):
    with get_conn() as conn:
        _require_machine(conn, machine_id)
        conn.execute(
            "INSERT INTO recent_machines (user_id, machine_id, last_used_at, is_favorite) "
            "VALUES (%s, %s, now(), %s) "
            "ON CONFLICT(user_id, machine_id) DO UPDATE SET is_favorite = %s",
            (user.id, machine_id, favorite, favorite),
        )
    return {"ok": True}

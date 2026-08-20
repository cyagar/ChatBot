from __future__ import annotations

from dataclasses import dataclass

from fastapi import Cookie, Depends, HTTPException, status

from app.auth.security import decode_session_token
from app.db import get_conn

SESSION_COOKIE = "tma_session"


@dataclass
class CurrentUser:
    id: int
    email: str
    role: str
    display_name: str | None


def get_current_user(tma_session: str | None = Cookie(default=None, alias=SESSION_COOKIE)) -> CurrentUser:
    if not tma_session:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Not authenticated.")
    payload = decode_session_token(tma_session)
    if not payload:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Session expired or invalid.")

    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, email, role, display_name FROM users WHERE id = ?", (int(payload["sub"]),)
        ).fetchone()
    if not row:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="User no longer exists.")

    return CurrentUser(id=row["id"], email=row["email"], role=row["role"], display_name=row["display_name"])


def require_admin(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    if user.role != "administrator":
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Administrator role required.")
    return user

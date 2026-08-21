from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, EmailStr, Field

from app.auth.audit import log_audit_event
from app.auth.deps import SESSION_COOKIE, CurrentUser, get_current_user
from app.auth.security import (
    create_session_token,
    hash_invitation_token,
    hash_password,
    verify_password,
)
from app.config import get_settings
from app.db import get_conn
from app.rate_limit import AUTH_RATE_LIMIT, limiter

router = APIRouter(prefix="/api/auth", tags=["auth"])


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=72)  # bcrypt's hard limit is 72 bytes
    display_name: str | None = Field(default=None, max_length=100)
    invite_token: str = Field(min_length=1, max_length=200)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=200)


class UserOut(BaseModel):
    id: int
    email: str
    role: str
    display_name: str | None


def _set_session_cookie(response: Response, user_id: int, role: str, token_version: int):
    settings = get_settings()
    token = create_session_token(user_id, role, token_version)
    response.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        httponly=True,
        secure=settings.app_env != "development",
        samesite="lax",
        max_age=settings.session_ttl_minutes * 60,
    )


@router.post("/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
@limiter.limit(AUTH_RATE_LIMIT)
def register(payload: RegisterRequest, request: Request, response: Response):
    """Independent follow-up review P0-5: public self-registration used to let
    anyone become administrator by winning a race to register first, and any
    other email through unconditionally once domain-restricted. Registration
    now requires a valid, unexpired, unused, email-bound invitation issued by
    an existing administrator (POST /api/admin/invitations) -- there is no
    path from an anonymous request to an account anymore. The very first
    administrator is created by scripts/bootstrap_admin.py, not this endpoint.
    """
    token_hash = hash_invitation_token(payload.invite_token)
    now = datetime.now(timezone.utc).isoformat()

    with get_conn() as conn:
        invite = conn.execute(
            "SELECT id, email, role, expires_at, used_at, revoked_at FROM invitations WHERE token_hash = ?",
            (token_hash,),
        ).fetchone()
        if invite is None:
            raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Invalid invitation.")
        if invite["used_at"] is not None:
            raise HTTPException(status.HTTP_403_FORBIDDEN, detail="This invitation has already been used.")
        if invite["revoked_at"] is not None:
            raise HTTPException(status.HTTP_403_FORBIDDEN, detail="This invitation has been revoked.")
        if invite["expires_at"] <= now:
            raise HTTPException(status.HTTP_403_FORBIDDEN, detail="This invitation has expired.")
        if invite["email"].lower() != payload.email.lower():
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                detail="This invitation was issued for a different email address.",
            )

        existing = conn.execute("SELECT id FROM users WHERE email = ?", (payload.email,)).fetchone()
        if existing:
            raise HTTPException(status.HTTP_409_CONFLICT, detail="An account with this email already exists.")

        cur = conn.execute(
            "INSERT INTO users (email, password_hash, role, display_name) VALUES (?, ?, ?, ?)",
            (payload.email, hash_password(payload.password), invite["role"], payload.display_name),
        )
        user_id = cur.lastrowid
        conn.execute(
            "UPDATE invitations SET used_at = datetime('now'), used_by = ? WHERE id = ?",
            (user_id, invite["id"]),
        )
        log_audit_event(conn, "invite_used", actor_user_id=user_id, target_type="invitation",
                         target_id=invite["id"], detail=f"Registered as {invite['role']} via invitation.")

    # 0 matches the `users.token_version` column DEFAULT used by this INSERT
    # (not read back) -- if that default ever changes, this literal must move too.
    _set_session_cookie(response, user_id, invite["role"], token_version=0)
    return UserOut(id=user_id, email=payload.email, role=invite["role"], display_name=payload.display_name)


@router.post("/login", response_model=UserOut)
@limiter.limit(AUTH_RATE_LIMIT)
def login(payload: LoginRequest, request: Request, response: Response):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, email, password_hash, role, display_name, is_disabled, token_version "
            "FROM users WHERE email = ?",
            (payload.email,),
        ).fetchone()
        if not row or not verify_password(payload.password, row["password_hash"]):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password.")
        if row["is_disabled"]:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="This account has been disabled.")
        conn.execute("UPDATE users SET last_login_at = datetime('now') WHERE id = ?", (row["id"],))

    _set_session_cookie(response, row["id"], row["role"], row["token_version"])
    return UserOut(id=row["id"], email=row["email"], role=row["role"], display_name=row["display_name"])


@router.post("/logout")
def logout(response: Response):
    response.delete_cookie(SESSION_COOKIE)
    return {"ok": True}


@router.get("/me", response_model=UserOut)
def me(user: CurrentUser = Depends(get_current_user)):
    return UserOut(id=user.id, email=user.email, role=user.role, display_name=user.display_name)

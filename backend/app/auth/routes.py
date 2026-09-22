from __future__ import annotations

from datetime import datetime, timezone

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, EmailStr, Field, field_validator

from app.auth.audit import log_audit_event
from app.auth.deps import SESSION_COOKIE, CurrentUser, get_current_user
from app.auth.security import (
    create_session_token,
    hash_invitation_token,
    hash_password,
    normalize_email,
    verify_password,
)
from app.config import get_settings
from app.db import get_conn
from app.rate_limit import AUTH_RATE_LIMIT, auth_key_func, limiter

router = APIRouter(prefix="/api/auth", tags=["auth"])


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=72)
    display_name: str | None = Field(default=None, max_length=100)
    invite_token: str = Field(min_length=1, max_length=200)

    @field_validator("password")
    @classmethod
    def _password_fits_bcrypt(cls, v: str) -> str:
        """P0-5 (independent follow-up review): max_length=72 above counts
        *characters*, but bcrypt's hard limit is 72 UTF-8 *bytes* -- a
        40-emoji password can be under 72 characters yet well over 72 bytes,
        which used to reach app.auth.security.hash_password's own byte check
        and raise an uncaught ValueError (an unhandled 500) instead of a
        normal 422 naming the problem."""
        if len(v.encode("utf-8")) > 72:
            raise ValueError("Password must be at most 72 bytes when UTF-8 encoded.")
        return v


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=200)


class UserOut(BaseModel):
    id: int
    email: str
    role: str
    display_name: str | None
    # Phase 1 (narrowed scope, 2026-08-26): "role/capabilities". Mechanical,
    # not a new permission system -- every technician-facing capability is
    # available to any authenticated user (there's no per-technician
    # variation), and the administrator-only ones are exactly the routes
    # app.auth.deps.require_admin actually gates (routes_admin.py). This
    # lists what the server already enforces; it does not itself enforce
    # anything.
    capabilities: list[str] = Field(default_factory=list)


_TECHNICIAN_CAPABILITIES = [
    "ask_questions",
    "search_machines",
    "view_history",
    "save_answers",
]
_ADMINISTRATOR_ONLY_CAPABILITIES = [
    "manage_documents",
    "manage_users",
    "manage_invitations",
    "review_corpus",
    "run_ingestion",
    "view_admin_reports",
]


def _capabilities_for_role(role: str) -> list[str]:
    if role == "administrator":
        return _TECHNICIAN_CAPABILITIES + _ADMINISTRATOR_ONLY_CAPABILITIES
    return list(_TECHNICIAN_CAPABILITIES)


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
@limiter.limit(AUTH_RATE_LIMIT, key_func=auth_key_func)
def register(payload: RegisterRequest, request: Request, response: Response):
    """Independent follow-up review P0-5: public self-registration used to let
    anyone become administrator by winning a race to register first, and any
    other email through unconditionally once domain-restricted. Registration
    now requires a valid, unexpired, unused, email-bound invitation issued by
    an existing administrator (POST /api/admin/invitations) -- there is no
    path from an anonymous request to an account anymore. The very first
    administrator is created by scripts/bootstrap_admin.py, not this endpoint.

    P1-6 (2026-08-24 independent follow-up review, "concurrent ...
    invitation ... tests"): the invite used to be consumed with a
    check-then-act read (the `used_at is not None` check below) followed by
    an unconditional UPDATE at the end -- two requests racing on the SAME
    invite token both read used_at=NULL and both proceeded to INSERT a user,
    relying entirely on users.email's UNIQUE constraint to stop the second
    one. That constraint does stop a duplicate account, but the loser's
    INSERT raised an unhandled sqlite3.IntegrityError instead of the same
    clean 403 every other invitation-rejection path returns. The invite is
    now claimed atomically, before any user row is touched, via the same
    claim-UPDATE pattern used everywhere else in this codebase for exactly
    this reason (conversations.pending_message_id, messages.answer_status):
    only a request that flips used_at from NULL to non-NULL proceeds.
    """
    token_hash = hash_invitation_token(payload.invite_token)
    # P1-20 (external review, 2026-09-21): every read/write of this address
    # from here on uses the normalized form -- see normalize_email's
    # docstring. Stored this way (not as-entered), so a technician who typed
    # mixed case at registration can still log in with any casing later.
    email = normalize_email(payload.email)
    # invitations.expires_at is TIMESTAMPTZ -- psycopg hands it back as a
    # real tz-aware datetime (unlike SQLite's TEXT column, which forced an
    # isoformat-string comparison), so `now` must be one too.
    now = datetime.now(timezone.utc)

    with get_conn() as conn:
        invite = conn.execute(
            "SELECT id, email, role, expires_at, used_at, revoked_at FROM invitations WHERE token_hash = %s",
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
        if normalize_email(invite["email"]) != email:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                detail="This invitation was issued for a different email address.",
            )

        claim = conn.execute(
            "UPDATE invitations SET used_at = now() WHERE id = %s AND used_at IS NULL",
            (invite["id"],),
        )
        if claim.rowcount == 0:
            # Lost the race to a concurrent request for this same token.
            raise HTTPException(status.HTTP_403_FORBIDDEN, detail="This invitation has already been used.")

        existing = conn.execute("SELECT id FROM users WHERE email = %s", (email,)).fetchone()
        if existing:
            conn.execute("UPDATE invitations SET used_at = NULL WHERE id = %s", (invite["id"],))
            raise HTTPException(status.HTTP_409_CONFLICT, detail="An account with this email already exists.")

        try:
            # A nested transaction (SAVEPOINT under the connection's already
            # -open outer transaction) -- unlike sqlite3, a Postgres
            # constraint violation aborts the whole transaction until a
            # ROLLBACK, so without this savepoint the recovery UPDATE in the
            # except block below would itself fail with
            # InFailedSqlTransaction instead of running.
            with conn.transaction():
                cur = conn.execute(
                    "INSERT INTO users (email, password_hash, role, display_name) "
                    "VALUES (%s, %s, %s, %s) RETURNING id",
                    (email, hash_password(payload.password), invite["role"], payload.display_name),
                )
                user_id = cur.fetchone()["id"]
        except psycopg.errors.UniqueViolation:
            # A DIFFERENT invitation for the same email, redeemed concurrently
            # with this one, can still slip past the "existing" check above
            # (each connection's read happens before either commits) --
            # users.email's UNIQUE constraint is the actual backstop for that
            # case. Restore the claim so this invite isn't burned for an
            # account that was never created.
            conn.execute("UPDATE invitations SET used_at = NULL WHERE id = %s", (invite["id"],))
            raise HTTPException(status.HTTP_409_CONFLICT, detail="An account with this email already exists.")
        conn.execute("UPDATE invitations SET used_by = %s WHERE id = %s", (user_id, invite["id"]))
        log_audit_event(conn, "invite_used", actor_user_id=user_id, target_type="invitation",
                         target_id=invite["id"], detail=f"Registered as {invite['role']} via invitation.")

    # 0 matches the `users.token_version` column DEFAULT used by this INSERT
    # (not read back) -- if that default ever changes, this literal must move too.
    _set_session_cookie(response, user_id, invite["role"], token_version=0)
    return UserOut(id=user_id, email=email, role=invite["role"], display_name=payload.display_name,
                   capabilities=_capabilities_for_role(invite["role"]))


@router.post("/login", response_model=UserOut)
@limiter.limit(AUTH_RATE_LIMIT, key_func=auth_key_func)
def login(payload: LoginRequest, request: Request, response: Response):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, email, password_hash, role, display_name, is_disabled, token_version "
            "FROM users WHERE email = %s",
            (normalize_email(payload.email),),
        ).fetchone()
        if not row or not verify_password(payload.password, row["password_hash"]):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password.")
        if row["is_disabled"]:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="This account has been disabled.")
        conn.execute("UPDATE users SET last_login_at = now() WHERE id = %s", (row["id"],))

    _set_session_cookie(response, row["id"], row["role"], row["token_version"])
    return UserOut(id=row["id"], email=row["email"], role=row["role"], display_name=row["display_name"],
                   capabilities=_capabilities_for_role(row["role"]))


@router.post("/logout")
def logout(response: Response):
    response.delete_cookie(SESSION_COOKIE)
    return {"ok": True}


@router.get("/me", response_model=UserOut)
def me(user: CurrentUser = Depends(get_current_user)):
    return UserOut(id=user.id, email=user.email, role=user.role, display_name=user.display_name,
                   capabilities=_capabilities_for_role(user.role))

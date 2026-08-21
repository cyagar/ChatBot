from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, EmailStr, Field

from app.auth.deps import SESSION_COOKIE, CurrentUser, get_current_user
from app.auth.security import create_session_token, hash_password, verify_password
from app.config import get_settings
from app.db import get_conn
from app.rate_limit import AUTH_RATE_LIMIT, limiter

router = APIRouter(prefix="/api/auth", tags=["auth"])


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=72)  # bcrypt's hard limit is 72 bytes
    display_name: str | None = Field(default=None, max_length=100)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=200)


class UserOut(BaseModel):
    id: int
    email: str
    role: str
    display_name: str | None


def _set_session_cookie(response: Response, user_id: int, role: str):
    settings = get_settings()
    token = create_session_token(user_id, role)
    response.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        httponly=True,
        secure=settings.app_env != "development",
        samesite="lax",
        max_age=settings.session_ttl_minutes * 60,
    )


def _check_registration_allowed(email: str) -> None:
    allowed = [d.strip().lower() for d in get_settings().allowed_registration_domains.split(",") if d.strip()]
    if not allowed:
        return
    domain = email.rsplit("@", 1)[-1].lower()
    if domain not in allowed:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail="Registration is restricted to approved email domains. Contact an administrator for access.",
        )


@router.post("/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
@limiter.limit(AUTH_RATE_LIMIT)
def register(payload: RegisterRequest, request: Request, response: Response):
    _check_registration_allowed(payload.email)
    with get_conn() as conn:
        existing = conn.execute("SELECT id FROM users WHERE email = ?", (payload.email,)).fetchone()
        if existing:
            raise HTTPException(status.HTTP_409_CONFLICT, detail="An account with this email already exists.")
        # First-ever account becomes administrator so the system is bootstrap-able
        # without direct DB access; every subsequent signup is a technician.
        # Still subject to the domain check above once allowed_registration_domains
        # is set, so an attacker can't win the race to be "first" from outside.
        user_count = conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
        role = "administrator" if user_count == 0 else "technician"
        cur = conn.execute(
            "INSERT INTO users (email, password_hash, role, display_name) VALUES (?, ?, ?, ?)",
            (payload.email, hash_password(payload.password), role, payload.display_name),
        )
        user_id = cur.lastrowid

    _set_session_cookie(response, user_id, role)
    return UserOut(id=user_id, email=payload.email, role=role, display_name=payload.display_name)


@router.post("/login", response_model=UserOut)
@limiter.limit(AUTH_RATE_LIMIT)
def login(payload: LoginRequest, request: Request, response: Response):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, email, password_hash, role, display_name FROM users WHERE email = ?",
            (payload.email,),
        ).fetchone()
        if not row or not verify_password(payload.password, row["password_hash"]):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password.")
        conn.execute("UPDATE users SET last_login_at = datetime('now') WHERE id = ?", (row["id"],))

    _set_session_cookie(response, row["id"], row["role"])
    return UserOut(id=row["id"], email=row["email"], role=row["role"], display_name=row["display_name"])


@router.post("/logout")
def logout(response: Response):
    response.delete_cookie(SESSION_COOKIE)
    return {"ok": True}


@router.get("/me", response_model=UserOut)
def me(user: CurrentUser = Depends(get_current_user)):
    return UserOut(id=user.id, email=user.email, role=user.role, display_name=user.display_name)

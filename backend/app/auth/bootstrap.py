"""Creates the very first administrator account, out-of-band from any public
HTTP endpoint.

Public self-registration does not grant the administrator role at all -- an
administrator can only be created here, and only while the users table is
empty, so this can never mint a second uncontrolled admin by accident
(letting anyone who won a race to register first on a fresh deployment
become administrator would be the alternative failure mode).

Email/password are validated with the same rules as public registration
(`app.auth.routes.RegisterRequest`), enforced here rather than only at the
CLI layer, so no future caller can bypass them and mint an administrator
with no working credential (an empty password, or an unparseable email).
"""

from __future__ import annotations

from pydantic import BaseModel, EmailStr, Field, ValidationError

from app.auth.audit import log_audit_event
from app.auth.security import hash_password, normalize_email
from app.db import get_conn


class _BootstrapCredentials(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=72)  # bcrypt's hard limit is 72 bytes


def bootstrap_admin(email: str, password: str, display_name: str | None = None) -> int:
    try:
        creds = _BootstrapCredentials(email=email, password=password)
    except ValidationError as exc:
        raise ValueError(f"Invalid administrator credentials: {exc}") from exc
    # Must be normalized before storing -- see normalize_email's docstring.
    email = normalize_email(creds.email)
    password = creds.password

    with get_conn() as conn:
        existing = conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
        if existing > 0:
            raise RuntimeError(
                f"Refusing to bootstrap: {existing} user(s) already exist. "
                "Use the admin invitation flow (POST /api/admin/invitations) to add more accounts."
            )
        cur = conn.execute(
            "INSERT INTO users (email, password_hash, role, display_name) "
            "VALUES (%s, %s, 'administrator', %s) RETURNING id",
            (email, hash_password(password), display_name),
        )
        user_id = cur.fetchone()["id"]
        log_audit_event(conn, "admin_bootstrap", actor_user_id=user_id, target_type="user",
                         target_id=user_id, detail=f"Bootstrap administrator created: {email}")
    return user_id

"""Creates the very first administrator account, out-of-band from any public
HTTP endpoint.

Independent follow-up review P0-5: the previous design let anyone who won the
race to register first on a fresh deployment become administrator. Public
self-registration no longer grants that role at all -- an administrator can
only be created here, and only while the users table is empty, so this can
never mint a second uncontrolled admin by accident.

Independent follow-up review 2026-08-24 P0-8: this function accepted any
string as an email and any password, including an empty one, so a rushed or
scripted bootstrap could mint an administrator with no working credential.
Email/password are now validated with the same rules as public registration
(`app.auth.routes.RegisterRequest`), enforced here rather than only at the
CLI layer, so no future caller can bypass them.
"""

from __future__ import annotations

from pydantic import BaseModel, EmailStr, Field, ValidationError

from app.auth.audit import log_audit_event
from app.auth.security import hash_password
from app.db import get_conn


class _BootstrapCredentials(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=72)  # bcrypt's hard limit is 72 bytes


def bootstrap_admin(email: str, password: str, display_name: str | None = None) -> int:
    try:
        creds = _BootstrapCredentials(email=email, password=password)
    except ValidationError as exc:
        raise ValueError(f"Invalid administrator credentials: {exc}") from exc
    email = creds.email
    password = creds.password

    with get_conn() as conn:
        existing = conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
        if existing > 0:
            raise RuntimeError(
                f"Refusing to bootstrap: {existing} user(s) already exist. "
                "Use the admin invitation flow (POST /api/admin/invitations) to add more accounts."
            )
        cur = conn.execute(
            "INSERT INTO users (email, password_hash, role, display_name) VALUES (?, ?, 'administrator', ?)",
            (email, hash_password(password), display_name),
        )
        user_id = cur.lastrowid
        log_audit_event(conn, "admin_bootstrap", actor_user_id=user_id, target_type="user",
                         target_id=user_id, detail=f"Bootstrap administrator created: {email}")
    return user_id

"""Creates the very first administrator account, out-of-band from any public
HTTP endpoint.

Independent follow-up review P0-5: the previous design let anyone who won the
race to register first on a fresh deployment become administrator. Public
self-registration no longer grants that role at all -- an administrator can
only be created here, and only while the users table is empty, so this can
never mint a second uncontrolled admin by accident.
"""

from __future__ import annotations

from app.auth.audit import log_audit_event
from app.auth.security import hash_password
from app.db import get_conn


def bootstrap_admin(email: str, password: str, display_name: str | None = None) -> int:
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

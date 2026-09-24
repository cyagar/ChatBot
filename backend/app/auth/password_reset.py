"""Operator-run password reset. There is no self-service reset, so an account
whose password is lost or was ever a seeded/demo credential is recovered here."""

from __future__ import annotations

from app.auth.audit import log_audit_event
from app.auth.security import hash_password, normalize_email
from app.db import get_conn

MIN_PASSWORD_LENGTH = 8


def reset_password(email: str, new_password: str) -> int:
    """Sets a new password and bumps token_version so every session issued to
    the account stops working. Returns the user id; raises LookupError when no
    such account exists."""
    if len(new_password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
    with get_conn() as conn:
        row = conn.execute(
            "UPDATE users SET password_hash = %s, token_version = token_version + 1 "
            "WHERE email = %s RETURNING id",
            (hash_password(new_password), normalize_email(email)),
        ).fetchone()
        if row is None:
            raise LookupError(f"No account with email {email!r}.")
        log_audit_event(conn, "password_reset", target_type="user", target_id=row["id"],
                         detail="Password reset by an operator with backend access.")
    return row["id"]

from __future__ import annotations

import hashlib
import secrets
import time

import bcrypt
import jwt

from app.config import get_settings

JWT_ALGORITHM = "HS256"
# bcrypt silently truncates/errors past 72 bytes; reject rather than let two
# different long passwords hash identically past that point.
MAX_PASSWORD_BYTES = 72


def hash_password(password: str) -> str:
    if len(password.encode("utf-8")) > MAX_PASSWORD_BYTES:
        raise ValueError(f"Password must be at most {MAX_PASSWORD_BYTES} bytes.")
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("ascii")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("ascii"))
    except Exception:
        return False


def create_session_token(user_id: int, role: str, token_version: int) -> str:
    """token_version is embedded so a stateless JWT can still be revoked before
    its natural expiry: disabling a user (or any future "sign out everywhere"
    action) bumps users.token_version in the DB, and any token minted before
    that bump stops verifying (see decode_session_token's caller in
    app/auth/deps.py, which compares this against the current DB value)."""
    settings = get_settings()
    now = int(time.time())
    payload = {
        "sub": str(user_id),
        "role": role,
        "tv": token_version,
        "iat": now,
        "exp": now + settings.session_ttl_minutes * 60,
    }
    return jwt.encode(payload, settings.secret_key, algorithm=JWT_ALGORITHM)


def decode_session_token(token: str) -> dict | None:
    settings = get_settings()
    try:
        return jwt.decode(token, settings.secret_key, algorithms=[JWT_ALGORITHM])
    except jwt.PyJWTError:
        return None


def generate_invitation_token() -> tuple[str, str]:
    """Returns (raw_token, token_hash). Only the hash is ever persisted -- the
    raw token is shown to the inviting admin exactly once, the same way a
    password reset link would be, so a DB read alone can never be used to
    register as the invited user."""
    raw = secrets.token_urlsafe(32)
    return raw, hash_invitation_token(raw)


def hash_invitation_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()

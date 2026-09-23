"""CLI: create or reset a local-only technician demo account.

Never prints or commits a password; running it again with a new --password
rotates the existing account instead of failing.

Refuses to run outside APP_ENV=development, same spirit as
`Settings.validate_for_startup()` -- this is dev-only seeding, not something
to run against a real deployment.

Usage (from backend/):
    py scripts/seed_dev_technician.py
    (defaults to tech.demo@hmwagner.com; prompts for a password rather than
    taking one on the command line, so it doesn't land in shell history)

    py scripts/seed_dev_technician.py --email someone@example.com --display-name "Demo Tech"
"""

import argparse
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pydantic import BaseModel, EmailStr, Field, ValidationError

from app.auth.security import hash_password
from app.config import get_settings
from app.db import get_conn, run_migrations

DEFAULT_EMAIL = "tech.demo@hmwagner.com"
DEFAULT_DISPLAY_NAME = "Demo Technician"


class _SeedCredentials(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=72)  # bcrypt's hard limit is 72 bytes


def seed_dev_technician(email: str, password: str, display_name: str | None = None) -> tuple[int, bool]:
    """Returns (user_id, created) -- created is False when an existing row was updated instead."""
    settings = get_settings()
    if settings.app_env != "development":
        raise RuntimeError(
            f"Refusing to seed a demo account outside development (APP_ENV={settings.app_env!r})."
        )
    creds = _SeedCredentials(email=email, password=password)
    password_hash = hash_password(creds.password)

    with get_conn() as conn:
        existing = conn.execute("SELECT id FROM users WHERE email = %s", (creds.email,)).fetchone()
        if existing:
            conn.execute(
                "UPDATE users SET password_hash = %s, display_name = %s, token_version = token_version + 1 WHERE id = %s",
                (password_hash, display_name, existing["id"]),
            )
            return existing["id"], False
        cur = conn.execute(
            "INSERT INTO users (email, password_hash, role, display_name) VALUES (%s, %s, 'technician', %s) RETURNING id",
            (creds.email, password_hash, display_name),
        )
        return cur.fetchone()["id"], True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--email", default=DEFAULT_EMAIL)
    parser.add_argument("--display-name", default=DEFAULT_DISPLAY_NAME)
    parser.add_argument("--password", default=None, help="Omit to be prompted (recommended).")
    args = parser.parse_args()

    password = args.password or getpass.getpass("Demo technician password: ")

    run_migrations()
    try:
        user_id, created = seed_dev_technician(args.email, password, args.display_name)
    except (ValueError, ValidationError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
    verb = "Created" if created else "Updated (rotated password for)"
    print(f"{verb} technician account {args.email!r} (user id {user_id}).")


if __name__ == "__main__":
    main()

"""CLI: reset a user's password (and end all of their sessions).

Usage (from backend/, or `docker compose exec app` in the container):
    py scripts/reset_password.py --email tech@example.com
    (prompts for the new password so it doesn't land in shell history)
"""

import argparse
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.auth.password_reset import reset_password
from app.db import run_migrations


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--email", required=True)
    parser.add_argument("--password", default=None, help="Omit to be prompted (recommended).")
    args = parser.parse_args()

    password = args.password or getpass.getpass("New password: ")
    run_migrations()
    try:
        user_id = reset_password(args.email, password)
    except (ValueError, LookupError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
    print(f"Password reset for {args.email!r} (user id {user_id}); existing sessions are invalidated.")


if __name__ == "__main__":
    main()

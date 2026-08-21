"""CLI: create the very first administrator account.

Public self-registration can no longer create an administrator (independent
follow-up review P0-5: the old "first HTTP registrant becomes admin" design
was a public race). This only works while the users table is empty.

Usage (from backend/, or `docker compose exec app` in the container):
    py scripts/bootstrap_admin.py --email admin@example.com
    (prompts for a password rather than taking one on the command line, so it
    doesn't land in shell history)
"""

import argparse
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.auth.bootstrap import bootstrap_admin
from app.db import run_migrations


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--email", required=True)
    parser.add_argument("--display-name", default=None)
    parser.add_argument("--password", default=None, help="Omit to be prompted (recommended).")
    args = parser.parse_args()

    password = args.password or getpass.getpass("Administrator password: ")
    if len(password.encode("utf-8")) > 72:
        print("Password must be at most 72 bytes.", file=sys.stderr)
        raise SystemExit(1)

    run_migrations()
    user_id = bootstrap_admin(args.email, password, args.display_name)
    print(f"Created administrator {args.email!r} (user id {user_id}).")


if __name__ == "__main__":
    main()

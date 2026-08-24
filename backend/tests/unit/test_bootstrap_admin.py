"""Independent follow-up review 2026-08-24 P0-8: bootstrap_admin() accepted
any string as an email (e.g. "not-an-email") and any password, including an
empty one -- so a rushed or scripted bootstrap could mint an administrator
with no working credential. These tests cover the validation now enforced in
app.auth.bootstrap.bootstrap_admin itself (not just the CLI), using the same
rules as public registration.
"""

from __future__ import annotations

import pytest

from app.auth.bootstrap import bootstrap_admin
from app.db import get_conn


def test_rejects_invalid_email(test_env):
    with pytest.raises(ValueError):
        bootstrap_admin("not-an-email", "a-valid-password-123")
    with get_conn() as conn:
        count = conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
    assert count == 0


def test_rejects_blank_password(test_env):
    with pytest.raises(ValueError):
        bootstrap_admin("admin@example.com", "")
    with get_conn() as conn:
        count = conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
    assert count == 0


def test_rejects_short_password(test_env):
    with pytest.raises(ValueError):
        bootstrap_admin("admin@example.com", "short1")
    with get_conn() as conn:
        count = conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
    assert count == 0


def test_valid_credentials_succeed(test_env):
    user_id = bootstrap_admin("admin@example.com", "a-valid-password-123")
    with get_conn() as conn:
        row = conn.execute("SELECT email, role FROM users WHERE id = ?", (user_id,)).fetchone()
    assert row["email"] == "admin@example.com"
    assert row["role"] == "administrator"


def test_refuses_when_a_user_already_exists(test_env):
    bootstrap_admin("first-admin@example.com", "a-valid-password-123")
    with pytest.raises(RuntimeError):
        bootstrap_admin("second-admin@example.com", "another-valid-password-123")

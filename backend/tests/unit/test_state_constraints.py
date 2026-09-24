"""The database itself refuses states the application treats as impossible."""
from __future__ import annotations

import psycopg
import pytest

from app.db import get_conn


def _expect_rejected(sql, params=()):
    with pytest.raises(psycopg.errors.CheckViolation):
        with get_conn() as conn:
            conn.execute(sql, params)


def test_unknown_user_role_is_rejected(test_env):
    _expect_rejected("INSERT INTO users (email, password_hash, role) VALUES ('x@example.com', 'h', 'superuser')")


def test_unknown_message_role_and_answer_status_are_rejected(test_env):
    with get_conn() as conn:
        conn.execute("INSERT INTO users (email, password_hash, role) VALUES ('x@example.com', 'h', 'technician')")
        conn.execute("INSERT INTO conversations (user_id) VALUES (1)")
    _expect_rejected("INSERT INTO messages (conversation_id, role, content) VALUES (1, 'system', 'x')")
    _expect_rejected(
        "INSERT INTO messages (conversation_id, role, content, answer_status) VALUES (1, 'assistant', 'x', 'done')"
    )


def test_unknown_document_review_status_is_rejected(test_env):
    _expect_rejected(
        "INSERT INTO documents (original_filename, storage_path, source_system, file_type, sha256, byte_size, "
        "status, review_status) VALUES ('a', 'a', 'google_drive', 'pdf', 'h', 1, 'indexed', 'maybe')"
    )

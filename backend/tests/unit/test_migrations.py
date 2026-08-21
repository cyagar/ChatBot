"""P1-9 (independent follow-up review): run_migrations used conn.executescript(),
which issues an implicit COMMIT before running the script. A migration that
failed part-way through therefore left its earlier statements permanently
applied with no schema_migrations row to explain them -- and the next start
would retry from a schema that no longer matched what the migration expected.

These tests prove each migration is now all-or-nothing and retries cleanly.
"""
from __future__ import annotations

import sqlite3

import pytest

from app import db as db_module
from app.db import get_conn, run_migrations, split_sql_statements


def _table_exists(conn, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def test_split_respects_semicolons_inside_string_literals():
    """Migration 0003's grandfathering note contains a semicolon inside a
    string literal -- a naive split(';') tears that statement in half."""
    script = (
        "CREATE TABLE t (a TEXT);\n"
        "UPDATE t SET a = 'first clause; second clause' WHERE a IS NULL;\n"
    )
    statements = split_sql_statements(script)
    assert len(statements) == 2
    assert "first clause; second clause" in statements[1]


def test_real_migration_files_all_split_into_valid_statements():
    for path in sorted(db_module.MIGRATIONS_DIR.glob("*.sql")):
        statements = split_sql_statements(path.read_text(encoding="utf-8"))
        assert statements, f"{path.name} produced no statements"
        for stmt in statements:
            assert sqlite3.complete_statement(stmt), f"incomplete statement in {path.name}: {stmt[:80]}"


def test_failing_migration_rolls_back_completely_and_leaves_no_record(test_env, tmp_path, monkeypatch):
    """Inject a migration whose second statement fails. The first statement's
    table must NOT survive, and no schema_migrations row may be written."""
    mig_dir = tmp_path / "migrations_broken"
    mig_dir.mkdir()
    (mig_dir / "9001_broken.sql").write_text(
        "CREATE TABLE p1_9_first (id INTEGER PRIMARY KEY);\n"
        "CREATE TABLE p1_9_second (id INTEGER PRIMARY KEY REFERENCES nonexistent_table(id));\n"
        "INSERT INTO definitely_not_a_table (x) VALUES (1);\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(db_module, "MIGRATIONS_DIR", mig_dir)

    with pytest.raises(sqlite3.Error):
        run_migrations()

    with get_conn() as conn:
        assert not _table_exists(conn, "p1_9_first"), "partial schema survived a failed migration"
        assert not _table_exists(conn, "p1_9_second")
        recorded = conn.execute(
            "SELECT 1 FROM schema_migrations WHERE version = '9001_broken'"
        ).fetchone()
    assert recorded is None, "a failed migration must not be recorded as applied"


def test_migration_applies_cleanly_on_retry_after_being_fixed(test_env, tmp_path, monkeypatch):
    """The whole point of rolling back: after the bad statement is corrected,
    re-running must succeed from the original schema rather than tripping over
    leftovers from the failed attempt."""
    mig_dir = tmp_path / "migrations_retry"
    mig_dir.mkdir()
    path = mig_dir / "9002_retry.sql"
    path.write_text(
        "CREATE TABLE p1_9_retry (id INTEGER PRIMARY KEY);\n"
        "INSERT INTO definitely_not_a_table (x) VALUES (1);\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(db_module, "MIGRATIONS_DIR", mig_dir)

    with pytest.raises(sqlite3.Error):
        run_migrations()

    # Fix the migration and retry -- the CREATE TABLE must not collide with a
    # leftover table from the failed run.
    path.write_text(
        "CREATE TABLE p1_9_retry (id INTEGER PRIMARY KEY);\n"
        "INSERT INTO p1_9_retry (id) VALUES (1);\n",
        encoding="utf-8",
    )
    applied = run_migrations()
    assert "9002_retry" in applied

    with get_conn() as conn:
        assert _table_exists(conn, "p1_9_retry")
        assert conn.execute("SELECT COUNT(*) c FROM p1_9_retry").fetchone()["c"] == 1


def test_rerunning_migrations_is_idempotent(test_env):
    """test_env already ran migrations; a second call must apply nothing."""
    assert run_migrations() == []

"""P1-9 (independent follow-up review): run_migrations used conn.executescript(),
which issues an implicit COMMIT before running the script. A migration that
failed part-way through therefore left its earlier statements permanently
applied with no schema_migrations row to explain them -- and the next start
would retry from a schema that no longer matched what the migration expected.

These tests prove each migration is now all-or-nothing and retries cleanly.
"""
from __future__ import annotations

import shutil
import sqlite3

import pytest

from app import db as db_module
from app.db import get_conn, run_migrations, split_sql_statements

_REAL_MIGRATION_PATHS = sorted(db_module.MIGRATIONS_DIR.glob("*.sql"))


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


def _fresh_unmigrated_db(tmp_path, monkeypatch):
    """Unlike test_env, does NOT call run_migrations() first -- the sweep
    below needs a DB that has never seen ANY migration, so the real version
    names (e.g. '0001_init') aren't already in schema_migrations. If they
    were, run_migrations() would just skip them as already-applied and every
    case below would pass without ever exercising the rollback path."""
    db_dir = tmp_path / "db"
    db_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("DB_PATH", str(db_dir / "sweep.db"))
    monkeypatch.setenv("SECRET_KEY", "test-secret-key")
    from app.config import get_settings
    get_settings.cache_clear()


@pytest.mark.parametrize("target_index", range(len(_REAL_MIGRATION_PATHS)))
def test_every_real_migration_rolls_back_and_retries_cleanly_on_failure(target_index, tmp_path, monkeypatch):
    """P1-9's own wording: 'retry successfully after every simulated
    statement-boundary failure.' The tests above prove the rollback
    MECHANISM works in principle using a synthetic migration; this sweeps
    every REAL migration file to prove none of them contains a statement
    that defeats it -- e.g. a PRAGMA that turns out not to be transactional.
    (0001_init.sql contains `PRAGMA foreign_keys = ON;`, which SQLite's own
    docs say is a no-op inside a transaction; harmless here only because
    _connect() already sets it outside any migration's transaction, but
    that's exactly the kind of thing worth a real assertion rather than a
    docstring's say-so.)

    For each real migration, every migration BEFORE it is applied for real
    (so it sees the schema it actually expects), its OWN last statement is
    replaced with a guaranteed failure, and the whole thing must roll back
    with no schema_migrations row -- then swapping in the real, unmodified
    file must apply cleanly. That last step is what actually catches a
    partial application that survived rollback: the real migration's own
    CREATE TABLE/ADD COLUMN would collide with any leftover object from the
    failed attempt."""
    _fresh_unmigrated_db(tmp_path, monkeypatch)

    target_path = _REAL_MIGRATION_PATHS[target_index]
    target_stem = target_path.stem
    real_statements = split_sql_statements(target_path.read_text(encoding="utf-8"))

    mig_dir = tmp_path / "migrations_sweep"
    mig_dir.mkdir()
    for path in _REAL_MIGRATION_PATHS[:target_index]:
        shutil.copy(path, mig_dir / path.name)

    broken_sql = "\n".join(real_statements[:-1]) + (
        "\nINSERT INTO definitely_not_a_table__p1_9_sweep (x) VALUES (1);\n"
    )
    (mig_dir / target_path.name).write_text(broken_sql, encoding="utf-8")

    monkeypatch.setattr(db_module, "MIGRATIONS_DIR", mig_dir)

    with pytest.raises(sqlite3.Error):
        db_module.run_migrations()

    with get_conn() as conn:
        recorded = conn.execute(
            "SELECT 1 FROM schema_migrations WHERE version = ?", (target_stem,)
        ).fetchone()
    assert recorded is None, f"{target_stem} must not be recorded as applied after failing part-way through"

    shutil.copy(target_path, mig_dir / target_path.name)
    applied = db_module.run_migrations()
    assert target_stem in applied, f"{target_stem} did not apply cleanly on retry after its own rollback"

"""P1-9 (independent follow-up review): run_migrations used conn.executescript(),
which issues an implicit COMMIT before running the script. A migration that
failed part-way through therefore left its earlier statements permanently
applied with no schema_migrations row to explain them -- and the next start
would retry from a schema that no longer matched what the migration expected.

These tests prove each migration is now all-or-nothing and retries cleanly.

Ported from SQLite to Postgres (2026-09-14): split_sql_statements() used to be
a real statement-aware parser (sqlite3.complete_statement-based) that
respected semicolons inside string literals/comments. It's now a naive
split(';') -- see split_sql_statements's own docstring in app/db.py -- so
every migration author's real, accepted constraint is: never write a
semicolon anywhere except as a genuine statement terminator, not even inside
a string literal or a comment. test_split_does_not_respect_semicolons_inside_
string_literals below pins that this really is naive, deliberately, so
nobody "fixes" it into doing something smarter without updating this file
and confirming no real migration relies on the naive behavior.
"""
from __future__ import annotations

import shutil

import psycopg
import pytest
from dotenv import dotenv_values

from app import db as db_module
from app.db import get_conn, run_migrations, split_sql_statements
from tests.conftest import TEST_ENV_FILE

_REAL_MIGRATION_PATHS = sorted(db_module.MIGRATIONS_DIR.glob("*.sql"))


def _table_exists(conn, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_schema = 'public' AND table_name = %s", (name,)
    ).fetchone() is not None


def test_split_does_not_respect_semicolons_inside_string_literals():
    """The naive split(';') this app now uses does NOT special-case string
    literals or comments the way the old sqlite3.complete_statement-based
    splitter did -- a semicolon anywhere ends a statement, even mid-literal.
    This is a real, accepted constraint on every migration author now (see
    0001_initial_schema.sql's own two comment edits made to satisfy it)."""
    script = (
        "CREATE TABLE t (a TEXT);\n"
        "UPDATE t SET a = 'first clause; second clause' WHERE a IS NULL;\n"
    )
    statements = split_sql_statements(script)
    assert len(statements) == 3, "a semicolon inside the string literal splits the statement in two"


def test_real_migration_files_never_contain_a_semicolon_that_would_split_a_statement_mid_way():
    """Direct regression guard for the naive-splitter constraint above: a
    semicolon that tore a string literal in half would leave an odd number
    of single quotes in one of the resulting fragments (half the literal's
    opening/closing quote pair torn off into the next fragment)."""
    for path in sorted(db_module.MIGRATIONS_DIR.glob("*.sql")):
        statements = split_sql_statements(path.read_text(encoding="utf-8"))
        assert statements, f"{path.name} produced no statements"
        for stmt in statements:
            assert stmt.count("'") % 2 == 0, (
                f"a semicolon likely split a string literal mid-statement in {path.name}: {stmt[:80]}"
            )


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

    with pytest.raises(psycopg.Error):
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
    leftovers from the failed attempt.

    Unlike the old per-test SQLite file, the Postgres test branch's `public`
    schema persists across test runs (tests/conftest.py's test_env only
    TRUNCATEs the real migration's own tables, not ad hoc ones a test creates
    directly) -- so this test must clean up its own '9002_retry'
    schema_migrations row and p1_9_retry table itself, or the next run would
    find '9002_retry' already recorded as applied, skip re-running the
    (rewritten, still-broken-first) migration file entirely, and
    `pytest.raises(psycopg.Error)` below would fail with "DID NOT RAISE"."""
    mig_dir = tmp_path / "migrations_retry"
    mig_dir.mkdir()
    path = mig_dir / "9002_retry.sql"
    path.write_text(
        "CREATE TABLE p1_9_retry (id INTEGER PRIMARY KEY);\n"
        "INSERT INTO definitely_not_a_table (x) VALUES (1);\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(db_module, "MIGRATIONS_DIR", mig_dir)

    try:
        with pytest.raises(psycopg.Error):
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
    finally:
        with get_conn() as conn:
            conn.execute("DROP TABLE IF EXISTS p1_9_retry")
            conn.execute("DELETE FROM schema_migrations WHERE version = '9002_retry'")


def test_rerunning_migrations_is_idempotent(test_env):
    """test_env already ran migrations; a second call must apply nothing."""
    assert run_migrations() == []


@pytest.fixture
def fresh_unmigrated_db(monkeypatch):
    """Unlike test_env, does NOT apply the real schema first -- the sweep
    below needs a database that has never seen ANY migration, so a real
    version name (e.g. '0001_initial_schema') isn't already in
    schema_migrations and its CREATE TABLEs don't collide with
    already-existing tables. If they were, run_migrations() would just skip
    it as already-applied (or fail on "relation already exists" for an
    unrelated reason) and the sweep below would never exercise the rollback
    path it exists to prove.

    Rather than a whole separate Neon branch, this sandboxes a disposable
    Postgres SCHEMA (namespace) on the same "test" branch tests/conftest.py
    already uses: search_path is pointed at it via the connection URL's
    `options` param, so every unqualified table/reference name in a
    migration resolves inside the empty schema instead of the real `public`
    one. Explicitly loads backend/.env.test itself (not the test_env
    fixture) -- test_env's own TRUNCATE/run_migrations setup targets
    `public` and would be pointless work here; this fixture never touches
    `public` at all."""
    if not TEST_ENV_FILE.is_file():
        raise RuntimeError(
            f"{TEST_ENV_FILE} is missing. Tests must not fall back to "
            "backend/.env's production DATABASE_URL -- see tests/conftest.py's "
            "test_env fixture for how to set up the dedicated Neon test branch."
        )
    base_unpooled = dotenv_values(TEST_ENV_FILE)["DATABASE_URL_UNPOOLED"]
    schema = "migration_sweep_test"
    sep = "&" if "?" in base_unpooled else "?"
    scoped_dsn = f"{base_unpooled}{sep}options=-c%20search_path%3D{schema}"

    setup_conn = psycopg.connect(base_unpooled, autocommit=True)
    try:
        setup_conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        setup_conn.execute(f"CREATE SCHEMA {schema}")
    finally:
        setup_conn.close()

    monkeypatch.setenv("DATABASE_URL_UNPOOLED", scoped_dsn)
    monkeypatch.setenv("DATABASE_URL", scoped_dsn)
    monkeypatch.setenv("SECRET_KEY", "test-secret-key")
    from app.config import get_settings
    get_settings.cache_clear()

    try:
        yield
    finally:
        get_settings.cache_clear()
        cleanup_conn = psycopg.connect(base_unpooled, autocommit=True)
        try:
            cleanup_conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        finally:
            cleanup_conn.close()


@pytest.mark.parametrize("target_index", range(len(_REAL_MIGRATION_PATHS)))
def test_every_real_migration_rolls_back_and_retries_cleanly_on_failure(
    target_index, tmp_path, monkeypatch, fresh_unmigrated_db
):
    """P1-9's own wording: 'retry successfully after every simulated
    statement-boundary failure.' The tests above prove the rollback
    MECHANISM works in principle using a synthetic migration; this sweeps
    every REAL migration file to prove none of them contains a statement
    that defeats it.

    For each real migration, every migration BEFORE it is applied for real
    (so it sees the schema it actually expects), its OWN last statement is
    replaced with a guaranteed failure, and the whole thing must roll back
    with no schema_migrations row -- then swapping in the real, unmodified
    file must apply cleanly. That last step is what actually catches a
    partial application that survived rollback: the real migration's own
    CREATE TABLE/ADD COLUMN would collide with any leftover object from the
    failed attempt."""
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

    with pytest.raises(psycopg.Error):
        db_module.run_migrations()

    with get_conn() as conn:
        recorded = conn.execute(
            "SELECT 1 FROM schema_migrations WHERE version = %s", (target_stem,)
        ).fetchone()
    assert recorded is None, f"{target_stem} must not be recorded as applied after failing part-way through"

    shutil.copy(target_path, mig_dir / target_path.name)
    applied = db_module.run_migrations()
    assert target_stem in applied, f"{target_stem} did not apply cleanly on retry after its own rollback"

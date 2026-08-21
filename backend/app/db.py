import sqlite3
from contextlib import contextmanager
from pathlib import Path

from app.config import get_settings

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


def _connect() -> sqlite3.Connection:
    settings = get_settings()
    settings.db_path_resolved.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(settings.db_path_resolved, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


@contextmanager
def get_conn():
    conn = _connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def split_sql_statements(script: str) -> list[str]:
    """Split a migration script into individual statements.

    Uses sqlite3.complete_statement rather than a naive split on ';' because
    a semicolon can legitimately appear inside a string literal -- migration
    0003's grandfathering review_note contains one, and a naive split would
    tear that INSERT in half (independent follow-up review P1-9)."""
    statements: list[str] = []
    buf = ""
    for line in script.splitlines(keepends=True):
        buf += line
        if sqlite3.complete_statement(buf):
            stmt = buf.strip()
            if stmt:
                statements.append(stmt)
            buf = ""
    tail = buf.strip()
    if tail:
        statements.append(tail)
    return statements


def run_migrations() -> list[str]:
    """Apply any .sql files in migrations/ not yet recorded in schema_migrations.
    Safe to call repeatedly (idempotent).

    Each migration runs inside one explicit transaction together with its own
    schema_migrations INSERT, so a failure part-way through rolls the whole
    migration back and leaves no record -- the next start retries it cleanly
    from the original schema. This deliberately does NOT use
    conn.executescript(), which issues an implicit COMMIT before running and
    would therefore leave partially-applied schema changes behind with no
    migration row to explain them (independent follow-up review P1-9).
    SQLite DDL is transactional, which is what makes the rollback complete;
    don't "simplify" this back to executescript()."""
    applied = []
    conn = _connect()
    # Explicit transaction control: with the default isolation_level, sqlite3
    # decides on its own when to BEGIN, which is exactly the ambiguity this
    # function must not have.
    conn.isolation_level = None
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "version TEXT PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
        )
        already = {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            version = path.stem
            if version in already:
                continue
            statements = split_sql_statements(path.read_text(encoding="utf-8"))
            conn.execute("BEGIN")
            try:
                for stmt in statements:
                    conn.execute(stmt)
                conn.execute("INSERT INTO schema_migrations (version) VALUES (?)", (version,))
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
            applied.append(version)
    finally:
        conn.close()
    return applied

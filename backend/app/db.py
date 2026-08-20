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


def run_migrations() -> list[str]:
    """Apply any .sql files in migrations/ not yet recorded in schema_migrations.
    Safe to call repeatedly (idempotent)."""
    applied = []
    conn = _connect()
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
            script = path.read_text(encoding="utf-8")
            conn.executescript(script)
            conn.execute(
                "INSERT INTO schema_migrations (version) VALUES (?)", (version,)
            )
            applied.append(version)
        conn.commit()
    finally:
        conn.close()
    return applied

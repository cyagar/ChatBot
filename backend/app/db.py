from contextlib import contextmanager
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from app.config import get_settings

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


def _connect() -> psycopg.Connection:
    settings = get_settings()
    # Pooled endpoint (PgBouncer, transaction mode): correct choice for
    # request-scoped, connection-per-call app traffic. Never use this
    # connection for anything needing session state (SET, LISTEN/NOTIFY,
    # multi-statement transactions spanning route boundaries) -- see
    # run_migrations() below, which deliberately uses the direct URL instead.
    return psycopg.connect(settings.database_url, row_factory=dict_row)


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
    """Split a migration script into individual statements on ';' boundaries.

    Safe because these migration files are authored by us and never contain a
    semicolon inside a string literal or comment -- unlike the old SQLite
    migrations (one of which grandfathered seed data with an embedded
    semicolon), starting the Postgres database empty means no migration here
    ever carries row data, only schema."""
    return [stmt.strip() + ";" for stmt in script.split(";") if stmt.strip()]


def run_migrations() -> list[str]:
    """Apply any .sql files in migrations/ not yet recorded in schema_migrations.
    Safe to call repeatedly (idempotent).

    Uses the direct (unpooled) connection, not the pooled app connection --
    Postgres DDL needs real session-level transaction control, which a
    PgBouncer transaction-mode pooler doesn't support (see the neon-postgres
    skill's pooled-vs-direct guidance). Each migration runs inside its own
    transaction together with its own schema_migrations INSERT, so a failure
    part-way through rolls the whole migration back and leaves no record --
    the next start retries it cleanly from the original schema."""
    settings = get_settings()
    applied = []
    conn = psycopg.connect(settings.database_url_unpooled, row_factory=dict_row, autocommit=True)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "version TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
        already = {r["version"] for r in conn.execute("SELECT version FROM schema_migrations").fetchall()}
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            version = path.stem
            if version in already:
                continue
            statements = split_sql_statements(path.read_text(encoding="utf-8"))
            with conn.transaction():
                for stmt in statements:
                    conn.execute(stmt)
                conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (version,))
            applied.append(version)
    finally:
        conn.close()
    return applied

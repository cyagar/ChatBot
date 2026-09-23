import re
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


_DOLLAR_TAG_RE = re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*\$|\$\$")


def split_sql_statements(script: str) -> list[str]:
    """SQL-aware statement splitter: tracks single-quoted string literals
    ('' escaping), double-quoted identifiers, -- line comments, /* */ block
    comments, and $$.../$tag$...$tag$ dollar-quoted bodies, splitting only
    on a ';' outside all of them.

    run_migrations() below does not use this for real execution -- it sends
    each migration file to Postgres as a single multi-statement script
    instead, which lets Postgres's own parser (not a hand-rolled one) handle
    every one of these cases correctly, including a PL/pgSQL function/trigger
    body (routine semicolons inside $$...$$) that a naive split(';') would
    silently mangle. This function exists purely as a TEST helper (see
    test_migrations.py's rollback sweep, which needs "this migration's
    statements minus its last one" to construct a deliberately-broken
    migration) -- production correctness does not depend on it."""
    statements: list[str] = []
    buf: list[str] = []
    i, n = 0, len(script)
    in_single = in_double = in_line_comment = in_block_comment = False
    dollar_tag: str | None = None

    while i < n:
        ch = script[i]

        if in_line_comment:
            buf.append(ch)
            in_line_comment = ch != "\n"
            i += 1
            continue
        if in_block_comment:
            if ch == "*" and script[i + 1 : i + 2] == "/":
                buf.append("*/")
                i += 2
                in_block_comment = False
            else:
                buf.append(ch)
                i += 1
            continue
        if dollar_tag is not None:
            if script.startswith(dollar_tag, i):
                buf.append(dollar_tag)
                i += len(dollar_tag)
                dollar_tag = None
            else:
                buf.append(ch)
                i += 1
            continue
        if in_single:
            if ch == "'" and script[i + 1 : i + 2] == "'":
                buf.append("''")
                i += 2
            elif ch == "'":
                buf.append(ch)
                i += 1
                in_single = False
            else:
                buf.append(ch)
                i += 1
            continue
        if in_double:
            buf.append(ch)
            if ch == '"':
                in_double = False
            i += 1
            continue

        # Not inside any quoted/comment region -- check for one starting here.
        if ch == "-" and script[i + 1 : i + 2] == "-":
            in_line_comment = True
            buf.append(ch)
            i += 1
            continue
        if ch == "/" and script[i + 1 : i + 2] == "*":
            in_block_comment = True
            buf.append(ch)
            i += 1
            continue
        if ch == "'":
            in_single = True
            buf.append(ch)
            i += 1
            continue
        if ch == '"':
            in_double = True
            buf.append(ch)
            i += 1
            continue
        if ch == "$":
            m = _DOLLAR_TAG_RE.match(script, i)
            if m:
                dollar_tag = m.group(0)
                buf.append(dollar_tag)
                i += len(dollar_tag)
                continue
        if ch == ";":
            buf.append(ch)
            stmt = "".join(buf).strip()
            if stmt:
                statements.append(stmt)
            buf = []
            i += 1
            continue

        buf.append(ch)
        i += 1

    tail = "".join(buf).strip()
    if tail:
        statements.append(tail)
    return statements


def run_migrations() -> list[str]:
    """Apply any .sql files in migrations/ not yet recorded in schema_migrations.
    Safe to call repeatedly (idempotent).

    Uses the direct (unpooled) connection, not the pooled app connection --
    Postgres DDL needs real session-level transaction control, which a
    PgBouncer transaction-mode pooler doesn't support (see the neon-postgres
    skill's pooled-vs-direct guidance). Each migration runs inside its own
    transaction together with its own schema_migrations INSERT, so a failure
    part-way through rolls the whole migration back and leaves no record --
    the next start retries it cleanly from the original schema.

    Each migration file is sent to Postgres as ONE multi-statement script via
    a single parameterless execute() call -- psycopg3 falls back to libpq's
    simple query protocol for a parameterless execute(), the same protocol
    psql itself uses, which supports a full multi-statement script, correctly
    stops at the first failing statement, and still rolls back atomically
    inside `with conn.transaction():`, with a semicolon inside a string
    literal surviving intact. This keeps statement-splitting entirely out of
    the trusted-execution path rather than relying on a hand-rolled splitter
    to be perfect: a migration author who puts a semicolon inside a string
    literal or comment does not need to think about it."""
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
            with conn.transaction():
                conn.execute(path.read_text(encoding="utf-8"))
                conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (version,))
            applied.append(version)
    finally:
        conn.close()
    return applied

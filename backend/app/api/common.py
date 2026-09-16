"""Shared response-shaping helpers used across app/api/*.py."""

from __future__ import annotations

from datetime import datetime, timezone


def iso_utc(ts: datetime | str | None) -> str | None:
    """Normalizes a stored timestamp to ISO-8601 with an explicit UTC
    offset (Phase 1: "Return UTC ISO-8601 timestamps with offsets").

    Postgres TIMESTAMPTZ columns (every timestamp column in the schema) come
    back from psycopg as real, already-tz-aware `datetime` objects, not
    strings -- those pass through the isoformat() call below unchanged. The
    `str` branch exists for any caller that still hands this a raw string
    (e.g. a value built in Python via `.isoformat()` before being persisted,
    as `invitations.expires_at`'s computation used to do): if it has no
    offset, UTC is assumed, matching this app's SQLite-era columns, which
    had no timezone marker at all but were always UTC.
    """
    if ts is None:
        return None
    dt = ts if isinstance(ts, datetime) else datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()

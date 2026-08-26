"""Shared response-shaping helpers used across app/api/*.py."""

from __future__ import annotations

from datetime import datetime, timezone


def iso_utc(ts: str | None) -> str | None:
    """Normalizes a stored timestamp to ISO-8601 with an explicit UTC
    offset (Phase 1: "Return UTC ISO-8601 timestamps with offsets").

    Most timestamps in this app come from SQLite's `datetime('now')`, which
    returns `'YYYY-MM-DD HH:MM:SS'` with no timezone marker at all (SQLite
    has no separate timestamp type) -- SQLite's clock is UTC, but a client
    has no way to know that from the bare string. A few columns
    (`invitations.expires_at`, computed via Python's
    `datetime.now(timezone.utc).isoformat()`) are already full ISO strings
    with an offset; those pass through unchanged rather than being
    double-converted or having a real offset silently overwritten.
    """
    if ts is None:
        return None
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()

"""Phase 1 (narrowed scope, 2026-08-26): "Return UTC ISO-8601 timestamps
with offsets." Most timestamps in this app come from SQLite's
datetime('now'), which returns 'YYYY-MM-DD HH:MM:SS' with no timezone
marker -- not valid ISO-8601, and ambiguous to any client that doesn't
already know SQLite's clock is UTC. app.api.common.iso_utc() normalizes
this; these tests pin its exact behavior, including the two cases that are
easy to get subtly wrong: a bare SQLite string must gain a UTC offset, but
a column already written with a real offset (invitations.expires_at, via
Python's own datetime.now(timezone.utc).isoformat()) must pass through
unchanged rather than being reinterpreted or double-converted.
"""
from __future__ import annotations

from app.api.common import iso_utc


def test_bare_sqlite_timestamp_gains_a_utc_offset():
    assert iso_utc("2026-08-26 14:30:00") == "2026-08-26T14:30:00+00:00"


def test_already_offset_timestamp_passes_through_unchanged():
    assert iso_utc("2026-08-27T09:15:00+00:00") == "2026-08-27T09:15:00+00:00"


def test_non_utc_offset_is_preserved_not_overwritten():
    # Not a value this app actually writes today, but the function must not
    # silently claim a non-UTC-tagged timestamp is UTC.
    assert iso_utc("2026-08-27T09:15:00+05:00") == "2026-08-27T09:15:00+05:00"


def test_none_stays_none():
    assert iso_utc(None) is None


def test_fractional_seconds_are_preserved():
    assert iso_utc("2026-08-26 14:30:00.123456") == "2026-08-26T14:30:00.123456+00:00"

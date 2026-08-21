"""Minimal audit trail for the security-relevant actions named in the
independent follow-up review's P0-5/P0-6 required fixes -- not a
general-purpose event log."""

from __future__ import annotations


def log_audit_event(
    conn,
    event_type: str,
    actor_user_id: int | None = None,
    target_type: str | None = None,
    target_id: int | None = None,
    detail: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO audit_events (actor_user_id, event_type, target_type, target_id, detail) "
        "VALUES (?, ?, ?, ?, ?)",
        (actor_user_id, event_type, target_type, target_id, detail),
    )

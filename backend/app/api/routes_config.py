"""GET /config: maintenance state, feature flags, minimum supported version,
support link, and safe status text. Deliberately public (no auth) -- a
client needs this before it can know whether logging in is even worth
trying (maintenance mode) and what version it needs to be.

Role/capabilities live on GET /api/auth/me instead, alongside the rest of
the caller's own identity -- see app/auth/routes.py.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter
from pydantic import BaseModel

from app.config import get_settings
from app.db import get_conn

router = APIRouter(prefix="/api", tags=["config"])


class ConfigOut(BaseModel):
    maintenance_mode: bool
    maintenance_message: str
    feature_flags: dict[str, bool]
    minimum_supported_version: str
    support_contact: str
    status: str  # "ok" | "degraded"
    status_message: str


def _corpus_status(settings) -> tuple[str, str]:
    """Best-effort only -- mirrors the staleness check
    GET /api/admin/ingestion/status already runs (kept separate rather than
    factored out, since that endpoint's tested behavior shouldn't have to
    change for this public, coarser-grained summary). Never raises: a
    config endpoint that itself fails because the corpus-freshness query
    had a problem would be a worse outcome than just reporting "ok".
    """
    if not settings.google_drive_folder_id:
        return "ok", ""
    try:
        with get_conn() as conn:
            last_success = conn.execute(
                "SELECT finished_at FROM ingestion_runs "
                "WHERE status IN ('completed', 'completed_with_errors') "
                "ORDER BY finished_at DESC LIMIT 1"
            ).fetchone()
        if last_success is None:
            return "ok", ""
        # finished_at is a TIMESTAMPTZ column -- psycopg hands it back as a
        # real, aware datetime, not a string. Calling
        # datetime.fromisoformat() on it would raise TypeError every time,
        # which the blanket `except Exception` below would silently turn
        # into ("ok", "") -- so a corpus that hadn't synced in days, or
        # ever, would always report healthy.
        finished = last_success["finished_at"]
        if finished.tzinfo is None:
            finished = finished.replace(tzinfo=timezone.utc)
        hours_since = (datetime.now(timezone.utc) - finished).total_seconds() / 3600
        if hours_since > settings.ingestion_staleness_threshold_hours:
            return "degraded", "The manual corpus has not synced recently and may be out of date."
        return "ok", ""
    except Exception:
        return "ok", ""


@router.get("/config", response_model=ConfigOut)
def get_config() -> ConfigOut:
    settings = get_settings()
    status_, status_message = (
        ("degraded", settings.maintenance_message or "This app is temporarily in maintenance mode.")
        if settings.maintenance_mode
        else _corpus_status(settings)
    )
    return ConfigOut(
        maintenance_mode=settings.maintenance_mode,
        maintenance_message=settings.maintenance_message,
        feature_flags={},
        minimum_supported_version=settings.minimum_supported_version,
        support_contact=settings.support_contact,
        status=status_,
        status_message=status_message,
    )

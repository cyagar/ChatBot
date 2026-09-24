"""Interval-based automated corpus sync. It runs the same ingest_all() path as
a manual "Run re-index now", including per-file isolation, skip-if-unchanged
and the ingestion locks, so a timer tick and a manual run cannot race. The
timer lives in the web process, so it stops when the process does; the first
sync after a start is scheduled from the last successful run."""

from __future__ import annotations

import asyncio
import logging

from app.config import get_settings
from app.db import get_conn
from app.ingestion.pipeline import ingest_all

logger = logging.getLogger(__name__)


def is_enabled(settings=None) -> bool:
    """Whether the automated sync loop should run at all. Shared by main.py
    (decides whether to start the background task) and the
    /api/admin/ingestion/status endpoint (reports the same answer to the
    admin UI) so the two can never silently disagree."""
    settings = settings or get_settings()
    return bool(settings.google_drive_folder_id) and settings.ingestion_sync_interval_minutes > 0


FIRST_SYNC_MIN_DELAY_SECONDS = 60


def _seconds_until_next_sync(interval_minutes: int) -> float:
    """Delay before the first sync after a start. A deployment whose corpus is
    already stale (or has never synced) syncs shortly after start instead of
    waiting a full interval; a fresh corpus waits out the rest of its interval."""
    try:
        with get_conn() as conn:
            row = conn.execute(
                "SELECT EXTRACT(EPOCH FROM (now() - max(finished_at))) AS age FROM ingestion_runs "
                "WHERE status IN ('completed', 'completed_with_errors')"
            ).fetchone()
    except Exception:
        return interval_minutes * 60
    age = row["age"] if row else None
    if age is None:
        return FIRST_SYNC_MIN_DELAY_SECONDS
    return max(FIRST_SYNC_MIN_DELAY_SECONDS, interval_minutes * 60 - float(age))


async def run_scheduled_sync_loop() -> None:
    """Waits (see _seconds_until_next_sync for the first wait), syncs, then
    repeats every `ingestion_sync_interval_minutes` until cancelled. Started
    as an asyncio task from the app lifespan; a no-op when the interval is <= 0."""
    settings = get_settings()
    interval_minutes = settings.ingestion_sync_interval_minutes
    if interval_minutes <= 0:
        logger.info("Scheduled Drive sync is disabled (INGESTION_SYNC_INTERVAL_MINUTES<=0).")
        return

    logger.info("Scheduled Drive sync started: every %d minutes.", interval_minutes)
    first = True
    while True:
        await asyncio.sleep(await asyncio.to_thread(_seconds_until_next_sync, interval_minutes) if first
                            else interval_minutes * 60)
        first = False
        try:
            logger.info("Scheduled Drive sync starting.")
            report = await asyncio.to_thread(ingest_all, None, True, "scheduled")
            logger.info("Scheduled Drive sync finished: %s", report.counts())
        except asyncio.CancelledError:
            raise
        except RuntimeError as e:
            # _INGEST_LOCK already held by a concurrent manual run -- skip this
            # tick rather than erroring the loop; the next tick retries.
            logger.info("Scheduled Drive sync skipped: %s", e)
        except Exception:
            logger.exception("Scheduled Drive sync failed unexpectedly.")

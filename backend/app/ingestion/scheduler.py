"""Time-based automated corpus sync (independent follow-up review P1-4:
"corpus freshness depends on an admin remembering to reindex... add a safe
scheduled/push-triggered sync... manual triggering can remain as an
override, not the only freshness mechanism").

A push-triggered sync (a Google Drive `changes.watch` webhook) needs a
publicly reachable HTTPS endpoint and channel-renewal bookkeeping (watch
channels expire and must be re-registered) -- infrastructure this
single-instance pilot deployment doesn't have. A plain interval timer is the
safe, proportionate mechanism at this scale: it reuses the exact same
ingest_all() path a manual "Run re-index now" click already uses, including
its existing per-file isolation, idempotent skip-if-unchanged logic, and the
_INGEST_LOCK that already guards against two runs racing."""

from __future__ import annotations

import asyncio
import logging

from app.config import get_settings
from app.ingestion.pipeline import ingest_all

logger = logging.getLogger(__name__)


def is_enabled(settings=None) -> bool:
    """Whether the automated sync loop should run at all. Shared by main.py
    (decides whether to start the background task) and the
    /api/admin/ingestion/status endpoint (reports the same answer to the
    admin UI) so the two can never silently disagree."""
    settings = settings or get_settings()
    return bool(settings.google_drive_folder_id) and settings.ingestion_sync_interval_minutes > 0


async def run_scheduled_sync_loop() -> None:
    """Sleeps a full `ingestion_sync_interval_minutes` first, then syncs, then
    repeats, until cancelled. Deliberately does NOT sync immediately on
    startup: this loop starts on every container start, and syncing first
    would turn every restart -- including a crash-loop -- into an immediate
    full Drive listing/download against the live corpus. A sleep-first loop
    costs at most one interval of extra staleness after a fresh deploy;
    manual "Run re-index now" remains available as the immediate override the
    review asked for. Intended to be started as an asyncio task from the
    app's lifespan and cancelled on shutdown; a caller checking
    `settings.ingestion_sync_interval_minutes <= 0` before starting this can
    skip it entirely, but this also degrades to a no-op on its own if called
    anyway."""
    settings = get_settings()
    interval_minutes = settings.ingestion_sync_interval_minutes
    if interval_minutes <= 0:
        logger.info("Scheduled Drive sync is disabled (INGESTION_SYNC_INTERVAL_MINUTES<=0).")
        return

    logger.info("Scheduled Drive sync started: every %d minutes.", interval_minutes)
    while True:
        await asyncio.sleep(interval_minutes * 60)
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

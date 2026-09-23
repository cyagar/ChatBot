"""Unit tests for the automated corpus sync loop
(app/ingestion/scheduler.py).

No real sleeping or Drive access: ingest_all() and asyncio.sleep() are both
monkeypatched, and every test cancels the loop after a bounded number of
ticks rather than waiting on a real interval.

The loop sleeps BEFORE its first sync, not after (a deliberate choice: it
starts on every container restart, and syncing immediately would turn every
restart into an unconditional live Drive listing/download -- see the
function's own docstring). Tests that need to observe one ingest_all() call
therefore let the first fake sleep() return normally and cancel on the
second one, rather than cancelling on the first."""

from __future__ import annotations

import asyncio

import pytest

from app.ingestion import scheduler


def test_disabled_interval_returns_immediately_without_calling_ingest_all(test_env, monkeypatch):
    monkeypatch.setenv("INGESTION_SYNC_INTERVAL_MINUTES", "0")
    from app.config import get_settings
    get_settings.cache_clear()

    calls = []
    monkeypatch.setattr(scheduler, "ingest_all", lambda *a, **k: calls.append((a, k)))

    asyncio.run(scheduler.run_scheduled_sync_loop())

    assert calls == []


def test_loop_sleeps_before_its_first_sync_then_calls_ingest_all(test_env, monkeypatch):
    monkeypatch.setenv("INGESTION_SYNC_INTERVAL_MINUTES", "5")
    from app.config import get_settings
    get_settings.cache_clear()

    calls = []

    def fake_ingest_all(source, embed, trigger):
        calls.append((source, embed, trigger))
        from app.ingestion.pipeline import IngestionReport
        return IngestionReport(run_id=1)

    sleep_calls = []

    async def fake_sleep(seconds):
        sleep_calls.append(seconds)
        if len(sleep_calls) >= 2:
            raise asyncio.CancelledError()  # stop after the second sleep (one full tick)

    monkeypatch.setattr(scheduler, "ingest_all", fake_ingest_all)
    monkeypatch.setattr(scheduler.asyncio, "sleep", fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(scheduler.run_scheduled_sync_loop())

    assert calls == [(None, True, "scheduled")], "must not sync before the first sleep completes"
    assert sleep_calls == [5 * 60, 5 * 60]


def test_loop_survives_a_concurrent_manual_run_holding_the_lock(test_env, monkeypatch):
    """If a manual re-index is already running when a scheduled tick fires,
    ingest_all() raises RuntimeError (the existing _INGEST_LOCK guard) -- the
    loop must log and continue to the next tick, not crash."""
    monkeypatch.setenv("INGESTION_SYNC_INTERVAL_MINUTES", "5")
    from app.config import get_settings
    get_settings.cache_clear()

    def raising_ingest_all(source, embed, trigger):
        raise RuntimeError("An ingestion run is already in progress.")

    sleep_calls = []

    async def fake_sleep(seconds):
        sleep_calls.append(seconds)
        if len(sleep_calls) >= 2:
            raise asyncio.CancelledError()

    monkeypatch.setattr(scheduler, "ingest_all", raising_ingest_all)
    monkeypatch.setattr(scheduler.asyncio, "sleep", fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(scheduler.run_scheduled_sync_loop())

    assert sleep_calls == [5 * 60, 5 * 60], "a locked-out tick must still proceed to sleep for the next one"


def test_loop_survives_an_unexpected_exception(test_env, monkeypatch):
    """Any other failure (e.g. a real Drive auth error) must not kill the
    background task permanently -- the next tick should still be attempted."""
    monkeypatch.setenv("INGESTION_SYNC_INTERVAL_MINUTES", "5")
    from app.config import get_settings
    get_settings.cache_clear()

    def exploding_ingest_all(source, embed, trigger):
        raise ValueError("simulated unexpected failure")

    sleep_calls = []

    async def fake_sleep(seconds):
        sleep_calls.append(seconds)
        if len(sleep_calls) >= 2:
            raise asyncio.CancelledError()

    monkeypatch.setattr(scheduler, "ingest_all", exploding_ingest_all)
    monkeypatch.setattr(scheduler.asyncio, "sleep", fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(scheduler.run_scheduled_sync_loop())

    assert sleep_calls == [5 * 60, 5 * 60]


def test_cancellation_during_a_sync_propagates_without_an_extra_sleep(test_env, monkeypatch):
    """A real shutdown (main.py cancelling the task) mid-sync must stop the
    loop immediately -- the loop must not swallow the cancellation and go
    back to sleep for another tick."""
    monkeypatch.setenv("INGESTION_SYNC_INTERVAL_MINUTES", "5")
    from app.config import get_settings
    get_settings.cache_clear()

    def cancelling_ingest_all(source, embed, trigger):
        raise asyncio.CancelledError()

    sleep_calls = []

    async def fake_sleep(seconds):
        sleep_calls.append(seconds)

    monkeypatch.setattr(scheduler, "ingest_all", cancelling_ingest_all)
    monkeypatch.setattr(scheduler.asyncio, "sleep", fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(scheduler.run_scheduled_sync_loop())

    assert sleep_calls == [5 * 60], (
        "exactly one sleep (before the sync that got cancelled) -- no second sleep afterward"
    )

"""P1-4: the automated sync loop must actually be gated correctly and wired
into the app's lifespan -- not just exist as an importable function nothing
calls.

is_enabled() is unit-tested directly (fast, no ASGI involved) for every
combination of "Drive configured" x "interval positive". A real end-to-end
wiring test then exercises the actual ASGI lifespan via
`with TestClient(app) as client:` (a bare `TestClient(app)` never triggers
startup/shutdown, which is why no other test file in this suite needs the
`with` form) to prove main.py actually starts and cleanly cancels the task,
not just that the boolean logic is correct in isolation.

Note: `validate_for_startup()` unconditionally requires GOOGLE_DRIVE_FOLDER_ID
to be set in every environment (not just production), so a Drive-unconfigured
app cannot boot via the real lifespan at all -- that combination is only
reachable as a pure function call on is_enabled(), never via a real running
app, and is tested that way below rather than by fighting the ASGI lifecycle
to reach an unreachable state."""

from __future__ import annotations

import asyncio
import json

from fastapi.testclient import TestClient

from app.ingestion import scheduler
from app.main import app


def test_is_enabled_requires_both_drive_configured_and_positive_interval(test_env, monkeypatch):
    from app.config import get_settings

    def check(folder_id: str, interval: str) -> bool:
        monkeypatch.setenv("GOOGLE_DRIVE_FOLDER_ID", folder_id)
        monkeypatch.setenv("INGESTION_SYNC_INTERVAL_MINUTES", interval)
        get_settings.cache_clear()
        try:
            return scheduler.is_enabled(get_settings())
        finally:
            get_settings.cache_clear()

    assert check("", "360") is False, "no folder configured -- must not enable"
    assert check("fake-folder", "0") is False, "interval disabled -- must not enable"
    assert check("", "0") is False
    assert check("fake-folder", "360") is True, "both conditions met -- must enable"


def test_scheduler_task_starts_and_stops_cleanly_when_enabled(test_env, monkeypatch, tmp_path):
    key_path = tmp_path / "fake-service-account.json"
    key_path.write_text(json.dumps({
        "type": "service_account", "project_id": "fake-project",
        "private_key": "fake-key", "client_email": "fake@fake-project.iam.gserviceaccount.com",
    }))
    monkeypatch.setenv("GOOGLE_DRIVE_FOLDER_ID", "fake-folder-id")
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON_PATH", str(key_path))
    monkeypatch.setenv("INGESTION_SYNC_INTERVAL_MINUTES", "5")
    from app.config import get_settings
    get_settings.cache_clear()

    started = []
    cancelled = []

    async def fake_loop():
        started.append(True)
        try:
            await asyncio.Event().wait()  # would run forever if not cancelled on shutdown
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    monkeypatch.setattr(scheduler, "run_scheduled_sync_loop", fake_loop)

    with TestClient(app):
        pass

    get_settings.cache_clear()
    assert started == [True], "the scheduler must start when Drive is configured and the interval is positive"
    assert cancelled == [True], "shutdown must cancel the background task, not leak it"


def test_scheduler_task_does_not_start_when_interval_is_disabled(test_env, monkeypatch, tmp_path):
    key_path = tmp_path / "fake-service-account.json"
    key_path.write_text(json.dumps({
        "type": "service_account", "project_id": "fake-project",
        "private_key": "fake-key", "client_email": "fake@fake-project.iam.gserviceaccount.com",
    }))
    monkeypatch.setenv("GOOGLE_DRIVE_FOLDER_ID", "fake-folder-id")
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON_PATH", str(key_path))
    monkeypatch.setenv("INGESTION_SYNC_INTERVAL_MINUTES", "0")
    from app.config import get_settings
    get_settings.cache_clear()

    started = []

    async def fake_loop():
        started.append(True)
        await asyncio.Event().wait()

    monkeypatch.setattr(scheduler, "run_scheduled_sync_loop", fake_loop)

    with TestClient(app):
        pass

    get_settings.cache_clear()
    assert started == [], "a zero/negative interval must disable the scheduler even with Drive configured"

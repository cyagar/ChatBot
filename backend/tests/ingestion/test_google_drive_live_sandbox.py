"""Gated, read-only integration test against a REAL Google Drive sandbox
folder -- never the production manuals folder.

Skipped unless explicitly opted into via TMA_LIVE_DRIVE_TEST=1 plus its own,
separate TMA_LIVE_DRIVE_TEST_FOLDER_ID / TMA_LIVE_DRIVE_TEST_CREDENTIALS_PATH
env vars. Deliberately NOT the app's own GOOGLE_DRIVE_FOLDER_ID /
GOOGLE_SERVICE_ACCOUNT_JSON_PATH settings -- those may be pointed at the real
production folder in a developer's .env, and this test must never be able to
touch that folder just because the app happens to be configured for it --
normal CI must never contact the production folder.

The skip check runs unconditionally as the first line of the test body, not
only via the `live_drive` marker -- a marker alone only stops -m-based
deselection, so a bare `pytest` or `pytest -m ""` run still executes the
function body and must see the skip before anything network-related runs."""
from __future__ import annotations

import os

import pytest

from app.ingestion.sources import GoogleDriveSource


@pytest.mark.live_drive
def test_live_sandbox_folder_lists_and_fetches_read_only(tmp_path):
    if os.environ.get("TMA_LIVE_DRIVE_TEST") != "1":
        pytest.skip("TMA_LIVE_DRIVE_TEST=1 not set -- this test never runs by default.")

    folder_id = os.environ.get("TMA_LIVE_DRIVE_TEST_FOLDER_ID")
    creds_path = os.environ.get("TMA_LIVE_DRIVE_TEST_CREDENTIALS_PATH")
    if not folder_id or not creds_path:
        pytest.fail(
            "TMA_LIVE_DRIVE_TEST=1 requires TMA_LIVE_DRIVE_TEST_FOLDER_ID and "
            "TMA_LIVE_DRIVE_TEST_CREDENTIALS_PATH to also be set -- point these at a "
            "dedicated read-only sandbox folder, never the production manuals folder."
        )

    source = GoogleDriveSource(
        folder_id=folder_id,
        service_account_path=creds_path,
        cache_dir=tmp_path / "cache",
    )

    files = source.list_files()
    assert isinstance(files, list)  # an empty sandbox folder is a valid state; this just proves auth/listing works

    for f in files:
        fetched = source.fetch(f.source_ref)
        assert fetched.exists()
        assert fetched.stat().st_size == f.byte_size

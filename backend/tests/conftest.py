import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture
def test_env(tmp_path, monkeypatch):
    """Isolated settings + a fresh migrated SQLite DB per test. Uses env vars
    (not a .env file) so tests never touch the developer's real data."""
    db_dir = tmp_path / "db"
    storage_dir = tmp_path / "storage"
    manuals_dir = tmp_path / "manuals"
    for d in (db_dir, storage_dir, manuals_dir):
        d.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("DB_PATH", str(db_dir / "test.db"))
    monkeypatch.setenv("LOCAL_STORAGE_DIR", str(storage_dir))
    monkeypatch.setenv("LOCAL_MANUALS_DIR", str(manuals_dir))
    monkeypatch.setenv("SECRET_KEY", "test-secret-key")
    monkeypatch.setenv("AI_PROVIDER", "local_extractive")
    monkeypatch.setenv("TESSERACT_CMD", "")
    # Explicit, not just relying on config.py's default -- the real .env can
    # (and, once a production Drive folder exists, does) set
    # DOCUMENT_SOURCE=google_drive. Without this override, any test that
    # triggers a background ingestion task would make real network calls
    # against the production Drive folder instead of the tmp_path fixture
    # directory above.
    monkeypatch.setenv("DOCUMENT_SOURCE", "local_directory")
    monkeypatch.setenv("GOOGLE_DRIVE_FOLDER_ID", "")
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON_PATH", "")

    from app.config import get_settings
    get_settings.cache_clear()

    from app.db import run_migrations
    run_migrations()

    yield get_settings()

    get_settings.cache_clear()


@pytest.fixture
def make_pdf(tmp_path):
    """Creates a minimal real PDF with the given pages of text, using pymupdf.
    Returns the file path. Used instead of hand-built fixtures so extractors are
    exercised against an actual PDF text layer, not a mock."""
    import fitz

    counter = {"n": 0}

    def _make(pages: list[str], name: str | None = None) -> Path:
        counter["n"] += 1
        doc = fitz.open()
        for text in pages:
            page = doc.new_page()
            page.insert_text((72, 72), text, fontsize=11)
        out = tmp_path / (name or f"synthetic_{counter['n']}.pdf")
        doc.save(out)
        doc.close()
        return out

    return _make

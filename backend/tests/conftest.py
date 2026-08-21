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
    for d in (db_dir, storage_dir):
        d.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("DB_PATH", str(db_dir / "test.db"))
    monkeypatch.setenv("LOCAL_STORAGE_DIR", str(storage_dir))
    monkeypatch.setenv("SECRET_KEY", "test-secret-key")
    monkeypatch.setenv("AI_PROVIDER", "local_extractive")
    monkeypatch.setenv("TESSERACT_CMD", "")
    # Tests that exercise ingestion always pass an explicit FakeDirectorySource
    # (tests/ingestion/fakes.py) to ingest_all(), never relying on
    # get_document_source(settings) -- so these stay blank rather than
    # pointing at any real Drive folder/credential.
    monkeypatch.setenv("GOOGLE_DRIVE_FOLDER_ID", "")
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON_PATH", "")

    from app.config import get_settings
    get_settings.cache_clear()

    from app.db import run_migrations
    run_migrations()

    # slowapi's limiter storage is process-global, not per-request -- without
    # this, request counts from earlier tests in the same run accumulate
    # against the same in-memory bucket (test requests share a fake IP/no
    # session cookie yet) and an unrelated later test can start already
    # partway to AUTH_RATE_LIMIT or RATE_LIMIT_PER_MINUTE. This only resets
    # counters between tests; the limits themselves are untouched.
    from app.rate_limit import limiter
    limiter.reset()

    yield get_settings()

    get_settings.cache_clear()


def register_test_user(client, email, role="technician", password="password123",
                        admin_email="bootstrap-admin@example.com", admin_password="password123"):
    """Test-support helper mirroring the real post-P0-5 flow: public
    self-registration is closed, so getting a logged-in user of any role now
    requires (1) a bootstrap administrator to exist -- created directly via
    app.auth.bootstrap.bootstrap_admin, the same code scripts/bootstrap_admin.py
    uses, never through HTTP -- and (2) that admin issuing a single-use
    invitation the target email then registers with. Leaves `client`'s session
    cookie set to the requested user (or the bootstrap admin itself, if
    email == admin_email)."""
    from app.db import get_conn

    with get_conn() as conn:
        admin_row = conn.execute("SELECT id FROM users WHERE email = ?", (admin_email,)).fetchone()
    if admin_row is None:
        from app.auth.bootstrap import bootstrap_admin
        bootstrap_admin(admin_email, admin_password)

    login_resp = client.post("/api/auth/login", json={"email": admin_email, "password": admin_password})
    assert login_resp.status_code == 200, login_resp.text
    if email == admin_email:
        return login_resp

    invite_resp = client.post("/api/admin/invitations", json={"email": email, "role": role})
    assert invite_resp.status_code == 201, invite_resp.text
    token = invite_resp.json()["token"]

    client.post("/api/auth/logout")
    reg_resp = client.post(
        "/api/auth/register",
        json={"email": email, "password": password, "invite_token": token},
    )
    assert reg_resp.status_code == 201, reg_resp.text
    return reg_resp


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

"""P1-2 (independent follow-up review): "The credential test uses exists(), so
a directory passes."

validate_for_startup() only checked that *something* existed at the
service-account key path. A directory satisfies exists(), so the single most
common secret-mount misconfiguration (mounting a directory where a file was
intended) passed startup validation and only surfaced later as an ingestion
failure. These tests pin the stricter shape check.

Live Drive reachability is deliberately NOT validated at startup -- see the
docstring on Settings._validate_service_account_key.
"""
from __future__ import annotations

import json

import pytest

from app.config import Settings


def _settings(**overrides) -> Settings:
    base = dict(
        app_env="production",
        secret_key="x" * 48,
        ai_provider="local_extractive",
        allowed_registration_domains="example.com",
        google_drive_folder_id="folder123",
    )
    base.update(overrides)
    return Settings(**base)


def _write_key(path, **overrides):
    data = {
        "type": "service_account",
        "project_id": "proj",
        "private_key": "-----BEGIN PRIVATE KEY-----\nfake\n-----END PRIVATE KEY-----\n",
        "client_email": "svc@proj.iam.gserviceaccount.com",
    }
    data.update(overrides)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_directory_at_the_credential_path_is_rejected(tmp_path):
    """The exact bug: a mounted directory satisfies exists()."""
    key_dir = tmp_path / "sa-key.json"
    key_dir.mkdir()
    s = _settings(google_service_account_json_path=str(key_dir))
    with pytest.raises(RuntimeError, match="not a readable file"):
        s.validate_for_startup()


def test_missing_credential_file_is_rejected(tmp_path):
    s = _settings(google_service_account_json_path=str(tmp_path / "nope.json"))
    with pytest.raises(RuntimeError, match="not a readable file"):
        s.validate_for_startup()


def test_non_json_credential_file_is_rejected(tmp_path):
    p = tmp_path / "sa.json"
    p.write_text("this is not json", encoding="utf-8")
    s = _settings(google_service_account_json_path=str(p))
    with pytest.raises(RuntimeError, match="not valid JSON"):
        s.validate_for_startup()


def test_credential_missing_required_fields_is_rejected(tmp_path):
    p = tmp_path / "sa.json"
    p.write_text(json.dumps({"type": "service_account"}), encoding="utf-8")
    s = _settings(google_service_account_json_path=str(p))
    with pytest.raises(RuntimeError, match="missing required field"):
        s.validate_for_startup()


def test_oauth_client_secret_file_is_rejected(tmp_path):
    """An OAuth client-secret JSON is well-formed JSON with plausible-looking
    fields but is not a service-account key and will never authenticate."""
    p = tmp_path / "sa.json"
    _write_key(p, type="authorized_user")
    s = _settings(google_service_account_json_path=str(p))
    with pytest.raises(RuntimeError, match="expected 'service_account'"):
        s.validate_for_startup()


def test_valid_service_account_key_passes(tmp_path):
    p = _write_key(tmp_path / "sa.json")
    s = _settings(google_service_account_json_path=str(p))
    s.validate_for_startup()  # must not raise


def test_error_message_never_echoes_credential_contents(tmp_path):
    """Startup errors are logged and often surfaced in container logs -- they
    must name the path and the structural problem, never the key material,
    client_email, or project id."""
    p = tmp_path / "sa.json"
    _write_key(p, type="authorized_user", client_email="secret-svc@my-real-project.iam.gserviceaccount.com",
                project_id="my-real-project")
    s = _settings(google_service_account_json_path=str(p))
    with pytest.raises(RuntimeError) as exc:
        s.validate_for_startup()
    message = str(exc.value)
    assert "secret-svc@my-real-project" not in message
    assert "my-real-project" not in message
    assert "BEGIN PRIVATE KEY" not in message

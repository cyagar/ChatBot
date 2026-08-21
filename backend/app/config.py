from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=BACKEND_DIR / ".env", extra="ignore")

    app_env: str = "development"
    secret_key: str = "dev-only-insecure-key"
    session_ttl_minutes: int = 480

    storage_backend: str = "local"
    local_storage_dir: str = "../data/object_storage"
    db_path: str = "../data/db/app.db"

    # Google Drive is the only document source (2026-08-21: local-directory
    # ingestion and the direct-upload path were retired entirely).
    google_drive_folder_id: str = ""
    # Path to the downloaded service-account JSON key file, not the key
    # content itself -- keeps a private key out of the .env file/process
    # environment, mirroring how the key is kept out of git (see .gitignore).
    google_service_account_json_path: str = ""
    gdrive_cache_dir: str = "../data/gdrive_cache"

    ai_provider: str = "local_extractive"
    anthropic_api_key: str = ""
    openai_api_key: str = ""

    embedding_model: str = "BAAI/bge-small-en-v1.5"
    # Pinned commit SHA, not a branch name -- "main" can silently change what
    # bytes get downloaded. Baked into the Docker image at build time (see
    # Dockerfile) so first use never depends on an unannounced runtime
    # download (independent review concern #18).
    embedding_model_revision: str = "5c38ec7c405ec4b44b94cc5a9bb96e735b38267a"

    tesseract_cmd: str = ""

    rate_limit_per_minute: int = 60

    # Comma-separated email domains allowed to self-register (e.g.
    # "hmwagner.com,contractor-partner.com"). Empty = open registration --
    # fine for local dev, NOT for a real deployment holding proprietary
    # manuals (independent review concern #19). Set this before any pilot.
    allowed_registration_domains: str = ""

    def validate_for_startup(self) -> None:
        """Refuse to boot with an insecure or inconsistent configuration once we're
        outside local development. A blank/default/short SECRET_KEY means session
        cookies can be forged; a provider selected without its key means every
        chat request will fail at call time instead of at startup."""
        if self.app_env != "development":
            if not self.secret_key or self.secret_key == "dev-only-insecure-key" or len(self.secret_key) < 32:
                raise RuntimeError(
                    "SECRET_KEY is missing, default, or too short for APP_ENV="
                    f"{self.app_env!r}. Generate a unique random secret per environment."
                )
            if self.ai_provider == "anthropic" and not self.anthropic_api_key:
                raise RuntimeError("AI_PROVIDER=anthropic but ANTHROPIC_API_KEY is not set.")
            if self.ai_provider == "openai" and not self.openai_api_key:
                raise RuntimeError("AI_PROVIDER=openai but OPENAI_API_KEY is not set.")
            if not self.allowed_registration_domains:
                raise RuntimeError(
                    "ALLOWED_REGISTRATION_DOMAINS is not set for APP_ENV="
                    f"{self.app_env!r}. Registration itself requires an admin-issued invitation "
                    "now, but an administrator could still invite any email address without this "
                    "set -- it's the allowlist invitations are restricted to. Set it to a "
                    "comma-separated list before deploying."
                )
        if self.ai_provider not in {"local_extractive", "anthropic", "openai"}:
            raise RuntimeError(f"Unknown AI_PROVIDER: {self.ai_provider!r}")
        if not self.google_drive_folder_id:
            raise RuntimeError("GOOGLE_DRIVE_FOLDER_ID is not set -- ingestion has no document source.")
        if not self.google_service_account_json_path_resolved.exists():
            raise RuntimeError(
                "Google service-account key file was not found at "
                f"{self.google_service_account_json_path_resolved}."
            )

    @property
    def db_path_resolved(self) -> Path:
        p = Path(self.db_path)
        return p if p.is_absolute() else (BACKEND_DIR / p).resolve()

    @property
    def local_storage_dir_resolved(self) -> Path:
        p = Path(self.local_storage_dir)
        return p if p.is_absolute() else (BACKEND_DIR / p).resolve()

    @property
    def google_service_account_json_path_resolved(self) -> Path:
        p = Path(self.google_service_account_json_path)
        return p if p.is_absolute() else (BACKEND_DIR / p).resolve()

    @property
    def gdrive_cache_dir_resolved(self) -> Path:
        p = Path(self.gdrive_cache_dir)
        return p if p.is_absolute() else (BACKEND_DIR / p).resolve()


@lru_cache
def get_settings() -> Settings:
    return Settings()

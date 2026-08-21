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

    document_source: str = "local_directory"
    local_manuals_dir: str = "../data/manuals_incoming"
    google_drive_folder_id: str = ""
    google_service_account_json: str = ""

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
        if self.ai_provider not in {"local_extractive", "anthropic", "openai"}:
            raise RuntimeError(f"Unknown AI_PROVIDER: {self.ai_provider!r}")

    @property
    def db_path_resolved(self) -> Path:
        p = Path(self.db_path)
        return p if p.is_absolute() else (BACKEND_DIR / p).resolve()

    @property
    def local_storage_dir_resolved(self) -> Path:
        p = Path(self.local_storage_dir)
        return p if p.is_absolute() else (BACKEND_DIR / p).resolve()

    @property
    def local_manuals_dir_resolved(self) -> Path:
        p = Path(self.local_manuals_dir)
        return p if p.is_absolute() else (BACKEND_DIR / p).resolve()


@lru_cache
def get_settings() -> Settings:
    return Settings()

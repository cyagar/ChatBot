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

    tesseract_cmd: str = ""

    rate_limit_per_minute: int = 60

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

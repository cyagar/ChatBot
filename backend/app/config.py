from functools import lru_cache
from pathlib import Path
from typing import ClassVar

from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=BACKEND_DIR / ".env", extra="ignore")

    app_env: str = "development"
    secret_key: str = "dev-only-insecure-key"
    session_ttl_minutes: int = 480

    storage_backend: str = "local"
    local_storage_dir: str = "../data/object_storage"

    # Postgres (Neon). Pooled for normal app traffic; unpooled (direct) for
    # schema migrations, which need session-level BEGIN/COMMIT semantics that
    # PgBouncer transaction pooling doesn't support -- see app/db.py.
    database_url: str = ""
    database_url_unpooled: str = ""

    # Google Drive is the only document source (2026-08-21: local-directory
    # ingestion and the direct-upload path were retired entirely).
    google_drive_folder_id: str = ""
    # Path to the downloaded service-account JSON key file, not the key
    # content itself -- keeps a private key out of the .env file/process
    # environment, mirroring how the key is kept out of git (see .gitignore).
    google_service_account_json_path: str = ""
    gdrive_cache_dir: str = "../data/gdrive_cache"
    # Guardrail against a pathological single file (independent follow-up
    # review P1-3: "apply file count/size/type limits") -- generously above
    # any real manual, just bounds a stray multi-GB file. No file-COUNT
    # limit: capping list_files() at N files would make it return a partial
    # listing once a folder passes N, indistinguishable from files having
    # been removed -- see GoogleDriveSource's docstring for why that's
    # deliberately not done.
    max_drive_file_size_mb: int = 200
    # Automated corpus freshness (independent follow-up review P1-4: "corpus
    # freshness depends on an admin remembering to reindex"). A background
    # loop calls ingest_all() every N minutes so a manual "Run re-index now"
    # click is an override, not the only freshness mechanism. 0 disables it
    # (e.g. local dev without a configured Drive folder); the loop also never
    # starts unless google_drive_folder_id is set, so tests -- which always
    # leave that blank -- never start it regardless of this default.
    ingestion_sync_interval_minutes: int = 360  # 6 hours
    # The operational SLA the review asked for: how long the corpus can go
    # without a successful sync before the admin UI flags it as stale. Well
    # above the sync interval so one or two missed/slow ticks don't alarm.
    ingestion_staleness_threshold_hours: int = 48

    ai_provider: str = "local_extractive"
    anthropic_api_key: str = ""

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

    # GET /api/config (Phase 1, narrowed scope). All four are read, not
    # enforced -- a maintenance banner and a minimum-version nudge are UI
    # concerns for whichever client reads this; nothing server-side blocks a
    # request based on these values today.
    maintenance_mode: bool = False
    maintenance_message: str = ""
    # Compared against nothing automatically -- there is no client-submitted
    # app-version header/field this checks against yet. Kept in sync by hand
    # with android/app/build.gradle.kts's versionName for now.
    minimum_supported_version: str = "0.1.0-demo"
    # A real support inbox/URL for this deployment. The default below is a
    # placeholder, not a live inbox -- set this explicitly before treating
    # /api/config's support_contact as something a technician can actually
    # use.
    support_contact: str = "support@hmwagner.com"

    # P0-12 (external review, 2026-09-21): backend/.env.example's own SECRET_KEY
    # placeholder ("change-me-to-a-random-64-char-string") is 36 characters --
    # long enough to slip past a bare `len(secret_key) < 32` check, and isn't
    # equal to "dev-only-insecure-key" either, so a deployment that copied
    # .env.example into .env without replacing this line would boot in
    # APP_ENV=production with a session-signing key that's public knowledge
    # (this file is committed to the repo) -- anyone who reads it can mint
    # valid signed session tokens for any user ID/token_version they can
    # observe or guess. A maintained list catches known placeholders by
    # value, independent of length; the example file's own placeholder was
    # also shortened below so it fails the length check too, in case this
    # list is ever incomplete.
    _KNOWN_INSECURE_SECRET_KEYS: ClassVar[frozenset[str]] = frozenset({
        "dev-only-insecure-key",
        "change-me-to-a-random-64-char-string",
        "REPLACE_ME",
        "changeme",
        "change-me",
        "secret",
        "your-secret-key-here",
    })

    def validate_for_startup(self) -> None:
        """Refuse to boot with an insecure or inconsistent configuration once we're
        outside local development. A blank/default/short SECRET_KEY means session
        cookies can be forged; a provider selected without its key means every
        chat request will fail at call time instead of at startup."""
        if self.app_env != "development":
            if (
                not self.secret_key
                or self.secret_key.lower() in {v.lower() for v in self._KNOWN_INSECURE_SECRET_KEYS}
                or len(self.secret_key) < 32
            ):
                raise RuntimeError(
                    "SECRET_KEY is missing, a known placeholder, or too short for APP_ENV="
                    f"{self.app_env!r}. Generate a unique random secret per environment -- "
                    "never copy the example value from .env.example."
                )
            if self.ai_provider == "anthropic" and not self.anthropic_api_key:
                raise RuntimeError("AI_PROVIDER=anthropic but ANTHROPIC_API_KEY is not set.")
            if not self.allowed_registration_domains:
                raise RuntimeError(
                    "ALLOWED_REGISTRATION_DOMAINS is not set for APP_ENV="
                    f"{self.app_env!r}. Registration itself requires an admin-issued invitation "
                    "now, but an administrator could still invite any email address without this "
                    "set -- it's the allowlist invitations are restricted to. Set it to a "
                    "comma-separated list before deploying."
                )
        if self.ai_provider not in {"local_extractive", "anthropic"}:
            raise RuntimeError(f"Unknown AI_PROVIDER: {self.ai_provider!r}")

        if not self.database_url or not self.database_url_unpooled:
            raise RuntimeError(
                "DATABASE_URL / DATABASE_URL_UNPOOLED are not set -- there is no database to "
                "connect to."
            )

        # P1-2's "production can boot with the wrong source" half is already
        # structurally impossible: there is no DOCUMENT_SOURCE setting any
        # more, and get_document_source() (app/ingestion/sources.py) can only
        # ever construct a GoogleDriveSource. FakeDirectorySource
        # (tests/ingestion/fakes.py) survives solely as test infrastructure,
        # passed explicitly to ingest_all().
        if not self.google_drive_folder_id:
            raise RuntimeError("GOOGLE_DRIVE_FOLDER_ID is not set -- ingestion has no document source.")
        self._validate_service_account_key()

    def _validate_service_account_key(self) -> None:
        """Validate the credential is a readable, parseable service-account key
        file -- not merely that something exists at that path.

        `.exists()` is true for a DIRECTORY, so a misconfigured secret mount
        (a very common Docker/Kubernetes mistake: mounting a directory where a
        file was intended) passed startup validation and only failed later at
        the first ingestion attempt (independent follow-up review P1-2).

        This is a LOCAL shape check only. It deliberately does not call Drive:
        a bounded network readiness probe at startup would turn any Drive
        outage into a boot failure, taking down chat/retrieval that don't need
        Drive at all. Live reachability is therefore NOT verified here.
        Error messages never echo the key's contents, client_email, or
        project id -- only the path and the structural problem."""
        import json

        path = self.google_service_account_json_path_resolved
        if not path.is_file():
            raise RuntimeError(
                f"Google service-account key at {path} is not a readable file "
                "(missing, or a directory -- check the secret mount)."
            )
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as e:
            raise RuntimeError(
                f"Google service-account key at {path} could not be read: {e.strerror}."
            ) from e
        try:
            data = json.loads(raw)
        except ValueError as e:
            raise RuntimeError(
                f"Google service-account key at {path} is not valid JSON."
            ) from e
        if not isinstance(data, dict):
            raise RuntimeError(f"Google service-account key at {path} is not a JSON object.")
        missing = [k for k in ("type", "project_id", "private_key", "client_email") if not data.get(k)]
        if missing:
            raise RuntimeError(
                f"Google service-account key at {path} is missing required field(s): "
                f"{', '.join(missing)}. This does not look like a service-account key file."
            )
        if data.get("type") != "service_account":
            raise RuntimeError(
                f"Google credential at {path} has type={data.get('type')!r}, expected "
                "'service_account' (an OAuth client-secret file will not work here)."
            )

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

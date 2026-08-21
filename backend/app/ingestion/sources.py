"""Document sources.

Manuals live in a shared Google Drive folder -- that's the only source of
record (2026-08-21: the local-directory corpus/upload path was retired
entirely, so there is exactly one place the corpus can live). Ingestion talks
only to the `DocumentSource` interface below; tests exercise the pipeline
against a small directory-scanning fake (`tests/ingestion/fakes.py`), not
against this module or a live Drive connection.
"""

from __future__ import annotations

import abc
import hashlib
import io
import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SourceFile:
    """One file as seen by a DocumentSource, before any parsing happens."""

    source_ref: str          # stable identifier within the source (path, or Drive file id)
    filename: str
    local_path: Path         # always a local filesystem path once fetch() has run
    byte_size: int
    sha256: str


class DocumentSource(abc.ABC):
    source_system: str

    @abc.abstractmethod
    def list_files(self) -> list[SourceFile]:
        """Enumerate all currently-available files. Must be safe to call repeatedly."""

    def fetch(self, source_ref: str) -> Path:
        """Return a local path for the given source_ref (downloading/caching first
        if needed)."""
        raise NotImplementedError


def _sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


class GoogleDriveSource(DocumentSource):
    """Reads manuals from a shared Google Drive folder via a service account.

    Drive only exposes an md5Checksum for binary files, but the rest of the
    pipeline (cross-source exact-duplicate detection, documents.sha256)
    assumes real SHA-256 throughout -- so list_files() downloads each file
    into a local cache keyed by Drive file ID and hashes the cached bytes
    itself.

    Cache validity is keyed on Drive's md5Checksum (falling back to
    modifiedTime when a binary file has none), not on file size alone -- two
    different files, or two different revisions of the same file, can easily
    share a byte count. A manifest.json in the cache directory records the
    checksum/filename that was actually verified for each Drive file ID, and
    fetch() reads that manifest instead of glob-matching "some file whose
    name starts with this ID" -- after a rename, the old cache file used to
    stick around and glob() could return either one, silently feeding the
    pipeline different bytes than the ones it just hashed. Downloads land in
    a temp file first and are atomically renamed into place, so a crash or
    interrupted download can never be mistaken for a valid cache entry.

    Google Workspace files (Docs/Sheets/Slides -- mimeType
    'application/vnd.google-apps.*') have no downloadable binary and no
    md5Checksum; get_media() on them fails outright. They're skipped with a
    logged reason rather than attempted, and files the service account
    can't download (capabilities.canDownload = false) are skipped the same
    way.

    No incremental sync (changes.list/page tokens) by design: at this corpus
    size, a full listing every run is cheap, and idempotency is already
    handled by the existing sha256-based skip-if-unchanged logic in
    pipeline.py -- adding change-token bookkeeping on top would be
    complexity without a corresponding benefit yet.
    """

    source_system = "google_drive"
    SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
    _WORKSPACE_MIME_PREFIX = "application/vnd.google-apps."
    _LIST_FIELDS = (
        "nextPageToken, files(id, name, size, mimeType, md5Checksum, "
        "modifiedTime, capabilities(canDownload))"
    )

    def __init__(self, folder_id: str, service_account_path: Path, cache_dir: Path):
        self.folder_id = folder_id
        self.service_account_path = Path(service_account_path)
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._manifest_path = self.cache_dir / "manifest.json"
        self._service = None

    def _get_service(self):
        if self._service is None:
            from google.oauth2 import service_account
            from googleapiclient.discovery import build

            creds = service_account.Credentials.from_service_account_file(
                str(self.service_account_path), scopes=self.SCOPES
            )
            self._service = build("drive", "v3", credentials=creds, cache_discovery=False)
        return self._service

    def _load_manifest(self) -> dict:
        if not self._manifest_path.exists():
            return {}
        try:
            return json.loads(self._manifest_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            logger.warning("Drive cache manifest at %s is unreadable; treating as empty.",
                            self._manifest_path)
            return {}

    def _save_manifest(self, manifest: dict) -> None:
        tmp_fd, tmp_name = tempfile.mkstemp(dir=self.cache_dir, prefix="manifest__", suffix=".tmp")
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                json.dump(manifest, f, indent=2)
            os.replace(tmp_name, self._manifest_path)
        except Exception:
            Path(tmp_name).unlink(missing_ok=True)
            raise

    def _cache_path(self, file_id: str, name: str) -> Path:
        safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", name)
        return self.cache_dir / f"{file_id}__{safe_name}"

    def _download(self, service, file_id: str, cache_path: Path) -> None:
        """Downloads to a temp file in the cache dir and atomically renames it into
        place, retrying transient failures a few times before giving up."""
        from googleapiclient.http import MediaIoBaseDownload

        last_error: Exception | None = None
        for attempt in range(1, 4):
            tmp_name: str | None = None
            try:
                request = service.files().get_media(fileId=file_id)
                buf = io.BytesIO()
                downloader = MediaIoBaseDownload(buf, request)
                done = False
                while not done:
                    _, done = downloader.next_chunk()
                tmp_fd, tmp_name = tempfile.mkstemp(dir=self.cache_dir, prefix=f"{file_id}__.dl")
                with os.fdopen(tmp_fd, "wb") as f:
                    f.write(buf.getvalue())
                os.replace(tmp_name, cache_path)
                return
            except Exception as e:
                last_error = e
                if tmp_name is not None:
                    Path(tmp_name).unlink(missing_ok=True)
                logger.warning("Drive download attempt %d/3 failed for file %s: %s", attempt, file_id, e)
        raise RuntimeError(f"Failed to download Drive file {file_id} after 3 attempts: {last_error}")

    def list_files(self) -> list[SourceFile]:
        service = self._get_service()
        manifest = self._load_manifest()
        out: list[SourceFile] = []
        page_token = None
        while True:
            resp = (
                service.files()
                .list(
                    q=f"'{self.folder_id}' in parents and trashed = false",
                    fields=self._LIST_FIELDS,
                    pageSize=100,
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True,
                    pageToken=page_token,
                )
                .execute()
            )
            for f in resp.get("files", []):
                file_id = f["id"]
                name = f["name"]
                mime_type = f.get("mimeType", "")

                if mime_type.startswith(self._WORKSPACE_MIME_PREFIX):
                    logger.info("Skipping Drive file %s (%r): Google Workspace files have no "
                                "downloadable binary.", file_id, name)
                    continue
                if f.get("capabilities", {}).get("canDownload") is False:
                    logger.warning("Skipping Drive file %s (%r): service account lacks download "
                                    "permission.", file_id, name)
                    continue

                md5 = f.get("md5Checksum")
                modified_time = f.get("modifiedTime")
                reported_size = int(f.get("size") or 0)
                cache_path = self._cache_path(file_id, name)

                entry = manifest.get(file_id)
                cache_valid = (
                    entry is not None
                    and entry.get("cache_filename") == cache_path.name
                    and cache_path.exists()
                    and cache_path.stat().st_size == reported_size
                    and (
                        (md5 is not None and entry.get("md5Checksum") == md5)
                        or (md5 is None and entry.get("modifiedTime") == modified_time)
                    )
                )

                if not cache_valid:
                    self._download(service, file_id, cache_path)
                    # A rename changes the cache filename (it embeds the name);
                    # drop any other cached copy left behind under this file ID
                    # so a future fetch() can never see more than one candidate.
                    for stale in self.cache_dir.glob(f"{file_id}__*"):
                        if stale != cache_path:
                            stale.unlink(missing_ok=True)
                    manifest[file_id] = {
                        "cache_filename": cache_path.name,
                        "md5Checksum": md5,
                        "modifiedTime": modified_time,
                        "size": reported_size,
                    }

                out.append(
                    SourceFile(
                        source_ref=f"{self.source_system}:{file_id}",
                        filename=name,
                        local_path=cache_path,
                        byte_size=cache_path.stat().st_size,
                        sha256=_sha256_of(cache_path),
                    )
                )
            page_token = resp.get("nextPageToken")
            if not page_token:
                break

        self._save_manifest(manifest)
        return out

    def fetch(self, source_ref: str) -> Path:
        _, _, file_id = source_ref.partition(":")
        entry = self._load_manifest().get(file_id)
        if entry is not None:
            path = self.cache_dir / entry["cache_filename"]
            if path.exists():
                return path
        raise FileNotFoundError(
            f"Drive file {file_id} not found in the local cache -- list_files() "
            "must run at least once before fetch() for a new file."
        )


def get_document_source(settings) -> DocumentSource:
    if not settings.google_drive_folder_id:
        raise RuntimeError(
            "GOOGLE_DRIVE_FOLDER_ID is not set -- there is no configured document source. "
            "(Tests must pass an explicit source= to ingest_all() rather than relying on this.)"
        )
    return GoogleDriveSource(
        folder_id=settings.google_drive_folder_id,
        service_account_path=settings.google_service_account_json_path_resolved,
        cache_dir=settings.gdrive_cache_dir_resolved,
    )

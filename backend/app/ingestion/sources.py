"""Pluggable document sources.

The plan's production intent is a Google Drive folder that is regularly updated
with new manuals. Every ingestion component talks only to the `DocumentSource`
interface below, so `GoogleDriveSource` and `LocalDirectorySource` are
interchangeable from pipeline.py's point of view. Swap via the DOCUMENT_SOURCE
env var and the `get_document_source` factory below.
"""

from __future__ import annotations

import abc
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path


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
        """Return a local path for the given source_ref. LocalDirectorySource is a
        no-op passthrough; GoogleDriveSource would download to a local cache here."""
        raise NotImplementedError


def _sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


class LocalDirectorySource(DocumentSource):
    """Scans a flat local directory. Used for local development and for the
    initial one-off ZIP-derived corpus."""

    source_system = "local_directory"

    def __init__(self, directory: Path):
        self.directory = Path(directory)

    def list_files(self) -> list[SourceFile]:
        if not self.directory.exists():
            return []
        out = []
        for p in sorted(self.directory.iterdir()):
            if not p.is_file():
                continue
            out.append(
                SourceFile(
                    # Relative to the configured corpus root, not an absolute
                    # path -- an absolute path is specific to one machine, so
                    # relocating (or re-extracting) an otherwise-unchanged
                    # corpus made every file look like a brand new source and
                    # created a duplicate document row (independent review
                    # concern #13, reproduced directly: moving a file created
                    # document row 72 for the same bytes as row 71).
                    source_ref=f"{self.source_system}:{p.relative_to(self.directory).as_posix()}",
                    filename=p.name,
                    local_path=p,
                    byte_size=p.stat().st_size,
                    sha256=_sha256_of(p),
                )
            )
        return out

    def fetch(self, source_ref: str) -> Path:
        _, _, rel = source_ref.partition(":")
        return self.directory / rel


class GoogleDriveSource(DocumentSource):
    """Reads manuals from a shared Google Drive folder via a service account.

    Drive only exposes an md5Checksum for binary files, but the rest of the
    pipeline (cross-source exact-duplicate detection, documents.sha256)
    assumes real SHA-256 throughout, same as LocalDirectorySource -- so
    list_files() downloads each file into a local cache keyed by Drive file
    ID and hashes the cached bytes itself. A cached file whose size already
    matches Drive's reported size is reused rather than re-downloaded, so a
    repeat listing (e.g. from the manual "re-index now" trigger) only pays
    the download cost for files that are new or changed.

    No incremental sync (changes.list/page tokens) by design: at this corpus
    size, a full listing every run is cheap, and idempotency is already
    handled by the existing sha256-based skip-if-unchanged logic in
    pipeline.py -- adding change-token bookkeeping on top would be
    complexity without a corresponding benefit yet.
    """

    source_system = "google_drive"
    SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

    def __init__(self, folder_id: str, service_account_path: Path, cache_dir: Path):
        self.folder_id = folder_id
        self.service_account_path = Path(service_account_path)
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
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

    def _cache_path(self, file_id: str, name: str) -> Path:
        safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", name)
        return self.cache_dir / f"{file_id}__{safe_name}"

    def list_files(self) -> list[SourceFile]:
        import io

        from googleapiclient.http import MediaIoBaseDownload

        service = self._get_service()
        out: list[SourceFile] = []
        page_token = None
        while True:
            resp = (
                service.files()
                .list(
                    q=f"'{self.folder_id}' in parents and trashed = false",
                    fields="nextPageToken, files(id, name, size)",
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
                reported_size = int(f.get("size") or 0)
                cache_path = self._cache_path(file_id, name)

                if not (cache_path.exists() and cache_path.stat().st_size == reported_size):
                    request = service.files().get_media(fileId=file_id)
                    buf = io.BytesIO()
                    downloader = MediaIoBaseDownload(buf, request)
                    done = False
                    while not done:
                        _, done = downloader.next_chunk()
                    cache_path.write_bytes(buf.getvalue())

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
        return out

    def fetch(self, source_ref: str) -> Path:
        _, _, file_id = source_ref.partition(":")
        matches = list(self.cache_dir.glob(f"{file_id}__*"))
        if matches:
            return matches[0]
        raise FileNotFoundError(
            f"Drive file {file_id} not found in the local cache -- list_files() "
            "must run at least once before fetch() for a new file."
        )


def get_document_source(settings) -> DocumentSource:
    if settings.document_source == "local_directory":
        return LocalDirectorySource(settings.local_manuals_dir_resolved)
    if settings.document_source == "google_drive":
        return GoogleDriveSource(
            folder_id=settings.google_drive_folder_id,
            service_account_path=settings.google_service_account_json_path_resolved,
            cache_dir=settings.gdrive_cache_dir_resolved,
        )
    raise ValueError(f"Unknown DOCUMENT_SOURCE: {settings.document_source!r}")

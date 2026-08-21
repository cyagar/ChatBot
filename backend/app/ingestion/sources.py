"""Pluggable document sources.

The plan's production intent is a Google Drive folder that is regularly updated
with new manuals. Rather than build the Drive integration now (no folder to point
at yet, and secrets aren't available), every ingestion component below talks only
to this `DocumentSource` interface. `LocalDirectorySource` is the only
implementation today; a future `GoogleDriveSource` need only implement the same
three methods (list, fetch, source metadata) using the Drive API `files.list` /
`changes.list` for incremental sync, and nothing in ingestion/pipeline.py,
extractors.py, dedup.py, etc. would need to change. Swap it via the
DOCUMENT_SOURCE env var and a small factory (see `get_document_source` below).
"""

from __future__ import annotations

import abc
import hashlib
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


def get_document_source(settings) -> DocumentSource:
    if settings.document_source == "local_directory":
        return LocalDirectorySource(settings.local_manuals_dir_resolved)
    if settings.document_source == "google_drive":
        raise NotImplementedError(
            "Google Drive source is not implemented yet. Set DOCUMENT_SOURCE=local_directory "
            "for now. When the production Drive folder is ready, implement GoogleDriveSource "
            "(list_files via files.list scoped to GOOGLE_DRIVE_FOLDER_ID, fetch via files.get "
            "media download, incremental re-sync via changes.list) and register it here."
        )
    raise ValueError(f"Unknown DOCUMENT_SOURCE: {settings.document_source!r}")

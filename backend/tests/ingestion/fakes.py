"""Test-only DocumentSource implementations.

Production ingestion has exactly one real source (GoogleDriveSource in
app/ingestion/sources.py) -- there is no local-directory source in the app
anymore. FakeDirectorySource exists purely so the ingestion pipeline's
idempotency/dedup/relocation behavior can be exercised against real
synthetic files on disk without live Drive access; it is never imported by
application code.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from app.ingestion.sources import DocumentSource, SourceFile


def _sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


class FakeDirectorySource(DocumentSource):
    """Scans a flat local directory. source_ref is relative to the given
    directory (not absolute), mirroring the real source's contract so tests
    can exercise corpus-root-relocation behavior (independent review concern
    #13: an absolute-path source_ref made relocating the corpus root create
    duplicate document rows for unchanged bytes)."""

    source_system = "test_directory"

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

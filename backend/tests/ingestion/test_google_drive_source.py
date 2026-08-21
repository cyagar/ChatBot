"""Unit tests for GoogleDriveSource's cache-validity and fetch() logic.

No live Drive/network access: `_get_service()` is monkeypatched to return a
fake Drive `files().list()` client, and `_download()` is monkeypatched to
write fixed bytes instead of calling the real API. This isolates exactly the
bug the independent follow-up review flagged (P0-1): list_files() deciding
cache validity from file size alone, and fetch() picking a cache file by
glob() instead of the exact one that was actually verified/hashed.
"""

from __future__ import annotations

from app.ingestion.sources import GoogleDriveSource


class _FakeExecutable:
    def __init__(self, resp):
        self._resp = resp

    def execute(self):
        return self._resp


class _FakeFiles:
    def __init__(self, pages):
        self._pages = list(pages)
        self.list_calls = 0

    def list(self, **kwargs):
        resp = self._pages[self.list_calls] if self.list_calls < len(self._pages) else {"files": []}
        self.list_calls += 1
        return _FakeExecutable(resp)


class _FakeService:
    def __init__(self, pages):
        self._files = _FakeFiles(pages)

    def files(self):
        return self._files


def _make_source(tmp_path, pages, downloads: dict[str, bytes]):
    source = GoogleDriveSource(
        folder_id="fake-folder",
        service_account_path=tmp_path / "unused-key.json",
        cache_dir=tmp_path / "cache",
    )
    fake_service = _FakeService(pages)
    source._service = fake_service

    download_calls = []

    def fake_download(service, file_id, cache_path):
        download_calls.append(file_id)
        cache_path.write_bytes(downloads[file_id])

    source._download = fake_download
    return source, fake_service, download_calls


def test_first_listing_downloads_every_file(tmp_path):
    pages = [{"files": [
        {"id": "f1", "name": "axiom.pdf", "size": "3", "mimeType": "application/pdf", "md5Checksum": "aaa"},
        {"id": "f2", "name": "cma.pdf", "size": "3", "mimeType": "application/pdf", "md5Checksum": "bbb"},
    ]}]
    source, _, calls = _make_source(tmp_path, pages, {"f1": b"AAA", "f2": b"BBB"})

    files = source.list_files()

    assert calls == ["f1", "f2"]
    assert {f.source_ref for f in files} == {"google_drive:f1", "google_drive:f2"}


def test_same_size_different_checksum_is_not_treated_as_cached(tmp_path):
    """The exact P0-1 bug: two revisions can share a byte count. Cache
    validity must key on md5Checksum, not size alone."""
    page1 = {"files": [{"id": "f1", "name": "manual.pdf", "size": "3",
                         "mimeType": "application/pdf", "md5Checksum": "checksum-v1"}]}
    page2 = {"files": [{"id": "f1", "name": "manual.pdf", "size": "3",
                         "mimeType": "application/pdf", "md5Checksum": "checksum-v2"}]}
    source, service, calls = _make_source(
        tmp_path, [page1], {"f1": b"AAA"}
    )
    source.list_files()
    assert calls == ["f1"]

    # Second revision: same reported size, different checksum, different bytes.
    service._files._pages = [page2]
    service._files.list_calls = 0
    source._download = lambda service, file_id, cache_path: (calls.append(file_id), cache_path.write_bytes(b"ZZZ"))

    files = source.list_files()
    assert calls == ["f1", "f1"], "changed checksum at the same size must trigger a re-download"
    assert files[0].sha256 == __import__("hashlib").sha256(b"ZZZ").hexdigest()


def test_unchanged_checksum_is_not_redownloaded(tmp_path):
    page = {"files": [{"id": "f1", "name": "manual.pdf", "size": "3",
                        "mimeType": "application/pdf", "md5Checksum": "same-checksum"}]}
    source, service, calls = _make_source(tmp_path, [page], {"f1": b"AAA"})
    source.list_files()
    assert calls == ["f1"]

    service._files._pages = [page]
    service._files.list_calls = 0
    source.list_files()
    assert calls == ["f1"], "an unchanged checksum must reuse the cached file, not re-download"


def test_rename_does_not_leave_a_stale_cache_file_fetchable(tmp_path):
    """The exact P0-1 bug reproduction: a rename used to leave the old cache
    file behind, and fetch()'s glob(file_id + "__*") could return either one.
    After a rename, fetch() must return only the bytes from the new name."""
    page1 = {"files": [{"id": "f1", "name": "old_name.pdf", "size": "3",
                         "mimeType": "application/pdf", "md5Checksum": "same-checksum"}]}
    source, service, calls = _make_source(tmp_path, [page1], {"f1": b"AAA"})
    source.list_files()

    page2 = {"files": [{"id": "f1", "name": "new_name.pdf", "size": "3",
                         "mimeType": "application/pdf", "md5Checksum": "same-checksum"}]}
    service._files._pages = [page2]
    service._files.list_calls = 0
    # Cache-filename changed (embeds the name) even though content didn't --
    # must still resolve to exactly one file.
    source._download = lambda service, file_id, cache_path: (calls.append(file_id), cache_path.write_bytes(b"AAA"))
    source.list_files()

    remaining = list((tmp_path / "cache").glob("f1__*"))
    assert len(remaining) == 1, "the stale pre-rename cache file must be removed"
    assert remaining[0].name.endswith("new_name.pdf")

    fetched = source.fetch("google_drive:f1")
    assert fetched == remaining[0]


def test_fetch_uses_manifest_not_glob_ambiguity(tmp_path):
    source, _, _ = _make_source(
        tmp_path,
        [{"files": [{"id": "f1", "name": "manual.pdf", "size": "3",
                     "mimeType": "application/pdf", "md5Checksum": "c1"}]}],
        {"f1": b"AAA"},
    )
    source.list_files()

    # Simulate an orphaned stray file under the same file ID that glob() would
    # have matched (e.g. left over from an interrupted process in a version
    # before the manifest existed).
    (tmp_path / "cache" / "f1__decoy.pdf").write_bytes(b"WRONG BYTES")

    fetched = source.fetch("google_drive:f1")
    assert fetched.name == "f1__manual.pdf"
    assert fetched.read_bytes() == b"AAA"


def test_workspace_files_are_skipped_not_downloaded(tmp_path):
    pages = [{"files": [
        {"id": "doc1", "name": "Untitled document", "mimeType": "application/vnd.google-apps.document"},
        {"id": "f1", "name": "manual.pdf", "size": "3", "mimeType": "application/pdf", "md5Checksum": "c1"},
    ]}]
    source, _, calls = _make_source(tmp_path, pages, {"f1": b"AAA"})

    files = source.list_files()

    assert calls == ["f1"]
    assert [f.source_ref for f in files] == ["google_drive:f1"]


def test_undownloadable_files_are_skipped(tmp_path):
    pages = [{"files": [
        {"id": "locked1", "name": "restricted.pdf", "size": "3", "mimeType": "application/pdf",
         "md5Checksum": "c1", "capabilities": {"canDownload": False}},
    ]}]
    source, _, calls = _make_source(tmp_path, pages, {})

    files = source.list_files()

    assert calls == []
    assert files == []


def test_fetch_before_any_listing_raises(tmp_path):
    source, _, _ = _make_source(tmp_path, [{"files": []}], {})
    try:
        source.fetch("google_drive:never-listed")
        assert False, "expected FileNotFoundError"
    except FileNotFoundError:
        pass

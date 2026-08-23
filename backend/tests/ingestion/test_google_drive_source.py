"""Contract tests for GoogleDriveSource (independent follow-up review P1-1:
"the new production source has no automated test coverage").

No live Drive/network access anywhere in this file: `_get_service()` is
monkeypatched to return a fake Drive `files().list()` client, and most tests
monkeypatch `_download()` itself to write fixed bytes instead of calling the
real API. A separate, explicitly gated live-Drive sandbox test lives in
test_google_drive_live_sandbox.py -- never run by default (see pytest.ini's
`live_drive` marker).

Covers, per the review's own list: query/fields/pagination/shared-folder
support, caching (P0-1's original bug -- cache validity from size alone),
streaming download + retry (exercising the real `_download()`, not the
monkeypatched fake most other tests use), checksums, exports (the current
explicit contract: Workspace docs/folders/shortcuts are skipped, not
exported -- P1-3 territory if that policy ever changes), unsupported files,
errors (a mid-pagination listing failure, and a subsequent retry converging),
and restart (a brand-new instance over the same cache_dir picking up the
persisted manifest, never re-downloading unchanged content)."""

from __future__ import annotations

import pytest

from app.ingestion.sources import GoogleDriveSource


class _FakeExecutable:
    def __init__(self, resp):
        self._resp = resp

    def execute(self):
        if isinstance(self._resp, Exception):
            raise self._resp
        return self._resp


class _FakeFiles:
    def __init__(self, pages):
        self._pages = list(pages)
        self.list_calls = 0
        self.list_kwargs: list[dict] = []

    def list(self, **kwargs):
        self.list_kwargs.append(kwargs)
        resp = self._pages[self.list_calls] if self.list_calls < len(self._pages) else {"files": []}
        self.list_calls += 1
        return _FakeExecutable(resp)

    def get_media(self, fileId):
        return _FakeMediaRequest(fileId)


class _FakeMediaRequest:
    """Stand-in for the request object service.files().get_media() returns --
    real GoogleDriveSource._download() never touches it directly, only passes
    it to MediaIoBaseDownload, which our fakes below intercept instead."""

    def __init__(self, file_id):
        self.file_id = file_id


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


# --- query / fields / pagination / shared-folder contract -----------------

def test_list_query_requests_shared_drive_support_and_correct_fields(tmp_path):
    source, service, _ = _make_source(tmp_path, [{"files": []}], {})
    source.list_files()

    kwargs = service._files.list_kwargs[0]
    assert kwargs["q"] == "'fake-folder' in parents and trashed = false"
    assert kwargs["fields"] == GoogleDriveSource._LIST_FIELDS
    assert kwargs["supportsAllDrives"] is True, "a folder shared from another Drive/Shared Drive must still list"
    assert kwargs["includeItemsFromAllDrives"] is True


def test_pagination_fetches_every_page(tmp_path):
    page1 = {
        "files": [{"id": "f1", "name": "a.pdf", "size": "3", "mimeType": "application/pdf", "md5Checksum": "c1"}],
        "nextPageToken": "TOKEN2",
    }
    page2 = {"files": [{"id": "f2", "name": "b.pdf", "size": "3", "mimeType": "application/pdf", "md5Checksum": "c2"}]}
    source, service, calls = _make_source(tmp_path, [page1, page2], {"f1": b"AAA", "f2": b"BBB"})

    files = source.list_files()

    assert {f.source_ref for f in files} == {"google_drive:f1", "google_drive:f2"}
    assert service._files.list_calls == 2
    assert service._files.list_kwargs[0].get("pageToken") is None
    assert service._files.list_kwargs[1]["pageToken"] == "TOKEN2", (
        "the second page request must carry the token the first page returned"
    )


# --- unsupported items: folders and shortcuts, not just Workspace docs ----

def test_subfolders_and_shortcuts_are_skipped_not_downloaded(tmp_path):
    """list_files() only queries immediate children (P1-3 territory covers
    whether that should ever change), but a subfolder or a shortcut item can
    still appear in that immediate-children listing and must not be treated
    as a downloadable file."""
    pages = [{"files": [
        {"id": "folder1", "name": "Old Manuals", "mimeType": "application/vnd.google-apps.folder"},
        {"id": "shortcut1", "name": "Link to manual", "mimeType": "application/vnd.google-apps.shortcut"},
        {"id": "f1", "name": "manual.pdf", "size": "3", "mimeType": "application/pdf", "md5Checksum": "c1"},
    ]}]
    source, _, calls = _make_source(tmp_path, pages, {"f1": b"AAA"})

    files = source.list_files()

    assert calls == ["f1"]
    assert [f.source_ref for f in files] == ["google_drive:f1"]


# --- streaming download: the real _download(), not the monkeypatched fake -

class _ScriptedDownloader:
    """Stands in for googleapiclient.http.MediaIoBaseDownload so
    GoogleDriveSource._download()'s real retry/streaming/atomic-rename logic
    runs, instead of being monkeypatched away entirely as every other test in
    this file does. `plans[file_id]` is a list consumed one entry per
    constructed downloader (i.e. one entry per _download() attempt): either
    bytes to write and finish on the first next_chunk() call, or an exception
    instance to raise instead."""

    plans: dict[str, list] = {}

    def __init__(self, buf, request):
        self._buf = buf
        self._outcome = self.plans[request.file_id].pop(0)

    def next_chunk(self):
        if isinstance(self._outcome, Exception):
            raise self._outcome
        self._buf.write(self._outcome)
        return None, True


@pytest.fixture
def scripted_download(monkeypatch):
    monkeypatch.setattr("googleapiclient.http.MediaIoBaseDownload", _ScriptedDownloader)
    yield _ScriptedDownloader
    _ScriptedDownloader.plans = {}


def test_download_streams_and_atomically_renames_into_place(tmp_path, scripted_download):
    scripted_download.plans = {"f1": [b"HELLO"]}
    cache_dir = tmp_path / "cache"
    source = GoogleDriveSource(folder_id="x", service_account_path=tmp_path / "key.json", cache_dir=cache_dir)
    cache_path = cache_dir / "f1__manual.pdf"

    source._download(_FakeService([]), "f1", cache_path)

    assert cache_path.read_bytes() == b"HELLO"
    assert list(cache_dir.glob("*.dl*")) == [], "no leftover temp download file after a successful download"


def test_download_retries_a_transient_failure_then_succeeds(tmp_path, scripted_download):
    scripted_download.plans = {"f1": [ConnectionError("transient network error"), b"HELLO"]}
    cache_dir = tmp_path / "cache"
    source = GoogleDriveSource(folder_id="x", service_account_path=tmp_path / "key.json", cache_dir=cache_dir)
    cache_path = cache_dir / "f1__manual.pdf"

    source._download(_FakeService([]), "f1", cache_path)

    assert cache_path.read_bytes() == b"HELLO"


def test_download_raises_after_exhausting_retries_and_leaves_no_partial_file(tmp_path, scripted_download):
    scripted_download.plans = {"f1": [ConnectionError("e1"), ConnectionError("e2"), ConnectionError("e3")]}
    cache_dir = tmp_path / "cache"
    source = GoogleDriveSource(folder_id="x", service_account_path=tmp_path / "key.json", cache_dir=cache_dir)
    cache_path = cache_dir / "f1__manual.pdf"

    with pytest.raises(RuntimeError, match="f1"):
        source._download(_FakeService([]), "f1", cache_path)

    assert not cache_path.exists()
    assert list(cache_dir.glob("*")) == [], "no leftover temp file after exhausting all retry attempts"


# --- errors mid-pagination, and restart/reconciliation --------------------

def test_listing_error_mid_pagination_propagates_and_a_retry_still_converges(tmp_path):
    """Errors + restart, together: a transient failure on page 2 must
    propagate (so ingest_all's caller records a visible failed run --
    test_listing_failure_still_produces_a_visible_failed_run in
    test_pipeline_idempotency.py covers that side), and a subsequent
    successful listing must still converge on the complete, correct file set.

    Documents current (not ideal) behavior along the way: list_files() only
    calls _save_manifest() once, after the whole page loop completes, so a
    file downloaded during a run that later fails on a subsequent page gets
    re-downloaded on the next attempt even though correct bytes are already
    on disk -- wasteful, but not incorrect, since the checksum-keyed cache
    validity check (P0-1) still converges on the right content."""
    page1 = {
        "files": [{"id": "f1", "name": "a.pdf", "size": "3", "mimeType": "application/pdf", "md5Checksum": "c1"}],
        "nextPageToken": "TOKEN2",
    }
    source, service, calls = _make_source(tmp_path, [page1], {"f1": b"AAA"})
    service._files._pages.append(RuntimeError("simulated transient Drive error"))

    with pytest.raises(RuntimeError, match="simulated transient Drive error"):
        source.list_files()
    assert calls == ["f1"], "page 1's file is downloaded before the page-2 failure is hit"

    page2 = {"files": [{"id": "f2", "name": "b.pdf", "size": "3", "mimeType": "application/pdf", "md5Checksum": "c2"}]}
    service._files._pages = [page1, page2]
    service._files.list_calls = 0
    payloads = {"f1": b"AAA", "f2": b"BBB"}
    source._download = lambda service, file_id, cache_path: (calls.append(file_id), cache_path.write_bytes(payloads[file_id]))

    files = source.list_files()

    assert {f.source_ref for f in files} == {"google_drive:f1", "google_drive:f2"}
    assert calls == ["f1", "f1", "f2"], "f1 is re-downloaded on retry since the failed run never persisted its manifest entry"


def test_fresh_instance_after_restart_reuses_the_persisted_manifest(tmp_path):
    """The 'restart' item: a new process (a new GoogleDriveSource instance,
    a new fake service/download) pointed at the same cache_dir must read the
    manifest.json a prior process left on disk and not re-download content
    whose checksum hasn't changed."""
    page = {"files": [{"id": "f1", "name": "manual.pdf", "size": "3", "mimeType": "application/pdf", "md5Checksum": "c1"}]}

    source1, _, calls1 = _make_source(tmp_path, [page], {"f1": b"AAA"})
    source1.list_files()
    assert calls1 == ["f1"]

    source2, _, calls2 = _make_source(tmp_path, [page], {"f1": b"AAA"})
    files = source2.list_files()

    assert calls2 == [], "a restarted process must not re-download a file whose checksum hasn't changed"
    assert files[0].sha256 == __import__("hashlib").sha256(b"AAA").hexdigest()
    assert source2.fetch("google_drive:f1").read_bytes() == b"AAA"

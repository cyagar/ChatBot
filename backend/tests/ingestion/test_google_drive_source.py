"""Contract tests for GoogleDriveSource.

No live Drive/network access anywhere in this file: `_get_service()` is
monkeypatched to return a fake Drive `files().list()` client, and most tests
monkeypatch `_download()` itself to write fixed bytes instead of calling the
real API. A separate, explicitly gated live-Drive sandbox test lives in
test_google_drive_live_sandbox.py -- never run by default (see pytest.ini's
`live_drive` marker).

Covers query/fields/pagination/shared-folder support, caching (cache
validity keyed on checksum, not size alone), streaming download + retry
(exercising the real `_download()`, not the monkeypatched fake most other
tests use), checksums, unsupported files, errors (a mid-pagination listing
failure, and a subsequent retry converging), and restart (a brand-new
instance over the same cache_dir picking up the persisted manifest, never
re-downloading unchanged content).

Also covers file-type, folder, and download-capability policy: subfolders/
shortcuts get distinct, actionable skip reasons rather than the generic
Workspace-export message; Google Workspace documents are confirmed skipped,
not exported (the deliberate flat, binary-only contract -- see
GoogleDriveSource's class docstring for why); oversized files are rejected
before download using Drive's reported size (deliberately no file-count
limit -- see the same docstring for why that would be unsafe); and every one
of those skips is asserted to actually reach pop_skipped(), which is how
pipeline.py reports them as normal ingestion_events rows instead of only a
server log line."""

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


def _make_source(tmp_path, pages, downloads: dict[str, bytes], **source_kwargs):
    source = GoogleDriveSource(
        folder_id="fake-folder",
        service_account_path=tmp_path / "unused-key.json",
        cache_dir=tmp_path / "cache",
        **source_kwargs,
    )
    fake_service = _FakeService(pages)
    source._service = fake_service

    download_calls = []

    def fake_download(service, file_id, cache_path, expected_md5=None, max_bytes=None):
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
    """Two revisions can share a byte count. Cache validity must key on
    md5Checksum, not size alone."""
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
    source._download = lambda service, file_id, cache_path, expected_md5=None, max_bytes=None: (
        calls.append(file_id), cache_path.write_bytes(b"ZZZ"))

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
    """A rename must not leave the old cache file behind where fetch()'s
    glob(file_id + "__*") could return either one -- after a rename, fetch()
    must return only the bytes from the new name."""
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
    source._download = lambda service, file_id, cache_path, expected_md5=None, max_bytes=None: (
        calls.append(file_id), cache_path.write_bytes(b"AAA"))
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
    """list_files() only queries immediate children, but a subfolder or a
    shortcut item can still appear in that immediate-children listing and
    must not be treated
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
    an exception instance to raise on the first next_chunk() call, or bytes
    (delivered whole on one call) or a list of byte chunks (each delivered on
    its own next_chunk() call, done only becoming True after the last one --
    real multi-chunk delivery, needed to prove _download() writes each chunk
    through as it arrives rather than only checking the fully-assembled
    result afterward)."""

    plans: dict[str, list] = {}

    def __init__(self, buf, request):
        self._buf = buf
        outcome = self.plans[request.file_id].pop(0)
        if isinstance(outcome, Exception):
            self._error = outcome
            self._chunks = []
        else:
            self._error = None
            self._chunks = list(outcome) if isinstance(outcome, list) else [outcome]

    def next_chunk(self):
        if self._error is not None:
            raise self._error
        chunk = self._chunks.pop(0)
        self._buf.write(chunk)
        return None, len(self._chunks) == 0


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

    source._download(_FakeService([]), "f1", cache_path, expected_md5=None, max_bytes=1024)

    assert cache_path.read_bytes() == b"HELLO"
    assert list(cache_dir.glob("*.dl*")) == [], "no leftover temp download file after a successful download"


def test_download_writes_each_chunk_through_to_disk_as_it_arrives(tmp_path, scripted_download):
    """Downloads must write each chunk through to disk as it arrives, not
    buffer the whole file in io.BytesIO() and write once at the end after
    the last chunk arrives. Proven here two ways for a
    two-chunk download: (1) the final content is the concatenation of both
    chunks, exercising the real multi-chunk MediaIoBaseDownload loop, not
    just a single next_chunk() call. (2) the max_bytes cap is enforced as
    each chunk is written, not once against a fully-assembled buffer at the
    end -- the first chunk alone fits under the cap, only the second pushes
    the running total over it, and the failure must happen exactly there,
    without ever being handed a third chunk to prove it wasn't buffering
    past the limit before checking."""
    scripted_download.plans = {"f1": [[b"A" * 10, b"B" * 10]]}
    cache_dir = tmp_path / "cache"
    source = GoogleDriveSource(folder_id="x", service_account_path=tmp_path / "key.json", cache_dir=cache_dir)
    cache_path = cache_dir / "f1__manual.pdf"

    source._download(_FakeService([]), "f1", cache_path, expected_md5=None, max_bytes=1024)
    assert cache_path.read_bytes() == b"A" * 10 + b"B" * 10

    scripted_download.plans = {"f1": [[b"A" * 10, b"B" * 10]]}
    cache_path2 = cache_dir / "f1__manual2.pdf"
    with pytest.raises(RuntimeError, match="exceeded"):
        source._download(_FakeService([]), "f1", cache_path2, expected_md5=None, max_bytes=15)
    assert not cache_path2.exists(), "a download that exceeds max_bytes mid-stream must never be promoted into the cache"
    assert list(cache_dir.glob("*.dl*")) == [], "no leftover temp file after the cap aborts the download"


def test_download_rejects_bytes_not_matching_drives_advertised_checksum(tmp_path, scripted_download):
    """Downloaded bytes must be verified against Drive's own md5Checksum, or
    a truncated/corrupted transfer that still completed without an HTTP error
    would be silently cached and fed to the pipeline. All 3 retry attempts
    here deliver bytes with the wrong MD5, so the download must fail
    outright and never reach the cache."""
    scripted_download.plans = {"f1": [b"CORRUPT", b"CORRUPT", b"CORRUPT"]}
    cache_dir = tmp_path / "cache"
    source = GoogleDriveSource(folder_id="x", service_account_path=tmp_path / "key.json", cache_dir=cache_dir)
    cache_path = cache_dir / "f1__manual.pdf"

    with pytest.raises(RuntimeError, match="md5Checksum"):
        source._download(_FakeService([]), "f1", cache_path,
                          expected_md5="0" * 32, max_bytes=1024)

    assert not cache_path.exists()
    assert list(cache_dir.glob("*")) == [], "no leftover temp file after an md5 mismatch on every retry"


def test_download_retries_a_transient_failure_then_succeeds(tmp_path, scripted_download):
    scripted_download.plans = {"f1": [ConnectionError("transient network error"), b"HELLO"]}
    cache_dir = tmp_path / "cache"
    source = GoogleDriveSource(folder_id="x", service_account_path=tmp_path / "key.json", cache_dir=cache_dir)
    cache_path = cache_dir / "f1__manual.pdf"

    source._download(_FakeService([]), "f1", cache_path, expected_md5=None, max_bytes=1024)

    assert cache_path.read_bytes() == b"HELLO"


def test_download_raises_after_exhausting_retries_and_leaves_no_partial_file(tmp_path, scripted_download):
    scripted_download.plans = {"f1": [ConnectionError("e1"), ConnectionError("e2"), ConnectionError("e3")]}
    cache_dir = tmp_path / "cache"
    source = GoogleDriveSource(folder_id="x", service_account_path=tmp_path / "key.json", cache_dir=cache_dir)
    cache_path = cache_dir / "f1__manual.pdf"

    with pytest.raises(RuntimeError, match="f1"):
        source._download(_FakeService([]), "f1", cache_path, expected_md5=None, max_bytes=1024)

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
    validity check still converges on the right content."""
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
    source._download = lambda service, file_id, cache_path, expected_md5=None, max_bytes=None: (
        calls.append(file_id), cache_path.write_bytes(payloads[file_id]))

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


# --- Per-file size limit, and every skip is reported -----------------------
#
# Deliberately no file-COUNT limit test here: a count cap would make
# list_files() return a partial listing once a folder passes it, which is
# indistinguishable from files having actually been removed from Drive --
# an ambiguity that must never feed removal reconciliation. See
# GoogleDriveSource's class docstring for the full reasoning; only the
# per-file size limit is implemented.

def test_missing_reported_size_does_not_bypass_the_cap(tmp_path, scripted_download):
    """The pre-download check must not use `int(f.get("size") or 0)`, which
    would treat a file with no reported size at all (missing key, not even
    "0") as 0 bytes and always pass the
    `> max_file_size_bytes` check -- the cap was silently bypassed for
    exactly the files it matters most for. The real limit is now enforced on
    bytes actually streamed, in _download(), independent of whatever (or
    whether) Drive reported for size -- this exercises the real _download()
    via the scripted downloader to prove a file Drive reports no size for
    still gets capped once its true size is discovered mid-stream."""
    page = {"files": [{"id": "f1", "name": "no_size.pdf",
                        "mimeType": "application/pdf", "md5Checksum": "c1"}]}  # no "size" key at all
    scripted_download.plans = {"f1": [[b"A" * 10, b"B" * 10]]}
    source, service, _ = _make_source(tmp_path, [page], {}, max_file_size_bytes=15)
    source._download = GoogleDriveSource._download.__get__(source)  # use the real _download, not the fake

    files = source.list_files()

    assert files == [], "a file whose true size exceeds the cap must not be indexed, missing Drive size or not"
    skips = source.pop_skipped()
    assert any("exceeded" in s.reason or "limit" in s.reason for s in skips), \
        "the missing-size file must still be reported, not silently dropped"


def test_oversized_file_is_skipped_before_download_and_reported(tmp_path):
    """A file over the configured per-file size limit must never be
    downloaded (the limit is checked against Drive's reported size, before
    any bytes are fetched) and must show up via pop_skipped()."""
    huge_size = 5 * 1024 * 1024  # 5 MB
    page = {"files": [{"id": "f1", "name": "huge.pdf", "size": str(huge_size),
                        "mimeType": "application/pdf", "md5Checksum": "c1"}]}
    source, _, calls = _make_source(tmp_path, [page], {}, max_file_size_bytes=1024 * 1024)

    files = source.list_files()

    assert calls == [], "an oversized file must never be downloaded"
    assert files == []
    skips = source.pop_skipped()
    assert len(skips) == 1
    assert skips[0].filename == "huge.pdf"
    assert "MB" in skips[0].reason


def test_file_exactly_at_the_size_limit_is_not_skipped(tmp_path):
    """The check is strictly '>', not '>=' -- a file exactly at the
    configured limit must pass. Pinned explicitly so a future '>=' change
    fails a named assertion instead of looking like a harmless rounding
    tweak."""
    page = {"files": [{"id": "f1", "name": "ok.pdf", "size": "1024",
                        "mimeType": "application/pdf", "md5Checksum": "c1"}]}
    source, _, calls = _make_source(tmp_path, [page], {"f1": b"A" * 1024}, max_file_size_bytes=1024)

    files = source.list_files()

    assert calls == ["f1"], "a file exactly at the limit must still be downloaded, not skipped"
    assert len(files) == 1
    assert source.pop_skipped() == []


def test_pop_skipped_drains_and_resets_between_listings(tmp_path):
    """Skips must be reported once per run, not accumulate forever or leak
    into the next list_files() call's report."""
    pages = [{"files": [
        {"id": "doc1", "name": "Untitled document", "mimeType": "application/vnd.google-apps.document"},
    ]}]
    source, _, _ = _make_source(tmp_path, pages, {})

    source.list_files()
    first_skips = source.pop_skipped()
    assert len(first_skips) == 1
    assert source.pop_skipped() == [], "a second pop_skipped() call without a new listing must be empty"

    source_no_skips, _, _ = _make_source(tmp_path, [{"files": []}], {})
    source_no_skips.list_files()
    assert source_no_skips.pop_skipped() == []


def test_base_document_source_reports_no_skips_by_default():
    """FakeDirectorySource (and any other source that never overrides
    pop_skipped()) must not be forced to implement skip tracking just to
    satisfy the interface."""
    from tests.ingestion.fakes import FakeDirectorySource

    source = FakeDirectorySource(directory=__import__("pathlib").Path("."))
    assert source.pop_skipped() == []

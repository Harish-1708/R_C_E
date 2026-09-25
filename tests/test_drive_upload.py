"""
Tests for drive_upload.py against a fake Drive API client -- this
sandbox has no real Google Drive credentials, so build_drive_service()
and upload_file()'s own real network call are untested here; see
README "Testing the Drive backfill" for the required manual smoke test
once real credentials exist. find_existing_upload()'s own logic --
what it searches for, when it matches -- is fully testable against a
fake that mimics the real client's shape.

CONFIRMED REAL, direct decision from live evidence: this module went
back to uploading files itself after a long stretch using Refunnel's
own native "Save to Drive" feature. Every piece of that click-through
was eventually proven correct -- confirmed byte-identical to a version
that once worked, confirmed against the right Drive folder, confirmed
accepted by Refunnel's own "Uploading to Google Drive -- it will
appear shortly" toast -- and the file still never landed, across many
separate runs, on a transfer that happens entirely on Refunnel's own
servers once that toast appears. find_and_rename_uploaded_file and
rename_file (tested here in an earlier version of this file) existed
only for that flow -- finding a file REFUNNEL created under a raw name
and renaming it after the fact. Neither is needed anymore: upload_file
sets the correct name at upload time, since the upload is ours now.
"""
import pytest

from drive_upload import find_existing_upload, upload_file


class _FakeFilesResource:
    def __init__(self, list_responses=None, create_response=None):
        # a list of "files" lists, one per successive .list() call
        self._list_responses = list(list_responses or [])
        self._create_response = create_response
        self.list_calls = []
        self.create_calls = []

    def list(self, q, fields, orderBy, pageSize):
        self.list_calls.append(q)
        response = self._list_responses.pop(0) if self._list_responses else []
        return _ExecConst({"files": response})

    def create(self, body, media_body, fields):
        self.create_calls.append((body, media_body))
        return _ExecConst(self._create_response or {"id": "new_file_id"})


class _ExecConst:
    def __init__(self, value):
        self._value = value

    def execute(self):
        return self._value


class _FakeDriveService:
    def __init__(self, list_responses=None, create_response=None):
        self._files = _FakeFilesResource(list_responses, create_response)

    def files(self):
        return self._files


# ---------- find_existing_upload (duplicate-prevention safety net for a crashed prior run) ----------
#
# CONFIRMED REAL gap this covers: a run that downloads and uploads a
# video successfully but crashes before marking drive_uploaded_at in
# the sheet would otherwise download and upload that SAME media_id
# again on the next run -- a genuine duplicate file. The final
# filename already embeds media_id (parse_refunnel.build_drive_filename),
# so checking for it directly, before ever downloading anything, lets
# a caller catch that case and just mark the sheet instead.

def test_find_existing_upload_returns_none_when_nothing_matches():
    service = _FakeDriveService(list_responses=[[]])
    result = find_existing_upload(service, "folder1", "tk_12345678")
    assert result is None


def test_find_existing_upload_finds_a_file_from_an_earlier_crashed_run():
    service = _FakeDriveService(list_responses=[
        [{"id": "file_1", "name": "Swoveralls | @user | tk_12345678.mp4", "createdTime": "2026-09-01T00:00:00Z"}],
    ])
    result = find_existing_upload(service, "folder1", "tk_12345678")
    assert result == {"id": "file_1", "name": "Swoveralls | @user | tk_12345678.mp4",
                      "createdTime": "2026-09-01T00:00:00Z"}


def test_find_existing_upload_never_waits_or_polls():
    # a single .list() call, no retry loop -- confirmed real: this is
    # meant to be a cheap check run before every download, not a slow poll
    service = _FakeDriveService(list_responses=[[]])
    find_existing_upload(service, "folder1", "tk_12345678")
    assert len(service._files.list_calls) == 1


def test_find_existing_upload_search_query_has_no_createdtime_filter():
    # deliberately unscoped by time -- it's looking for something that
    # may have been uploaded on a PREVIOUS run, possibly hours ago
    service = _FakeDriveService(list_responses=[[]])
    find_existing_upload(service, "folder1", "tk_12345678")
    query = service._files.list_calls[0]
    assert "createdTime" not in query
    assert "folder1" in query and "tk_12345678" in query


def test_sabotage_a_crashed_runs_upload_getting_duplicated_would_be_caught():
    # models the full, correct caller flow: check first, mark the
    # sheet if found, only download+upload if genuinely nothing exists
    service = _FakeDriveService(list_responses=[
        [{"id": "file_1", "name": "Swoveralls | @user | tk_1.mp4", "createdTime": "2026-09-01T00:00:00Z"}],
    ])
    uploaded_again = []

    def caller_flow():
        existing = find_existing_upload(service, "folder1", "tk_1")
        if existing is not None:
            return  # already there -- just mark the sheet, don't touch Drive again
        uploaded_again.append("uploaded")  # would mean a genuine duplicate file

    caller_flow()
    with pytest.raises(AssertionError):
        assert uploaded_again == ["uploaded"]  # wrong -- that's the duplicate-upload bug
    assert uploaded_again == []


# ---------- upload_file (restored: this module owns the upload itself again) ----------

def test_upload_file_creates_with_the_given_name_and_folder(tmp_path):
    local = tmp_path / "tk_1.mp4"
    local.write_bytes(b"fake video bytes")
    service = _FakeDriveService(create_response={"id": "new_file_id"})

    file_id = upload_file(service, str(local), "Swoveralls | @user | tk_1.mp4", "folder1")

    assert file_id == "new_file_id"
    assert len(service._files.create_calls) == 1
    body, media = service._files.create_calls[0]
    assert body == {"name": "Swoveralls | @user | tk_1.mp4", "parents": ["folder1"]}


def test_upload_file_uses_the_given_filename_not_the_local_files_own_name(tmp_path):
    # CONFIRMED REAL reason this matters: media_id-based temp filenames
    # from the download step are never what should show up in Drive --
    # the final Brand/handle/id convention is applied here, at upload
    # time, not the local file's own on-disk name.
    local = tmp_path / "tk_1.mp4"
    local.write_bytes(b"fake video bytes")
    service = _FakeDriveService(create_response={"id": "new_file_id"})

    upload_file(service, str(local), "Swoveralls | @realname | tk_1.mp4", "folder1")

    body, _media = service._files.create_calls[0]
    assert body["name"] == "Swoveralls | @realname | tk_1.mp4"


def test_sabotage_uploading_with_the_local_temp_name_would_be_caught(tmp_path):
    local = tmp_path / "tk_1.mp4"
    local.write_bytes(b"fake video bytes")
    service = _FakeDriveService(create_response={"id": "new_file_id"})

    upload_file(service, str(local), "Swoveralls | @realname | tk_1.mp4", "folder1")

    body, _media = service._files.create_calls[0]
    with pytest.raises(AssertionError):
        assert body["name"] == "tk_1.mp4"  # wrong -- that's the local temp name, not the real one
    assert body["name"] == "Swoveralls | @realname | tk_1.mp4"

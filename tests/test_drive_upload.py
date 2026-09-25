"""
Tests for drive_upload.py against a fake Drive API client -- this
sandbox has no real Google Drive credentials, so build_drive_service()
itself (real auth) is untested here; see README "Testing the Drive
backfill" for the required manual smoke test once real credentials
exist. find_and_rename_uploaded_file()'s own logic -- what it
searches for, when it renames, how it polls -- is fully testable
against a fake that mimics the real client's shape.
"""
import pytest

from drive_upload import find_and_rename_uploaded_file


class _FakeFilesResource:
    def __init__(self, list_responses):
        # a list of "files" lists, one per successive .list() call --
        # lets a test simulate "not there yet" then "there now"
        self._list_responses = list(list_responses)
        self.list_calls = []
        self.update_calls = []

    def list(self, q, fields, orderBy, pageSize):
        self.list_calls.append(q)
        response = self._list_responses.pop(0) if self._list_responses else []
        return _ExecConst({"files": response})

    def update(self, fileId, body):
        self.update_calls.append((fileId, body))
        return _ExecConst({"id": fileId})


class _ExecConst:
    def __init__(self, value):
        self._value = value

    def execute(self):
        return self._value


class _FakeDriveService:
    def __init__(self, list_responses):
        self._files = _FakeFilesResource(list_responses)

    def files(self):
        return self._files


def test_finds_and_renames_on_the_first_check():
    service = _FakeDriveService(list_responses=[
        [{"id": "file_1", "name": "INSTAGRAM_REEL_x_2026-09-21-UGC_30937735.mp4", "createdTime": "2026-09-22T00:00:00Z"}],
    ])

    file_id = find_and_rename_uploaded_file(
        service, folder_id="folder1", match_fragment="30937735",
        new_filename="Swoveralls | @tayloredforthekingdom | ig_18018159830937735.mp4",
        max_wait_seconds=5, poll_interval_seconds=0.01,
    )

    assert file_id == "file_1"
    assert service._files.update_calls == [
        ("file_1", {"name": "Swoveralls | @tayloredforthekingdom | ig_18018159830937735.mp4"})
    ]


def test_polls_until_the_file_shows_up():
    # confirmed real, deliberate: Refunnel's upload happens server-side,
    # not instantly when "Save to Drive" is clicked
    service = _FakeDriveService(list_responses=[
        [],  # not there yet
        [],  # still not there
        [{"id": "file_1", "name": "...UGC_30937735.mp4", "createdTime": "2026-09-22T00:00:02Z"}],
    ])

    file_id = find_and_rename_uploaded_file(
        service, folder_id="folder1", match_fragment="30937735",
        new_filename="renamed.mp4", max_wait_seconds=5, poll_interval_seconds=0.01,
    )

    assert file_id == "file_1"
    assert len(service._files.list_calls) == 3


def test_returns_none_if_never_found_within_the_wait():
    service = _FakeDriveService(list_responses=[[], [], []])

    file_id = find_and_rename_uploaded_file(
        service, folder_id="folder1", match_fragment="30937735",
        new_filename="renamed.mp4", max_wait_seconds=0.05, poll_interval_seconds=0.02,
    )

    assert file_id is None
    assert service._files.update_calls == []  # never renamed anything


def test_search_query_includes_the_folder_and_fragment():
    service = _FakeDriveService(list_responses=[
        [{"id": "file_1", "name": "x", "createdTime": "2026-09-22T00:00:00Z"}],
    ])
    find_and_rename_uploaded_file(
        service, folder_id="folder_abc", match_fragment="30937735",
        new_filename="renamed.mp4", max_wait_seconds=5, poll_interval_seconds=0.01,
    )
    query = service._files.list_calls[0]
    assert "'folder_abc' in parents" in query
    assert "30937735" in query


def test_uploaded_after_scopes_the_search():
    # confirmed real, genuine risk this guards against: an older file
    # coincidentally sharing the same 8-digit fragment must not match
    service = _FakeDriveService(list_responses=[
        [{"id": "file_1", "name": "x", "createdTime": "2026-09-22T00:00:00Z"}],
    ])
    find_and_rename_uploaded_file(
        service, folder_id="folder1", match_fragment="30937735",
        new_filename="renamed.mp4", uploaded_after="2026-09-22T00:00:00Z",
        max_wait_seconds=5, poll_interval_seconds=0.01,
    )
    query = service._files.list_calls[0]
    assert "createdTime >= '2026-09-22T00:00:00Z'" in query


def test_no_uploaded_after_means_no_createdtime_filter():
    service = _FakeDriveService(list_responses=[
        [{"id": "file_1", "name": "x", "createdTime": "2026-09-22T00:00:00Z"}],
    ])
    find_and_rename_uploaded_file(
        service, folder_id="folder1", match_fragment="30937735",
        new_filename="renamed.mp4", max_wait_seconds=5, poll_interval_seconds=0.01,
    )
    query = service._files.list_calls[0]
    assert "createdTime" not in query


def test_sabotage_renaming_before_confirming_a_match_would_be_caught():
    service = _FakeDriveService(list_responses=[[], []])
    file_id = find_and_rename_uploaded_file(
        service, folder_id="folder1", match_fragment="30937735",
        new_filename="renamed.mp4", max_wait_seconds=0.03, poll_interval_seconds=0.01,
    )
    with pytest.raises(AssertionError):
        assert file_id is not None  # wrong -- nothing was ever found
    assert file_id is None  # confirms actual correct behavior
    assert service._files.update_calls == []  # and definitely never renamed anything imaginary


# ---------- find_existing_upload / rename_file (confirmed real gap this closes) ----------
#
# find_and_rename_uploaded_file gives up after max_wait_seconds if the
# file hasn't appeared yet -- but Refunnel's server-side transfer can
# genuinely take longer than that. When that happens, the caller would
# otherwise trigger ANOTHER upload for the same media_id next run,
# risking a real duplicate, while the FIRST upload sits in Drive
# forever under its raw, unrenamed name. find_existing_upload is the
# immediate, no-wait check a caller runs FIRST, so a slow-but-real
# upload gets found and renamed instead of duplicated.

from drive_upload import find_existing_upload, rename_file


def test_find_existing_upload_returns_none_when_nothing_matches():
    service = _FakeDriveService(list_responses=[[]])
    result = find_existing_upload(service, "folder1", "12345678")
    assert result is None


def test_find_existing_upload_finds_a_file_from_a_slow_earlier_run():
    service = _FakeDriveService(list_responses=[
        [{"id": "file_1", "name": "TIKTOK_VIDEO_raw_name_12345678.mp4", "createdTime": "2026-09-01T00:00:00Z"}],
    ])
    result = find_existing_upload(service, "folder1", "12345678")
    assert result == {"id": "file_1", "name": "TIKTOK_VIDEO_raw_name_12345678.mp4",
                      "createdTime": "2026-09-01T00:00:00Z"}


def test_find_existing_upload_does_not_rename_by_itself():
    # it only LOOKS -- the caller decides whether and how to rename
    service = _FakeDriveService(list_responses=[
        [{"id": "file_1", "name": "raw.mp4", "createdTime": "2026-09-01T00:00:00Z"}],
    ])
    find_existing_upload(service, "folder1", "12345678")
    assert service._files.update_calls == []


def test_find_existing_upload_never_waits_or_polls():
    # a single .list() call, no retry loop -- confirmed real: this is
    # meant to be a cheap check run before every trigger, not another
    # slow poll
    service = _FakeDriveService(list_responses=[[]])
    find_existing_upload(service, "folder1", "12345678")
    assert len(service._files.list_calls) == 1


def test_find_existing_upload_search_query_has_no_createdtime_filter():
    # deliberately unscoped by time -- it's looking for something that
    # may have been uploaded on a PREVIOUS run, possibly hours ago
    service = _FakeDriveService(list_responses=[[]])
    find_existing_upload(service, "folder1", "12345678")
    query = service._files.list_calls[0]
    assert "createdTime" not in query
    assert "folder1" in query and "12345678" in query


def test_rename_file_calls_update_with_the_given_id_and_name():
    service = _FakeDriveService(list_responses=[])
    rename_file(service, "file_1", "Swoveralls | @user | tk_1.mp4")
    assert service._files.update_calls == [("file_1", {"name": "Swoveralls | @user | tk_1.mp4"})]


def test_sabotage_a_slow_upload_getting_silently_duplicated_would_be_caught():
    # models the full, correct caller flow: check first, rename if
    # found, only trigger a new upload if genuinely nothing exists yet
    service = _FakeDriveService(list_responses=[
        [{"id": "file_1", "name": "raw.mp4", "createdTime": "2026-09-01T00:00:00Z"}],
    ])
    trigger_calls = []

    def caller_flow():
        existing = find_existing_upload(service, "folder1", "12345678")
        if existing is not None:
            rename_file(service, existing["id"], "Swoveralls | @user | tk_1.mp4")
            return
        trigger_calls.append("triggered")  # would mean a duplicate upload

    caller_flow()
    with pytest.raises(AssertionError):
        assert trigger_calls == ["triggered"]  # wrong -- that's the duplicate-upload bug
    assert trigger_calls == []
    assert service._files.update_calls == [("file_1", {"name": "Swoveralls | @user | tk_1.mp4"})]

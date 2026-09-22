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

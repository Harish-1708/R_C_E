"""
Tests for drive_upload.py against a fake Drive API client -- this
sandbox has no real Google Drive credentials, so build_drive_service()
itself (real auth) is untested here; see README "Testing the Drive
backfill" for the required manual smoke test once real credentials
exist. upload_file()'s own logic -- what it sends, what it returns --
is fully testable against a fake that mimics the real client's shape.
"""
import pytest

from drive_upload import upload_file


class _FakeFilesResource:
    def __init__(self):
        self.created_with = None

    def create(self, body, media_body, fields):
        self.created_with = {"body": body, "media_body": media_body, "fields": fields}
        return self

    def execute(self):
        return {"id": "fake_drive_file_id_123"}


class _FakeDriveService:
    def __init__(self):
        self._files = _FakeFilesResource()

    def files(self):
        return self._files


def test_upload_file_sends_the_exact_filename_not_the_local_path(tmp_path):
    local_file = tmp_path / "tk_7660267483391151373_download.mp4"
    local_file.write_bytes(b"fake video bytes")
    service = _FakeDriveService()

    file_id = upload_file(
        service, str(local_file),
        filename="Swoveralls | @ayorobynbanks | tk_7660267483391151373.mp4",
        folder_id="folder_abc",
    )

    assert file_id == "fake_drive_file_id_123"
    sent_body = service._files.created_with["body"]
    assert sent_body["name"] == "Swoveralls | @ayorobynbanks | tk_7660267483391151373.mp4"
    assert sent_body["parents"] == ["folder_abc"]


def test_upload_file_returns_the_real_drive_file_id(tmp_path):
    local_file = tmp_path / "video.mp4"
    local_file.write_bytes(b"x")
    service = _FakeDriveService()
    file_id = upload_file(service, str(local_file), "name.mp4", "folder_1")
    assert file_id == "fake_drive_file_id_123"


def test_sabotage_uploading_the_local_temp_name_would_be_caught(tmp_path):
    # the exact real risk: uploading under the download's own temp
    # filename instead of the agreed Brand | @handle | id.mp4 name
    local_file = tmp_path / "download_tmp_98213.mp4"
    local_file.write_bytes(b"x")
    service = _FakeDriveService()

    upload_file(service, str(local_file), filename="Swoveralls | @alice | tk_1.mp4", folder_id="f1")

    sent_name = service._files.created_with["body"]["name"]
    with pytest.raises(AssertionError):
        assert sent_name == "download_tmp_98213.mp4"  # wrong -- must not use the local temp name
    assert sent_name == "Swoveralls | @alice | tk_1.mp4"  # confirms actual correct behavior

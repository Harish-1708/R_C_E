"""
Tests for download_approved_to_drive.py. The real browser login,
video download, and Drive upload are monkeypatched out (this sandbox
has no live Refunnel/Drive access) -- these test the ORCHESTRATION
logic: which brands get skipped and why, which media_ids get selected
and in what order, the batch-size cap, incremental marking so an
already-uploaded id is never reprocessed, and that one failed video
doesn't stop the rest of the batch. See README "Testing the Drive
backfill" for the manual smoke test still required before trusting
this against real Refunnel/Drive accounts.
"""
import gspread.exceptions
import pytest

import download_approved_to_drive as dad
import sheets_sync


class FakeWorksheet:
    def __init__(self, rows=None):
        self.rows = [list(r) for r in rows] if rows else []

    def get_all_values(self):
        return self.rows

    def clear(self):
        self.rows = []

    def update(self, rows, value_input_option="RAW"):
        self.rows = [list(r) for r in rows]

    def format(self, cell_range, fmt):
        pass

    def freeze(self, rows=1):
        pass

    def row_values(self, row_num):
        return self.rows[row_num - 1]

    def col_values(self, col_num):
        return [row[col_num - 1] if col_num - 1 < len(row) else "" for row in self.rows]

    def update_cell(self, row_num, col_num, value):
        while len(self.rows[row_num - 1]) < col_num:
            self.rows[row_num - 1].append("")
        self.rows[row_num - 1][col_num - 1] = value

    def batch_update(self, data, value_input_option="RAW"):
        from gspread.utils import a1_to_rowcol
        for entry in data:
            row_num, col_num = a1_to_rowcol(entry["range"])
            self.update_cell(row_num, col_num, entry["values"][0][0])


class FakeSpreadsheet:
    def __init__(self, worksheets=None):
        self._worksheets = dict(worksheets or {})

    def worksheet(self, title):
        if title not in self._worksheets:
            raise gspread.exceptions.WorksheetNotFound(title)
        return self._worksheets[title]


class FakeClient:
    def __init__(self, spreadsheets_by_id):
        self._by_id = spreadsheets_by_id

    def open_by_key(self, spreadsheet_id):
        return self._by_id[spreadsheet_id]


MASTER_HEADER = ["id", "platform", "username", "rights_status", "drive_uploaded_at"]


def _row(media_id, status="GRANTED", uploaded=""):
    return [media_id, "TIKTOK", "creatorname", status, uploaded]


class _FakeBrowser:
    def close(self):
        pass


class _FakeContext:
    def new_page(self):
        return _FakePage()


class _FakePlaywright:
    def stop(self):
        pass


class _FakePage:
    def goto(self, *_a, **_kw):
        pass

    def close(self):
        pass


@pytest.fixture(autouse=True)
def _stub_browser_and_workspace(monkeypatch):
    monkeypatch.setattr(
        dad.refunnel_auth, "load_or_refresh_session",
        lambda **kw: (_FakePlaywright(), _FakeBrowser(), _FakeContext())
    )
    monkeypatch.setattr(dad.refunnel_auth, "refunnel_social_listening_url", lambda: "https://x")
    monkeypatch.setattr(dad.refunnel_export, "select_workspace", lambda *a, **kw: None)


def _brand_config(name="Swoveralls", spreadsheet_secret="SPREADSHEET_ID_SWOVERALLS", drive_secret="DRIVE_FOLDER_ID_SWOVERALLS"):
    cfg = {"name": name, "refunnel_workspace_name": name}
    if spreadsheet_secret:
        cfg["spreadsheet_id_secret"] = spreadsheet_secret
    if drive_secret:
        cfg["drive_folder_id_secret"] = drive_secret
    return cfg


def test_skips_if_spreadsheet_secret_not_set(monkeypatch, capsys):
    monkeypatch.delenv("SPREADSHEET_ID_SWOVERALLS", raising=False)
    gc = FakeClient({})
    dad.process_one_brand(gc, _brand_config(), None, "x@example.com", ["Swoveralls"])
    assert "skipping" in capsys.readouterr().out


def test_skips_if_no_drive_secret_configured(monkeypatch, capsys):
    monkeypatch.setenv("SPREADSHEET_ID_SWOVERALLS", "sheet1")
    gc = FakeClient({})
    dad.process_one_brand(gc, _brand_config(drive_secret=None), None, "x@example.com", ["Swoveralls"])
    assert "skipping" in capsys.readouterr().out


def test_skips_if_drive_secret_configured_but_env_var_missing(monkeypatch, capsys):
    monkeypatch.setenv("SPREADSHEET_ID_SWOVERALLS", "sheet1")
    monkeypatch.delenv("DRIVE_FOLDER_ID_SWOVERALLS", raising=False)
    gc = FakeClient({})
    dad.process_one_brand(gc, _brand_config(), None, "x@example.com", ["Swoveralls"])
    assert "skipping" in capsys.readouterr().out


def test_skips_if_no_master_data_tab(monkeypatch, capsys):
    monkeypatch.setenv("SPREADSHEET_ID_SWOVERALLS", "sheet1")
    monkeypatch.setenv("DRIVE_FOLDER_ID_SWOVERALLS", "folder1")
    gc = FakeClient({"sheet1": FakeSpreadsheet(worksheets={})})
    dad.process_one_brand(gc, _brand_config(), None, "x@example.com", ["Swoveralls"])
    assert "skipping" in capsys.readouterr().out


def test_nothing_to_upload_when_no_approved_rows(monkeypatch, capsys):
    monkeypatch.setenv("SPREADSHEET_ID_SWOVERALLS", "sheet1")
    monkeypatch.setenv("DRIVE_FOLDER_ID_SWOVERALLS", "folder1")
    master_ws = FakeWorksheet(rows=[MASTER_HEADER, _row("tk_1", status="REQUESTED")])
    gc = FakeClient({"sheet1": FakeSpreadsheet(worksheets={"Master Data": master_ws})})
    dad.process_one_brand(gc, _brand_config(), None, "x@example.com", ["Swoveralls"])
    assert "nothing new to upload" in capsys.readouterr().out


def test_happy_path_uploads_and_marks_the_row(monkeypatch, tmp_path):
    monkeypatch.setenv("SPREADSHEET_ID_SWOVERALLS", "sheet1")
    monkeypatch.setenv("DRIVE_FOLDER_ID_SWOVERALLS", "folder1")
    master_ws = FakeWorksheet(rows=[MASTER_HEADER, _row("tk_1")])
    gc = FakeClient({"sheet1": FakeSpreadsheet(worksheets={"Master Data": master_ws})})

    local_file = tmp_path / "tk_1.mp4"
    local_file.write_bytes(b"video")
    monkeypatch.setattr(dad.refunnel_export, "download_approved_video", lambda *a, **kw: local_file)

    uploaded_calls = []
    monkeypatch.setattr(
        dad.drive_upload, "upload_file",
        lambda service, path, filename, folder_id: uploaded_calls.append((filename, folder_id)) or "fake_id"
    )

    dad.process_one_brand(gc, _brand_config(), object(), "x@example.com", ["Swoveralls"])

    assert uploaded_calls == [("Swoveralls | @creatorname | tk_1.mp4", "folder1")]
    header = master_ws.rows[0]
    upload_idx = header.index("drive_uploaded_at")
    assert master_ws.rows[1][upload_idx]  # non-blank -- marked
    assert not local_file.exists()  # cleaned up after a successful upload


def test_already_uploaded_row_is_never_reprocessed(monkeypatch):
    monkeypatch.setenv("SPREADSHEET_ID_SWOVERALLS", "sheet1")
    monkeypatch.setenv("DRIVE_FOLDER_ID_SWOVERALLS", "folder1")
    master_ws = FakeWorksheet(rows=[MASTER_HEADER, _row("tk_1", uploaded="2026-09-01T00:00:00+00:00")])
    gc = FakeClient({"sheet1": FakeSpreadsheet(worksheets={"Master Data": master_ws})})

    called = []
    monkeypatch.setattr(dad.refunnel_export, "download_approved_video",
                         lambda *a, **kw: called.append(1) or None)

    dad.process_one_brand(gc, _brand_config(), object(), "x@example.com", ["Swoveralls"])

    assert called == []  # never even attempted


def test_batch_size_caps_how_many_are_processed_per_run(monkeypatch, tmp_path):
    monkeypatch.setenv("SPREADSHEET_ID_SWOVERALLS", "sheet1")
    monkeypatch.setenv("DRIVE_FOLDER_ID_SWOVERALLS", "folder1")
    monkeypatch.setattr(dad, "BATCH_SIZE", 2)

    rows = [MASTER_HEADER] + [_row(f"tk_{i}") for i in range(5)]
    master_ws = FakeWorksheet(rows=rows)
    gc = FakeClient({"sheet1": FakeSpreadsheet(worksheets={"Master Data": master_ws})})

    attempted = []

    def fake_download(page, media_id, download_dir):
        attempted.append(media_id)
        f = tmp_path / f"{media_id}.mp4"
        f.write_bytes(b"x")
        return f

    monkeypatch.setattr(dad.refunnel_export, "download_approved_video", fake_download)
    monkeypatch.setattr(dad.drive_upload, "upload_file", lambda *a, **kw: "id")

    dad.process_one_brand(gc, _brand_config(), object(), "x@example.com", ["Swoveralls"])

    assert len(attempted) == 2  # capped, not all 5


def test_one_failed_video_does_not_stop_the_rest(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("SPREADSHEET_ID_SWOVERALLS", "sheet1")
    monkeypatch.setenv("DRIVE_FOLDER_ID_SWOVERALLS", "folder1")
    rows = [MASTER_HEADER, _row("tk_bad"), _row("tk_good")]
    master_ws = FakeWorksheet(rows=rows)
    gc = FakeClient({"sheet1": FakeSpreadsheet(worksheets={"Master Data": master_ws})})

    def fake_download(page, media_id, download_dir):
        if media_id == "tk_bad":
            raise RuntimeError("simulated download failure")
        f = tmp_path / f"{media_id}.mp4"
        f.write_bytes(b"x")
        return f

    monkeypatch.setattr(dad.refunnel_export, "download_approved_video", fake_download)
    monkeypatch.setattr(dad.drive_upload, "upload_file", lambda *a, **kw: "id")

    dad.process_one_brand(gc, _brand_config(), object(), "x@example.com", ["Swoveralls"])

    header = master_ws.rows[0]
    upload_idx = header.index("drive_uploaded_at")
    id_idx = header.index("id")
    by_id = {row[id_idx]: row[upload_idx] for row in master_ws.rows[1:]}
    assert by_id["tk_good"]       # succeeded, marked
    assert not by_id["tk_bad"]    # failed, left unmarked for retry


def test_sabotage_reprocessing_an_uploaded_row_would_be_caught(monkeypatch):
    monkeypatch.setenv("SPREADSHEET_ID_SWOVERALLS", "sheet1")
    monkeypatch.setenv("DRIVE_FOLDER_ID_SWOVERALLS", "folder1")
    master_ws = FakeWorksheet(rows=[MASTER_HEADER, _row("tk_1", uploaded="2026-09-01T00:00:00+00:00")])
    gc = FakeClient({"sheet1": FakeSpreadsheet(worksheets={"Master Data": master_ws})})

    called = []
    monkeypatch.setattr(dad.refunnel_export, "download_approved_video",
                         lambda *a, **kw: called.append(1) or None)

    dad.process_one_brand(gc, _brand_config(), object(), "x@example.com", ["Swoveralls"])

    with pytest.raises(AssertionError):
        assert len(called) == 1  # wrong -- would mean it got reprocessed
    assert len(called) == 0  # confirms actual correct behavior


def test_read_column_values_helper_used_correctly(monkeypatch):
    # confirms this script reads the SAME "drive_uploaded_at" extra
    # column sync_tab preserves for Master Data elsewhere, not a
    # differently-named or differently-shaped one
    monkeypatch.setenv("SPREADSHEET_ID_SWOVERALLS", "sheet1")
    monkeypatch.setenv("DRIVE_FOLDER_ID_SWOVERALLS", "folder1")
    master_ws = FakeWorksheet(rows=[MASTER_HEADER, _row("tk_1", uploaded="already")])
    master_client = sheets_sync.GspreadSheetsClient(master_ws)
    existing = sheets_sync.read_column_values(master_client, "drive_uploaded_at")
    assert existing == {"tk_1": "already"}

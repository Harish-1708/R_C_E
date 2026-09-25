"""
Tests for download_approved_to_drive.py. Real browser login and the
real Drive API are monkeypatched out (this sandbox has no live
Refunnel/Drive access) -- these test the ORCHESTRATION logic: which
brands get skipped and why, which media_ids get selected and in what
order, the batch-size cap, incremental marking so an already-uploaded
id is never reprocessed, that one failed video doesn't stop the rest
of the batch, and that a genuinely stale status (Pending review on the
live page despite GRANTED in the sheet) is counted separately, not as
a failure. See README "Testing the Drive backfill" for the manual
smoke test still required before trusting this against real
Refunnel/Drive accounts.

CONFIRMED REAL, direct decision from live evidence: this project spent
a long stretch on Refunnel's own native "Save to Drive" feature
instead of downloading directly. Every piece of that click-through was
eventually proven correct -- confirmed byte-identical to a version
that once worked, confirmed against the right Drive folder, confirmed
accepted by Refunnel's own "Uploading to Google Drive -- it will
appear shortly" toast -- and the file still never landed, across many
separate runs, on a transfer that happens entirely on Refunnel's own
servers once that toast appears. This test file was rewritten
alongside download_approved_to_drive.py's return to downloading and
uploading the video itself.
"""
import gspread.exceptions
import pytest

import download_approved_to_drive as dad


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


MASTER_HEADER = ["id", "platform", "username", "rights_status", "drive_uploaded_at", "updated_at"]


def _row(media_id, status="GRANTED", uploaded="", updated_at=""):
    return [media_id, "TIKTOK", "creatorname", status, uploaded, updated_at]


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

    def evaluate(self, *_a, **_kw):
        pass  # refunnel_export.scroll_to_top() calls this once per batch

    def wait_for_timeout(self, *_a, **_kw):
        pass

    def close(self):
        pass


class _FakePath:
    """A minimal stand-in for the Path download_approved_video returns --
    just enough surface (suffix, exists, unlink, str/repr, equality by
    string) for process_one_brand's own logic to exercise correctly."""

    def __init__(self, path_str):
        self._s = path_str
        self.unlinked = False

    @property
    def suffix(self):
        import posixpath
        return posixpath.splitext(self._s)[1]

    def exists(self):
        return not self.unlinked

    def unlink(self):
        self.unlinked = True

    def __str__(self):
        return self._s

    def __repr__(self):
        return f"_FakePath({self._s!r})"

    def __eq__(self, other):
        return str(other) == self._s

    def __hash__(self):
        return hash(self._s)


@pytest.fixture(autouse=True)
def _stub_browser_and_workspace(monkeypatch):
    monkeypatch.setattr(
        dad.refunnel_auth, "load_or_refresh_session",
        lambda **kw: (_FakePlaywright(), _FakeBrowser(), _FakeContext())
    )
    monkeypatch.setattr(dad.refunnel_auth, "refunnel_social_listening_url", lambda *a, **kw: "https://x")
    monkeypatch.setattr(dad.refunnel_export, "goto_social_listening_for_workspace", lambda *a, **kw: None)
    monkeypatch.setattr(dad.refunnel_export, "scroll_to_top", lambda *a, **kw: None)
    # No test in this file should ever need a real sleep.
    monkeypatch.setattr(dad.time, "sleep", lambda *a: None)
    # Default: find_existing_upload finds nothing (genuinely new upload
    # every time), download succeeds with a plain .mp4 file, and
    # upload_file succeeds. Individual tests override any of these to
    # exercise other paths.
    monkeypatch.setattr(dad.drive_upload, "find_existing_upload", lambda *a, **kw: None)
    monkeypatch.setattr(
        dad.refunnel_export, "download_approved_video",
        lambda page, media_id, download_dir, **kw: _FakePath(f"{download_dir}/{media_id}.mp4")
    )
    monkeypatch.setattr(dad.drive_upload, "upload_file", lambda *a, **kw: "new_file_id")


def _brand_config(name="Swoveralls", spreadsheet_secret="SPREADSHEET_ID_SWOVERALLS", drive_secret="DRIVE_FOLDER_ID_SWOVERALLS"):
    cfg = {"name": name, "refunnel_workspace_name": name}
    if spreadsheet_secret:
        cfg["spreadsheet_id_secret"] = spreadsheet_secret
    if drive_secret:
        cfg["drive_folder_id_secret"] = drive_secret
    return cfg


# ---------- basic skip conditions ----------

def test_skips_if_spreadsheet_secret_not_set(monkeypatch, capsys):
    monkeypatch.delenv("SPREADSHEET_ID_SWOVERALLS", raising=False)
    dad.process_one_brand(FakeClient({}), _brand_config(), object(), "x@example.com", ["Swoveralls"])
    assert "SPREADSHEET_ID_SWOVERALLS" in capsys.readouterr().out


def test_skips_if_no_drive_secret_configured(monkeypatch, capsys):
    monkeypatch.setenv("SPREADSHEET_ID_SWOVERALLS", "sheet1")
    dad.process_one_brand(FakeClient({}), _brand_config(drive_secret=None), object(), "x@example.com", ["Swoveralls"])
    assert "no drive_folder_id_secret configured" in capsys.readouterr().out


def test_skips_if_drive_secret_configured_but_env_var_missing(monkeypatch, capsys):
    monkeypatch.setenv("SPREADSHEET_ID_SWOVERALLS", "sheet1")
    monkeypatch.delenv("DRIVE_FOLDER_ID_SWOVERALLS", raising=False)
    dad.process_one_brand(FakeClient({}), _brand_config(), object(), "x@example.com", ["Swoveralls"])
    assert "DRIVE_FOLDER_ID_SWOVERALLS" in capsys.readouterr().out


def test_skips_if_no_master_data_tab(monkeypatch, capsys):
    monkeypatch.setenv("SPREADSHEET_ID_SWOVERALLS", "sheet1")
    monkeypatch.setenv("DRIVE_FOLDER_ID_SWOVERALLS", "folder1")
    gc = FakeClient({"sheet1": FakeSpreadsheet(worksheets={})})
    dad.process_one_brand(gc, _brand_config(), object(), "x@example.com", ["Swoveralls"])
    assert "no 'Master Data' tab yet" in capsys.readouterr().out


def test_nothing_to_upload_when_no_approved_rows(monkeypatch, capsys):
    monkeypatch.setenv("SPREADSHEET_ID_SWOVERALLS", "sheet1")
    monkeypatch.setenv("DRIVE_FOLDER_ID_SWOVERALLS", "folder1")
    master_ws = FakeWorksheet(rows=[MASTER_HEADER, _row("tk_1", status="NONE")])
    gc = FakeClient({"sheet1": FakeSpreadsheet(worksheets={"Master Data": master_ws})})
    dad.process_one_brand(gc, _brand_config(), object(), "x@example.com", ["Swoveralls"])
    assert "nothing new to upload" in capsys.readouterr().out


# ---------- happy path: download, upload, mark the sheet ----------

def test_happy_path_downloads_uploads_and_marks_the_row(monkeypatch):
    monkeypatch.setenv("SPREADSHEET_ID_SWOVERALLS", "sheet1")
    monkeypatch.setenv("DRIVE_FOLDER_ID_SWOVERALLS", "folder1")
    master_ws = FakeWorksheet(rows=[MASTER_HEADER, _row("tk_1")])
    gc = FakeClient({"sheet1": FakeSpreadsheet(worksheets={"Master Data": master_ws})})

    download_calls = []
    monkeypatch.setattr(
        dad.refunnel_export, "download_approved_video",
        lambda page, media_id, download_dir, **kw: download_calls.append(media_id) or _FakePath(f"{download_dir}/{media_id}.mp4")
    )
    upload_calls = []
    monkeypatch.setattr(
        dad.drive_upload, "upload_file",
        lambda service, local_path, filename, folder_id: upload_calls.append((local_path, filename, folder_id)) or "file_123"
    )

    dad.process_one_brand(gc, _brand_config(), object(), "x@example.com", ["Swoveralls"])

    assert download_calls == ["tk_1"]
    assert len(upload_calls) == 1
    local_path, filename, folder_id = upload_calls[0]
    assert filename == "Swoveralls | @creatorname | tk_1.mp4"
    assert folder_id == "folder1"
    header = master_ws.rows[0]
    upload_idx = header.index("drive_uploaded_at")
    assert master_ws.rows[1][upload_idx]  # non-blank -- marked


def test_filename_uses_the_downloaded_files_real_extension_not_a_hardcoded_mp4(monkeypatch):
    # CONFIRMED REAL: the filename can only be built once local_path's
    # REAL extension is known -- matching the original, proven version
    # of this function, not a hardcoded ".mp4" guess.
    monkeypatch.setenv("SPREADSHEET_ID_SWOVERALLS", "sheet1")
    monkeypatch.setenv("DRIVE_FOLDER_ID_SWOVERALLS", "folder1")
    master_ws = FakeWorksheet(rows=[MASTER_HEADER, _row("tk_1")])
    gc = FakeClient({"sheet1": FakeSpreadsheet(worksheets={"Master Data": master_ws})})

    monkeypatch.setattr(
        dad.refunnel_export, "download_approved_video",
        lambda page, media_id, download_dir, **kw: _FakePath(f"{download_dir}/{media_id}.webm")
    )
    upload_calls = []
    monkeypatch.setattr(
        dad.drive_upload, "upload_file",
        lambda service, local_path, filename, folder_id: upload_calls.append(filename) or "file_123"
    )

    dad.process_one_brand(gc, _brand_config(), object(), "x@example.com", ["Swoveralls"])

    assert upload_calls == ["Swoveralls | @creatorname | tk_1.webm"]


def test_sabotage_hardcoding_mp4_would_be_caught(monkeypatch):
    monkeypatch.setenv("SPREADSHEET_ID_SWOVERALLS", "sheet1")
    monkeypatch.setenv("DRIVE_FOLDER_ID_SWOVERALLS", "folder1")
    master_ws = FakeWorksheet(rows=[MASTER_HEADER, _row("tk_1")])
    gc = FakeClient({"sheet1": FakeSpreadsheet(worksheets={"Master Data": master_ws})})

    monkeypatch.setattr(
        dad.refunnel_export, "download_approved_video",
        lambda page, media_id, download_dir, **kw: _FakePath(f"{download_dir}/{media_id}.webm")
    )
    upload_calls = []
    monkeypatch.setattr(
        dad.drive_upload, "upload_file",
        lambda service, local_path, filename, folder_id: upload_calls.append(filename) or "file_123"
    )

    dad.process_one_brand(gc, _brand_config(), object(), "x@example.com", ["Swoveralls"])

    with pytest.raises(AssertionError):
        assert upload_calls == ["Swoveralls | @creatorname | tk_1.mp4"]  # wrong -- ignores the real extension
    assert upload_calls == ["Swoveralls | @creatorname | tk_1.webm"]


def test_successful_upload_deletes_the_local_temp_file(monkeypatch):
    monkeypatch.setenv("SPREADSHEET_ID_SWOVERALLS", "sheet1")
    monkeypatch.setenv("DRIVE_FOLDER_ID_SWOVERALLS", "folder1")
    master_ws = FakeWorksheet(rows=[MASTER_HEADER, _row("tk_1")])
    gc = FakeClient({"sheet1": FakeSpreadsheet(worksheets={"Master Data": master_ws})})

    fake_path = _FakePath("downloads/drive_backfill/Swoveralls/tk_1.mp4")
    monkeypatch.setattr(dad.refunnel_export, "download_approved_video", lambda *a, **kw: fake_path)

    dad.process_one_brand(gc, _brand_config(), object(), "x@example.com", ["Swoveralls"])

    assert fake_path.unlinked is True


# ---------- already uploaded by an earlier, crashed run ----------

def test_already_present_from_a_crashed_prior_run_is_marked_without_re_downloading(monkeypatch):
    # CONFIRMED REAL gap this closes: a run that downloaded and
    # uploaded successfully but crashed before marking drive_uploaded_at
    # would otherwise download and upload the SAME media_id again --
    # a real duplicate file. The final filename already embeds
    # media_id, so checking for it directly, before ever downloading
    # anything, catches that case.
    monkeypatch.setenv("SPREADSHEET_ID_SWOVERALLS", "sheet1")
    monkeypatch.setenv("DRIVE_FOLDER_ID_SWOVERALLS", "folder1")
    master_ws = FakeWorksheet(rows=[MASTER_HEADER, _row("tk_1")])
    gc = FakeClient({"sheet1": FakeSpreadsheet(worksheets={"Master Data": master_ws})})

    monkeypatch.setattr(dad.drive_upload, "find_existing_upload",
                        lambda service, folder_id, media_id: {"id": "file_1", "name": "raw.mp4"})
    download_calls = []
    monkeypatch.setattr(dad.refunnel_export, "download_approved_video",
                        lambda *a, **kw: download_calls.append(1) or _FakePath("x.mp4"))
    upload_calls = []
    monkeypatch.setattr(dad.drive_upload, "upload_file", lambda *a, **kw: upload_calls.append(1) or "x")

    dad.process_one_brand(gc, _brand_config(), object(), "x@example.com", ["Swoveralls"])

    assert download_calls == []  # never downloaded again
    assert upload_calls == []    # never uploaded again
    header = master_ws.rows[0]
    upload_idx = header.index("drive_uploaded_at")
    assert master_ws.rows[1][upload_idx]  # still correctly marked


def test_sabotage_re_downloading_an_already_present_upload_would_be_caught(monkeypatch):
    monkeypatch.setenv("SPREADSHEET_ID_SWOVERALLS", "sheet1")
    monkeypatch.setenv("DRIVE_FOLDER_ID_SWOVERALLS", "folder1")
    master_ws = FakeWorksheet(rows=[MASTER_HEADER, _row("tk_1")])
    gc = FakeClient({"sheet1": FakeSpreadsheet(worksheets={"Master Data": master_ws})})

    monkeypatch.setattr(dad.drive_upload, "find_existing_upload",
                        lambda service, folder_id, media_id: {"id": "file_1", "name": "raw.mp4"})
    download_calls = []
    monkeypatch.setattr(dad.refunnel_export, "download_approved_video",
                        lambda *a, **kw: download_calls.append(1) or _FakePath("x.mp4"))

    dad.process_one_brand(gc, _brand_config(), object(), "x@example.com", ["Swoveralls"])

    with pytest.raises(AssertionError):
        assert download_calls == [1]  # wrong -- that would be a genuine duplicate download+upload
    assert download_calls == []


# ---------- pending review (status changed since export) is not a failure ----------

def test_status_changed_since_export_is_counted_separately_not_as_failed(monkeypatch, capsys):
    monkeypatch.setenv("SPREADSHEET_ID_SWOVERALLS", "sheet1")
    monkeypatch.setenv("DRIVE_FOLDER_ID_SWOVERALLS", "folder1")
    master_ws = FakeWorksheet(rows=[MASTER_HEADER, _row("tk_1")])
    gc = FakeClient({"sheet1": FakeSpreadsheet(worksheets={"Master Data": master_ws})})

    monkeypatch.setattr(dad.refunnel_export, "download_approved_video", lambda *a, **kw: None)

    dad.process_one_brand(gc, _brand_config(), object(), "x@example.com", ["Swoveralls"])

    out = capsys.readouterr().out
    assert "status changed since export 1" in out
    assert "failed 0" in out
    header = master_ws.rows[0]
    upload_idx = header.index("drive_uploaded_at")
    assert not master_ws.rows[1][upload_idx]  # left unmarked, corrects itself on a future export


def test_sabotage_counting_status_change_as_failed_would_be_caught(monkeypatch, capsys):
    monkeypatch.setenv("SPREADSHEET_ID_SWOVERALLS", "sheet1")
    monkeypatch.setenv("DRIVE_FOLDER_ID_SWOVERALLS", "folder1")
    master_ws = FakeWorksheet(rows=[MASTER_HEADER, _row("tk_1")])
    gc = FakeClient({"sheet1": FakeSpreadsheet(worksheets={"Master Data": master_ws})})

    monkeypatch.setattr(dad.refunnel_export, "download_approved_video", lambda *a, **kw: None)

    dad.process_one_brand(gc, _brand_config(), object(), "x@example.com", ["Swoveralls"])

    out = capsys.readouterr().out
    with pytest.raises(AssertionError):
        assert "failed 1" in out  # wrong -- would wrongly blame the download step for stale Master Data
    assert "failed 0" in out


# ---------- card not found ----------

def test_card_not_found_leaves_row_unmarked_for_retry(monkeypatch, capsys):
    monkeypatch.setenv("SPREADSHEET_ID_SWOVERALLS", "sheet1")
    monkeypatch.setenv("DRIVE_FOLDER_ID_SWOVERALLS", "folder1")
    master_ws = FakeWorksheet(rows=[MASTER_HEADER, _row("tk_1")])
    gc = FakeClient({"sheet1": FakeSpreadsheet(worksheets={"Master Data": master_ws})})

    monkeypatch.setattr(dad.refunnel_export, "download_approved_video", lambda *a, **kw: False)

    dad.process_one_brand(gc, _brand_config(), object(), "x@example.com", ["Swoveralls"])

    out = capsys.readouterr().out
    assert "couldn't locate media_id='tk_1'" in out
    header = master_ws.rows[0]
    upload_idx = header.index("drive_uploaded_at")
    assert not master_ws.rows[1][upload_idx]


# ---------- exceptions during download/upload ----------

def test_one_failed_video_does_not_stop_the_rest(monkeypatch, capsys):
    monkeypatch.setenv("SPREADSHEET_ID_SWOVERALLS", "sheet1")
    monkeypatch.setenv("DRIVE_FOLDER_ID_SWOVERALLS", "folder1")
    rows = [MASTER_HEADER, _row("tk_bad"), _row("tk_good")]
    master_ws = FakeWorksheet(rows=rows)
    gc = FakeClient({"sheet1": FakeSpreadsheet(worksheets={"Master Data": master_ws})})

    downloaded = []

    def fake_download(page, media_id, download_dir, **kw):
        if media_id == "tk_bad":
            raise RuntimeError("simulated download failure")
        downloaded.append(media_id)
        return _FakePath(f"{download_dir}/{media_id}.mp4")

    monkeypatch.setattr(dad.refunnel_export, "download_approved_video", fake_download)

    dad.process_one_brand(gc, _brand_config(), object(), "x@example.com", ["Swoveralls"])

    assert downloaded == ["tk_good"]  # tk_bad's failure didn't stop tk_good from being tried
    out = capsys.readouterr().out
    assert "failed 1" in out
    assert "uploaded 1" in out


def test_a_failed_download_keeps_the_local_file_for_debugging(monkeypatch):
    # a video that failed partway through is exactly what's worth
    # inspecting from the debug artifact -- deleting it unconditionally
    # would leave nothing to debug a failed run with
    monkeypatch.setenv("SPREADSHEET_ID_SWOVERALLS", "sheet1")
    monkeypatch.setenv("DRIVE_FOLDER_ID_SWOVERALLS", "folder1")
    master_ws = FakeWorksheet(rows=[MASTER_HEADER, _row("tk_1")])
    gc = FakeClient({"sheet1": FakeSpreadsheet(worksheets={"Master Data": master_ws})})

    fake_path = _FakePath("downloads/drive_backfill/Swoveralls/tk_1.mp4")
    monkeypatch.setattr(dad.refunnel_export, "download_approved_video", lambda *a, **kw: fake_path)
    monkeypatch.setattr(dad.drive_upload, "upload_file",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("simulated upload failure")))

    dad.process_one_brand(gc, _brand_config(), object(), "x@example.com", ["Swoveralls"])

    assert fake_path.unlinked is False


# ---------- incremental: an already-marked row is never reprocessed ----------

def test_already_uploaded_row_is_never_reprocessed(monkeypatch):
    monkeypatch.setenv("SPREADSHEET_ID_SWOVERALLS", "sheet1")
    monkeypatch.setenv("DRIVE_FOLDER_ID_SWOVERALLS", "folder1")
    master_ws = FakeWorksheet(rows=[MASTER_HEADER, _row("tk_1", uploaded="2026-01-01T00:00:00Z")])
    gc = FakeClient({"sheet1": FakeSpreadsheet(worksheets={"Master Data": master_ws})})

    download_calls = []
    monkeypatch.setattr(dad.refunnel_export, "download_approved_video",
                        lambda *a, **kw: download_calls.append(1) or _FakePath("x.mp4"))

    dad.process_one_brand(gc, _brand_config(), object(), "x@example.com", ["Swoveralls"])

    assert download_calls == []


def test_sabotage_reprocessing_an_uploaded_row_would_be_caught(monkeypatch):
    monkeypatch.setenv("SPREADSHEET_ID_SWOVERALLS", "sheet1")
    monkeypatch.setenv("DRIVE_FOLDER_ID_SWOVERALLS", "folder1")
    master_ws = FakeWorksheet(rows=[MASTER_HEADER, _row("tk_1", uploaded="2026-01-01T00:00:00Z")])
    gc = FakeClient({"sheet1": FakeSpreadsheet(worksheets={"Master Data": master_ws})})

    download_calls = []
    monkeypatch.setattr(dad.refunnel_export, "download_approved_video",
                        lambda *a, **kw: download_calls.append(1) or _FakePath("x.mp4"))

    dad.process_one_brand(gc, _brand_config(), object(), "x@example.com", ["Swoveralls"])

    with pytest.raises(AssertionError):
        assert download_calls == [1]  # wrong -- would mean re-downloading an already-uploaded video
    assert download_calls == []


# ---------- BATCH_SIZE caps how many successful uploads one run does ----------

def test_batch_size_caps_how_many_are_processed_per_run(monkeypatch):
    monkeypatch.setenv("SPREADSHEET_ID_SWOVERALLS", "sheet1")
    monkeypatch.setenv("DRIVE_FOLDER_ID_SWOVERALLS", "folder1")
    monkeypatch.setattr(dad, "BATCH_SIZE", 2)
    rows = [MASTER_HEADER] + [_row(f"tk_{i}") for i in range(5)]
    master_ws = FakeWorksheet(rows=rows)
    gc = FakeClient({"sheet1": FakeSpreadsheet(worksheets={"Master Data": master_ws})})

    downloaded = []
    monkeypatch.setattr(
        dad.refunnel_export, "download_approved_video",
        lambda page, media_id, download_dir, **kw: downloaded.append(media_id) or _FakePath(f"{download_dir}/{media_id}.mp4")
    )

    dad.process_one_brand(gc, _brand_config(), object(), "x@example.com", ["Swoveralls"])

    assert len(downloaded) == 2


# ---------- keeps going past failures to reach real successes (confirmed real bug fix) ----------

def test_run_keeps_going_past_failures_to_reach_real_successes(monkeypatch):
    # CONFIRMED REAL bug this fixes: a live run showed the exact same
    # media_ids, same order, same 0 successes, across multiple separate
    # runs and days -- because target_ids used to be a fixed slice of
    # the FIRST BATCH_SIZE ids, taken once. Any id that fails never
    # gets drive_uploaded_at set, so it's still first in line next time
    # -- the queue was permanently stuck on the same persistently-
    # failing front, never reaching ids further down that might
    # actually succeed.
    monkeypatch.setenv("SPREADSHEET_ID_SWOVERALLS", "sheet1")
    monkeypatch.setenv("DRIVE_FOLDER_ID_SWOVERALLS", "folder1")
    monkeypatch.setattr(dad, "BATCH_SIZE", 2)
    monkeypatch.setattr(dad, "MAX_ATTEMPTS_PER_RUN", 10)

    # first 5 are permanently un-locatable; ids 5 and 6 would actually succeed
    rows = [MASTER_HEADER] + [_row(f"tk_{i}") for i in range(8)]
    master_ws = FakeWorksheet(rows=rows)
    gc = FakeClient({"sheet1": FakeSpreadsheet(worksheets={"Master Data": master_ws})})

    downloaded = []

    def fake_download(page, media_id, download_dir, **kw):
        if media_id in ("tk_5", "tk_6"):
            downloaded.append(media_id)
            return _FakePath(f"{download_dir}/{media_id}.mp4")
        return False
    monkeypatch.setattr(dad.refunnel_export, "download_approved_video", fake_download)

    dad.process_one_brand(gc, _brand_config(), object(), "x@example.com", ["Swoveralls"])

    assert downloaded == ["tk_5", "tk_6"]


def test_sabotage_stopping_at_a_fixed_slice_would_be_caught(monkeypatch):
    monkeypatch.setenv("SPREADSHEET_ID_SWOVERALLS", "sheet1")
    monkeypatch.setenv("DRIVE_FOLDER_ID_SWOVERALLS", "folder1")
    monkeypatch.setattr(dad, "BATCH_SIZE", 2)
    monkeypatch.setattr(dad, "MAX_ATTEMPTS_PER_RUN", 10)

    rows = [MASTER_HEADER] + [_row(f"tk_{i}") for i in range(8)]
    master_ws = FakeWorksheet(rows=rows)
    gc = FakeClient({"sheet1": FakeSpreadsheet(worksheets={"Master Data": master_ws})})

    attempted = []

    def fake_download(page, media_id, download_dir, **kw):
        attempted.append(media_id)
        if media_id in ("tk_5", "tk_6"):
            return _FakePath(f"{download_dir}/{media_id}.mp4")
        return False
    monkeypatch.setattr(dad.refunnel_export, "download_approved_video", fake_download)

    dad.process_one_brand(gc, _brand_config(), object(), "x@example.com", ["Swoveralls"])

    with pytest.raises(AssertionError):
        assert "tk_5" not in attempted  # wrong -- that's the old, permanently-stuck behavior
    assert "tk_5" in attempted and "tk_6" in attempted


# ---------- MAX_ATTEMPTS_PER_RUN safety cap ----------

def test_max_attempts_stops_a_run_where_nothing_ever_succeeds(monkeypatch, capsys):
    monkeypatch.setenv("SPREADSHEET_ID_SWOVERALLS", "sheet1")
    monkeypatch.setenv("DRIVE_FOLDER_ID_SWOVERALLS", "folder1")
    monkeypatch.setattr(dad, "BATCH_SIZE", 50)
    monkeypatch.setattr(dad, "MAX_ATTEMPTS_PER_RUN", 3)

    rows = [MASTER_HEADER] + [_row(f"tk_{i}") for i in range(10)]
    master_ws = FakeWorksheet(rows=rows)
    gc = FakeClient({"sheet1": FakeSpreadsheet(worksheets={"Master Data": master_ws})})

    attempted = []
    monkeypatch.setattr(dad.refunnel_export, "download_approved_video",
                        lambda page, media_id, download_dir, **kw: attempted.append(media_id) or False)

    dad.process_one_brand(gc, _brand_config(), object(), "x@example.com", ["Swoveralls"])

    assert len(attempted) == 3
    assert "3-attempt safety cap" in capsys.readouterr().out


# ---------- time budget safety cap ----------

def test_time_budget_stops_a_run_where_everything_is_slow_not_stuck(monkeypatch):
    monkeypatch.setenv("SPREADSHEET_ID_SWOVERALLS", "sheet1")
    monkeypatch.setenv("DRIVE_FOLDER_ID_SWOVERALLS", "folder1")
    monkeypatch.setattr(dad, "BATCH_SIZE", 50)
    monkeypatch.setattr(dad, "MAX_ATTEMPTS_PER_RUN", 400)
    monkeypatch.setattr(dad, "DRIVE_BACKFILL_TIME_BUDGET_MINUTES", 0.0)  # already expired

    rows = [MASTER_HEADER] + [_row(f"tk_{i}") for i in range(10)]
    master_ws = FakeWorksheet(rows=rows)
    gc = FakeClient({"sheet1": FakeSpreadsheet(worksheets={"Master Data": master_ws})})

    attempted = []
    monkeypatch.setattr(dad.refunnel_export, "download_approved_video",
                        lambda page, media_id, download_dir, **kw: attempted.append(media_id) or True)

    dad.process_one_brand(gc, _brand_config(), object(), "x@example.com", ["Swoveralls"])

    assert len(attempted) == 0  # deadline already passed -- stopped before trying anything


def test_sabotage_ignoring_the_time_budget_would_be_caught(monkeypatch):
    monkeypatch.setenv("SPREADSHEET_ID_SWOVERALLS", "sheet1")
    monkeypatch.setenv("DRIVE_FOLDER_ID_SWOVERALLS", "folder1")
    monkeypatch.setattr(dad, "BATCH_SIZE", 50)
    monkeypatch.setattr(dad, "MAX_ATTEMPTS_PER_RUN", 400)
    monkeypatch.setattr(dad, "DRIVE_BACKFILL_TIME_BUDGET_MINUTES", 0.0)

    rows = [MASTER_HEADER] + [_row(f"tk_{i}") for i in range(10)]
    master_ws = FakeWorksheet(rows=rows)
    gc = FakeClient({"sheet1": FakeSpreadsheet(worksheets={"Master Data": master_ws})})

    attempted = []
    monkeypatch.setattr(dad.refunnel_export, "download_approved_video",
                        lambda page, media_id, download_dir, **kw: attempted.append(media_id) or True)

    dad.process_one_brand(gc, _brand_config(), object(), "x@example.com", ["Swoveralls"])

    with pytest.raises(AssertionError):
        assert len(attempted) == 10  # wrong -- would mean ignoring an already-expired budget
    assert len(attempted) == 0


# ---------- no sleep anywhere in a run ----------

def test_no_sleep_is_ever_called_anywhere_in_a_run(monkeypatch):
    monkeypatch.setenv("SPREADSHEET_ID_SWOVERALLS", "sheet1")
    monkeypatch.setenv("DRIVE_FOLDER_ID_SWOVERALLS", "folder1")
    rows = [MASTER_HEADER] + [_row(f"tk_{i}") for i in range(10)]
    master_ws = FakeWorksheet(rows=rows)
    gc = FakeClient({"sheet1": FakeSpreadsheet(worksheets={"Master Data": master_ws})})

    sleep_calls = []
    monkeypatch.setattr(dad.time, "sleep", lambda *a: sleep_calls.append(a))

    dad.process_one_brand(gc, _brand_config(), object(), "x@example.com", ["Swoveralls"])

    assert sleep_calls == []

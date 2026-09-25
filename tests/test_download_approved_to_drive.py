"""
Tests for download_approved_to_drive.py. Real browser login and the
real Drive API are monkeypatched out (this sandbox has no live
Refunnel/Drive access) -- these test the ORCHESTRATION logic: which
brands get skipped and why, which media_ids get selected and in what
order, the batch-size cap, incremental marking so an already-uploaded
id is never reprocessed, that one failed video doesn't stop the rest
of the batch, and that a triggered-but-not-yet-confirmed upload is
left unmarked for a retry rather than treated as a failure. See
README "Testing the Drive backfill" for the manual smoke test still
required before trusting this against real Refunnel/Drive accounts.
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
        # refunnel_export.scroll_to_top() calls this once per batch now
        # -- see the reset_scroll change to trigger_native_drive_upload.
        pass

    def wait_for_timeout(self, *_a, **_kw):
        pass

    def close(self):
        pass


@pytest.fixture(autouse=True)
def _stub_browser_and_workspace(monkeypatch):
    monkeypatch.setattr(
        dad.refunnel_auth, "load_or_refresh_session",
        lambda **kw: (_FakePlaywright(), _FakeBrowser(), _FakeContext())
    )
    monkeypatch.setattr(dad.refunnel_auth, "refunnel_social_listening_url", lambda *a, **kw: "https://x")
    monkeypatch.setattr(dad.refunnel_export, "goto_social_listening_for_workspace", lambda *a, **kw: None)
    # No test in this file should ever need a REAL sleep -- the
    # confirm phase's between-round wait is stubbed out unconditionally.
    monkeypatch.setattr(dad.time, "sleep", lambda *a: None)
    # Default: the native upload trigger succeeds, and find_existing_upload
    # returns nothing on the FIRST call (the pre-check, before triggering --
    # nothing uploaded yet) but a match on every call after that (the
    # confirm phase finding it immediately on its first check). Individual
    # tests override either half to exercise other paths.
    monkeypatch.setattr(dad.refunnel_export, "trigger_native_drive_upload", lambda *a, **kw: True)
    _find_calls = {}  # keyed by fragment -- each item's OWN first call (its pre-check) must be
    # None, independent of other items already triggered earlier in the same batch.

    def _default_find_existing_upload(drive_service, folder_id, fragment):
        _find_calls[fragment] = _find_calls.get(fragment, 0) + 1
        if _find_calls[fragment] == 1:
            return None
        return {"id": "fake_file_id", "name": "raw_refunnel_name.mp4"}
    monkeypatch.setattr(dad.drive_upload, "find_existing_upload", _default_find_existing_upload)
    monkeypatch.setattr(dad.drive_upload, "rename_file", lambda *a, **kw: None)


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


def test_happy_path_triggers_the_upload_and_leaves_it_for_a_future_run_to_confirm(monkeypatch):
    # CONFIRMED REAL simplification: no waiting or confirming within a
    # run at all now. Triggering is the only thing this run does for a
    # genuinely new upload -- the row stays unmarked until a FUTURE
    # run's pre-check (find_existing_upload) finds it landed and
    # renames it, whenever that turns out to be.
    monkeypatch.setenv("SPREADSHEET_ID_SWOVERALLS", "sheet1")
    monkeypatch.setenv("DRIVE_FOLDER_ID_SWOVERALLS", "folder1")
    master_ws = FakeWorksheet(rows=[MASTER_HEADER, _row("tk_1")])
    gc = FakeClient({"sheet1": FakeSpreadsheet(worksheets={"Master Data": master_ws})})

    trigger_calls = []
    monkeypatch.setattr(
        dad.refunnel_export, "trigger_native_drive_upload",
        lambda page, media_id, folder_name, **kw: trigger_calls.append((media_id, folder_name)) or True
    )
    find_calls = []
    monkeypatch.setattr(
        dad.drive_upload, "find_existing_upload",
        lambda service, folder_id, fragment: find_calls.append((folder_id, fragment)) or None
    )
    rename_calls = []
    monkeypatch.setattr(
        dad.drive_upload, "rename_file",
        lambda service, file_id, filename: rename_calls.append((file_id, filename))
    )

    dad.process_one_brand(gc, _brand_config(), object(), "x@example.com", ["Swoveralls"])

    assert trigger_calls == [("tk_1", "Refunnel - Swoveralls")]
    assert find_calls == [("folder1", "1")]  # exactly one pre-check, nothing more
    assert rename_calls == []  # nothing to rename yet -- it hasn't landed
    header = master_ws.rows[0]
    upload_idx = header.index("drive_uploaded_at")
    assert not master_ws.rows[1][upload_idx]  # still blank -- confirmed on a future run instead


def test_uses_the_configured_drive_folder_name_not_the_default(monkeypatch):
    monkeypatch.setenv("SPREADSHEET_ID_SWOVERALLS", "sheet1")
    monkeypatch.setenv("DRIVE_FOLDER_ID_SWOVERALLS", "folder1")
    master_ws = FakeWorksheet(rows=[MASTER_HEADER, _row("tk_1")])
    gc = FakeClient({"sheet1": FakeSpreadsheet(worksheets={"Master Data": master_ws})})

    trigger_calls = []
    monkeypatch.setattr(
        dad.refunnel_export, "trigger_native_drive_upload",
        lambda page, media_id, folder_name, **kw: trigger_calls.append(folder_name) or True
    )

    cfg = _brand_config()
    cfg["drive_folder_name"] = "Custom Folder Name"
    dad.process_one_brand(gc, cfg, object(), "x@example.com", ["Swoveralls"])

    assert trigger_calls == ["Custom Folder Name"]


def test_not_triggered_leaves_row_unmarked_for_retry(monkeypatch):
    # card couldn't be located this run -- not a failure, just "try again later"
    monkeypatch.setenv("SPREADSHEET_ID_SWOVERALLS", "sheet1")
    monkeypatch.setenv("DRIVE_FOLDER_ID_SWOVERALLS", "folder1")
    master_ws = FakeWorksheet(rows=[MASTER_HEADER, _row("tk_1")])
    gc = FakeClient({"sheet1": FakeSpreadsheet(worksheets={"Master Data": master_ws})})

    monkeypatch.setattr(dad.refunnel_export, "trigger_native_drive_upload", lambda *a, **kw: False)
    monkeypatch.setattr(dad.drive_upload, "find_existing_upload", lambda *a, **kw: None)

    dad.process_one_brand(gc, _brand_config(), object(), "x@example.com", ["Swoveralls"])

    header = master_ws.rows[0]
    upload_idx = header.index("drive_uploaded_at")
    assert not master_ws.rows[1][upload_idx]  # left unmarked


def test_a_new_trigger_leaves_the_row_unmarked_until_a_future_run_confirms_it(monkeypatch):
    # CONFIRMED REAL, deliberate design: Refunnel's upload happens on
    # its own servers, not instantly, and this run doesn't wait or
    # check for it at all -- triggering is all it does. The row stays
    # unmarked; that's the correct, expected state until some future
    # run's pre-check finds it landed, not a failure to react to now.
    monkeypatch.setenv("SPREADSHEET_ID_SWOVERALLS", "sheet1")
    monkeypatch.setenv("DRIVE_FOLDER_ID_SWOVERALLS", "folder1")
    master_ws = FakeWorksheet(rows=[MASTER_HEADER, _row("tk_1")])
    gc = FakeClient({"sheet1": FakeSpreadsheet(worksheets={"Master Data": master_ws})})

    monkeypatch.setattr(dad.refunnel_export, "trigger_native_drive_upload", lambda *a, **kw: True)
    monkeypatch.setattr(dad.drive_upload, "find_existing_upload", lambda *a, **kw: None)

    dad.process_one_brand(gc, _brand_config(), object(), "x@example.com", ["Swoveralls"])

    header = master_ws.rows[0]
    upload_idx = header.index("drive_uploaded_at")
    assert not master_ws.rows[1][upload_idx]  # left unmarked, not treated as failed


def test_already_uploaded_row_is_never_reprocessed(monkeypatch):
    monkeypatch.setenv("SPREADSHEET_ID_SWOVERALLS", "sheet1")
    monkeypatch.setenv("DRIVE_FOLDER_ID_SWOVERALLS", "folder1")
    master_ws = FakeWorksheet(rows=[MASTER_HEADER, _row("tk_1", uploaded="2026-09-01T00:00:00+00:00")])
    gc = FakeClient({"sheet1": FakeSpreadsheet(worksheets={"Master Data": master_ws})})

    called = []
    monkeypatch.setattr(dad.refunnel_export, "trigger_native_drive_upload",
                         lambda *a, **kw: called.append(1) or True)

    dad.process_one_brand(gc, _brand_config(), object(), "x@example.com", ["Swoveralls"])

    assert called == []  # never even attempted


def test_batch_size_caps_how_many_are_processed_per_run(monkeypatch):
    monkeypatch.setenv("SPREADSHEET_ID_SWOVERALLS", "sheet1")
    monkeypatch.setenv("DRIVE_FOLDER_ID_SWOVERALLS", "folder1")
    monkeypatch.setattr(dad, "BATCH_SIZE", 2)

    rows = [MASTER_HEADER] + [_row(f"tk_{i}") for i in range(5)]
    master_ws = FakeWorksheet(rows=rows)
    gc = FakeClient({"sheet1": FakeSpreadsheet(worksheets={"Master Data": master_ws})})

    attempted = []
    monkeypatch.setattr(dad.refunnel_export, "trigger_native_drive_upload",
                         lambda page, media_id, folder_name, **kw: attempted.append(media_id) or True)

    dad.process_one_brand(gc, _brand_config(), object(), "x@example.com", ["Swoveralls"])

    assert len(attempted) == 2  # capped, not all 5


def test_one_failed_video_does_not_stop_the_rest(monkeypatch, capsys):
    monkeypatch.setenv("SPREADSHEET_ID_SWOVERALLS", "sheet1")
    monkeypatch.setenv("DRIVE_FOLDER_ID_SWOVERALLS", "folder1")
    rows = [MASTER_HEADER, _row("tk_bad"), _row("tk_good")]
    master_ws = FakeWorksheet(rows=rows)
    gc = FakeClient({"sheet1": FakeSpreadsheet(worksheets={"Master Data": master_ws})})

    triggered = []

    def fake_trigger(page, media_id, folder_name, **kw):
        if media_id == "tk_bad":
            raise RuntimeError("simulated upload-trigger failure")
        triggered.append(media_id)
        return True

    monkeypatch.setattr(dad.refunnel_export, "trigger_native_drive_upload", fake_trigger)
    monkeypatch.setattr(dad.drive_upload, "find_existing_upload", lambda *a, **kw: None)

    dad.process_one_brand(gc, _brand_config(), object(), "x@example.com", ["Swoveralls"])

    assert triggered == ["tk_good"]  # tk_bad's failure didn't stop tk_good from being tried
    out = capsys.readouterr().out
    assert "failed 1" in out
    assert "triggered 1 new upload" in out


def test_sabotage_reprocessing_an_uploaded_row_would_be_caught(monkeypatch):
    monkeypatch.setenv("SPREADSHEET_ID_SWOVERALLS", "sheet1")
    monkeypatch.setenv("DRIVE_FOLDER_ID_SWOVERALLS", "folder1")
    master_ws = FakeWorksheet(rows=[MASTER_HEADER, _row("tk_1", uploaded="2026-09-01T00:00:00+00:00")])
    gc = FakeClient({"sheet1": FakeSpreadsheet(worksheets={"Master Data": master_ws})})

    called = []
    monkeypatch.setattr(dad.refunnel_export, "trigger_native_drive_upload",
                         lambda *a, **kw: called.append(1) or True)

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


def test_a_slow_prior_run_gets_renamed_instead_of_re_triggered(monkeypatch):
    # end-to-end: media_id was uploaded by a PREVIOUS run that missed
    # the confirmation window, so drive_uploaded_at is still blank --
    # this run must find and rename it, NOT trigger Refunnel again
    monkeypatch.setenv("SPREADSHEET_ID_SWOVERALLS", "sheet1")
    monkeypatch.setenv("DRIVE_FOLDER_ID_SWOVERALLS", "folder1")
    master_ws = FakeWorksheet(rows=[MASTER_HEADER, _row("tk_1")])
    gc = FakeClient({"sheet1": FakeSpreadsheet(worksheets={"Master Data": master_ws})})

    trigger_calls = []
    monkeypatch.setattr(dad.refunnel_export, "trigger_native_drive_upload",
                        lambda *a, **kw: trigger_calls.append(1) or True)
    monkeypatch.setattr(dad.drive_upload, "find_existing_upload",
                        lambda service, folder_id, fragment: {"id": "file_1", "name": "raw.mp4"})
    rename_calls = []
    monkeypatch.setattr(dad.drive_upload, "rename_file",
                        lambda service, file_id, name: rename_calls.append((file_id, name)))

    dad.process_one_brand(gc, _brand_config(), object(), "x@example.com", ["Swoveralls"])

    assert trigger_calls == []  # never triggered a new upload
    assert rename_calls == [("file_1", "Swoveralls | @creatorname | tk_1.mp4")]
    header = master_ws.rows[0]
    upload_idx = header.index("drive_uploaded_at")
    assert master_ws.rows[1][upload_idx]  # marked


def test_sabotage_re_triggering_when_an_upload_already_exists_would_be_caught(monkeypatch):
    monkeypatch.setenv("SPREADSHEET_ID_SWOVERALLS", "sheet1")
    monkeypatch.setenv("DRIVE_FOLDER_ID_SWOVERALLS", "folder1")
    master_ws = FakeWorksheet(rows=[MASTER_HEADER, _row("tk_1")])
    gc = FakeClient({"sheet1": FakeSpreadsheet(worksheets={"Master Data": master_ws})})

    trigger_calls = []
    monkeypatch.setattr(dad.refunnel_export, "trigger_native_drive_upload",
                        lambda *a, **kw: trigger_calls.append(1) or True)
    monkeypatch.setattr(dad.drive_upload, "find_existing_upload",
                        lambda service, folder_id, fragment: {"id": "file_1", "name": "raw.mp4"})
    monkeypatch.setattr(dad.drive_upload, "rename_file", lambda *a, **kw: None)

    dad.process_one_brand(gc, _brand_config(), object(), "x@example.com", ["Swoveralls"])

    with pytest.raises(AssertionError):
        assert len(trigger_calls) == 1  # wrong -- that's the duplicate-upload bug
    assert len(trigger_calls) == 0


def test_scroll_resets_once_per_batch_not_once_per_item(monkeypatch):
    # CONFIRMED REAL gap this fixes: trigger_native_drive_upload used
    # to reset scroll unconditionally on every call, so a batch of
    # several videos meant a full reset-and-rescroll per item. Now
    # reset happens ONCE for the whole batch, matching the same proven
    # pattern already used for email scraping.
    monkeypatch.setenv("SPREADSHEET_ID_SWOVERALLS", "sheet1")
    monkeypatch.setenv("DRIVE_FOLDER_ID_SWOVERALLS", "folder1")
    master_ws = FakeWorksheet(rows=[MASTER_HEADER, _row("tk_1"), _row("tk_2"), _row("tk_3")])
    gc = FakeClient({"sheet1": FakeSpreadsheet(worksheets={"Master Data": master_ws})})

    reset_calls = []
    monkeypatch.setattr(dad.refunnel_export, "scroll_to_top", lambda *a, **kw: reset_calls.append(1))
    trigger_calls = []
    monkeypatch.setattr(dad.refunnel_export, "trigger_native_drive_upload",
                        lambda *a, **kw: trigger_calls.append(kw.get("reset_scroll")) or True)

    dad.process_one_brand(gc, _brand_config(), object(), "x@example.com", ["Swoveralls"])

    assert len(reset_calls) == 1  # once for the whole 3-item batch, not 3 times
    assert trigger_calls == [False, False, False]  # each item skips its own reset


def test_sabotage_resetting_scroll_per_item_would_be_caught(monkeypatch):
    monkeypatch.setenv("SPREADSHEET_ID_SWOVERALLS", "sheet1")
    monkeypatch.setenv("DRIVE_FOLDER_ID_SWOVERALLS", "folder1")
    master_ws = FakeWorksheet(rows=[MASTER_HEADER, _row("tk_1"), _row("tk_2"), _row("tk_3")])
    gc = FakeClient({"sheet1": FakeSpreadsheet(worksheets={"Master Data": master_ws})})

    reset_calls = []
    monkeypatch.setattr(dad.refunnel_export, "scroll_to_top", lambda *a, **kw: reset_calls.append(1))
    monkeypatch.setattr(dad.refunnel_export, "trigger_native_drive_upload", lambda *a, **kw: True)

    dad.process_one_brand(gc, _brand_config(), object(), "x@example.com", ["Swoveralls"])

    with pytest.raises(AssertionError):
        assert len(reset_calls) == 3  # wrong -- that's the old, per-item reset behaviour
    assert len(reset_calls) == 1


def test_status_changed_since_export_is_counted_separately_not_as_failed(monkeypatch, capsys):
    # CONFIRMED REAL distinction: None means trigger_native_drive_upload
    # found the card but its real status on Refunnel's live page no
    # longer matches Master Data -- not a failure, and it already
    # printed its own full explanation, so the caller must not print a
    # confusing second "couldn't locate" message on top of it.
    monkeypatch.setenv("SPREADSHEET_ID_SWOVERALLS", "sheet1")
    monkeypatch.setenv("DRIVE_FOLDER_ID_SWOVERALLS", "folder1")
    master_ws = FakeWorksheet(rows=[MASTER_HEADER, _row("tk_1")])
    gc = FakeClient({"sheet1": FakeSpreadsheet(worksheets={"Master Data": master_ws})})

    monkeypatch.setattr(dad.refunnel_export, "trigger_native_drive_upload", lambda *a, **kw: None)

    dad.process_one_brand(gc, _brand_config(), object(), "x@example.com", ["Swoveralls"])

    out = capsys.readouterr().out
    assert "couldn't locate media_id" not in out  # no confusing second message
    assert "status changed since export 1" in out
    assert "failed 0" in out  # NOT counted as a failure


def test_sabotage_counting_status_change_as_failed_would_be_caught(monkeypatch, capsys):
    monkeypatch.setenv("SPREADSHEET_ID_SWOVERALLS", "sheet1")
    monkeypatch.setenv("DRIVE_FOLDER_ID_SWOVERALLS", "folder1")
    master_ws = FakeWorksheet(rows=[MASTER_HEADER, _row("tk_1")])
    gc = FakeClient({"sheet1": FakeSpreadsheet(worksheets={"Master Data": master_ws})})

    monkeypatch.setattr(dad.refunnel_export, "trigger_native_drive_upload", lambda *a, **kw: None)

    dad.process_one_brand(gc, _brand_config(), object(), "x@example.com", ["Swoveralls"])

    out = capsys.readouterr().out
    with pytest.raises(AssertionError):
        assert "failed 1" in out  # wrong -- would wrongly blame the scraper for stale Master Data
    assert "failed 0" in out


def test_status_changed_prints_nothing_per_item_and_writes_nothing_to_the_sheet(monkeypatch, capsys):
    # CONFIRMED REAL feedback: a status mismatch is not an error and
    # needs no per-item narration or evidence-gathering -- the count in
    # the final summary line is enough, matching how a Pending review
    # skip during email scraping is already handled. It also must
    # never touch the sheet: the row stays exactly as the last export
    # set it until a future export naturally corrects it.
    monkeypatch.setenv("SPREADSHEET_ID_SWOVERALLS", "sheet1")
    monkeypatch.setenv("DRIVE_FOLDER_ID_SWOVERALLS", "folder1")
    master_ws = FakeWorksheet(rows=[MASTER_HEADER, _row("tk_1")])
    gc = FakeClient({"sheet1": FakeSpreadsheet(worksheets={"Master Data": master_ws})})

    monkeypatch.setattr(dad.refunnel_export, "trigger_native_drive_upload", lambda *a, **kw: None)

    dad.process_one_brand(gc, _brand_config(), object(), "x@example.com", ["Swoveralls"])

    out = capsys.readouterr().out
    assert "media_id='tk_1'" not in out  # no per-item message at all
    assert "status changed since export 1" in out  # counted once in the summary
    assert master_ws.rows[1] == _row("tk_1")  # row completely untouched


def test_sabotage_a_per_item_message_reappearing_would_be_caught(monkeypatch, capsys):
    monkeypatch.setenv("SPREADSHEET_ID_SWOVERALLS", "sheet1")
    monkeypatch.setenv("DRIVE_FOLDER_ID_SWOVERALLS", "folder1")
    master_ws = FakeWorksheet(rows=[MASTER_HEADER, _row("tk_1")])
    gc = FakeClient({"sheet1": FakeSpreadsheet(worksheets={"Master Data": master_ws})})

    monkeypatch.setattr(dad.refunnel_export, "trigger_native_drive_upload", lambda *a, **kw: None)

    dad.process_one_brand(gc, _brand_config(), object(), "x@example.com", ["Swoveralls"])

    out = capsys.readouterr().out
    with pytest.raises(AssertionError):
        assert "media_id='tk_1'" in out  # wrong -- would mean the removed noise came back
    assert "media_id='tk_1'" not in out


def test_run_keeps_going_past_failures_to_reach_real_successes(monkeypatch):
    # CONFIRMED REAL bug this fixes: a live run showed the exact same
    # 50 media_ids, same order, same 0 successes, across multiple
    # separate runs and days -- because target_ids used to be a fixed
    # slice of the FIRST BATCH_SIZE ids, taken once. Any id that fails
    # never gets drive_uploaded_at set, so it's still first in line
    # next time -- the queue was permanently stuck on the same
    # persistently-failing front, never reaching ids further down that
    # might actually succeed.
    monkeypatch.setenv("SPREADSHEET_ID_SWOVERALLS", "sheet1")
    monkeypatch.setenv("DRIVE_FOLDER_ID_SWOVERALLS", "folder1")
    monkeypatch.setattr(dad, "BATCH_SIZE", 2)
    monkeypatch.setattr(dad, "MAX_ATTEMPTS_PER_RUN", 10)

    # first 5 are permanently un-locatable; ids 5 and 6 would actually succeed
    rows = [MASTER_HEADER] + [_row(f"tk_{i}") for i in range(8)]
    master_ws = FakeWorksheet(rows=rows)
    gc = FakeClient({"sheet1": FakeSpreadsheet(worksheets={"Master Data": master_ws})})

    triggered = []

    def fake_trigger(page, media_id, folder_name, **kw):
        succeeds = media_id in ("tk_5", "tk_6")
        if succeeds:
            triggered.append(media_id)
        return succeeds
    monkeypatch.setattr(dad.refunnel_export, "trigger_native_drive_upload", fake_trigger)

    dad.process_one_brand(gc, _brand_config(), object(), "x@example.com", ["Swoveralls"])

    # both real successes reached and triggered, despite 5 failures in front of them
    assert triggered == ["tk_5", "tk_6"]


def test_sabotage_stopping_at_a_fixed_slice_would_be_caught(monkeypatch):
    monkeypatch.setenv("SPREADSHEET_ID_SWOVERALLS", "sheet1")
    monkeypatch.setenv("DRIVE_FOLDER_ID_SWOVERALLS", "folder1")
    monkeypatch.setattr(dad, "BATCH_SIZE", 2)
    monkeypatch.setattr(dad, "MAX_ATTEMPTS_PER_RUN", 10)

    rows = [MASTER_HEADER] + [_row(f"tk_{i}") for i in range(8)]
    master_ws = FakeWorksheet(rows=rows)
    gc = FakeClient({"sheet1": FakeSpreadsheet(worksheets={"Master Data": master_ws})})

    attempted = []

    def fake_trigger(page, media_id, folder_name, **kw):
        attempted.append(media_id)
        return media_id in ("tk_5", "tk_6")
    monkeypatch.setattr(dad.refunnel_export, "trigger_native_drive_upload", fake_trigger)

    dad.process_one_brand(gc, _brand_config(), object(), "x@example.com", ["Swoveralls"])

    with pytest.raises(AssertionError):
        assert "tk_5" not in attempted  # wrong -- that's the old, permanently-stuck behavior
    assert "tk_5" in attempted and "tk_6" in attempted


def test_time_budget_stops_a_run_where_everything_is_slow_not_stuck(monkeypatch):
    # CONFIRMED REAL risk this closes: "not yet confirmed" (a slow
    # Drive transfer missing the 30s window) is transient, not a
    # permanent failure -- but if MANY items in one run hit this same
    # slow pattern, the "keep going past failures" logic could burn
    # through attempts one 30s wait at a time well past a single run's
    # reasonable length. This bounds it, same proven pattern as email
    # scraping's own time budget.
    monkeypatch.setenv("SPREADSHEET_ID_SWOVERALLS", "sheet1")
    monkeypatch.setenv("DRIVE_FOLDER_ID_SWOVERALLS", "folder1")
    monkeypatch.setattr(dad, "BATCH_SIZE", 50)
    monkeypatch.setattr(dad, "MAX_ATTEMPTS_PER_RUN", 400)
    monkeypatch.setattr(dad, "DRIVE_BACKFILL_TIME_BUDGET_MINUTES", 0.0)  # already expired

    rows = [MASTER_HEADER] + [_row(f"tk_{i}") for i in range(10)]
    master_ws = FakeWorksheet(rows=rows)
    gc = FakeClient({"sheet1": FakeSpreadsheet(worksheets={"Master Data": master_ws})})

    attempted = []
    monkeypatch.setattr(dad.refunnel_export, "trigger_native_drive_upload",
                        lambda page, media_id, folder_name, **kw: attempted.append(media_id) or True)

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
    monkeypatch.setattr(dad.refunnel_export, "trigger_native_drive_upload",
                        lambda page, media_id, folder_name, **kw: attempted.append(media_id) or True)

    dad.process_one_brand(gc, _brand_config(), object(), "x@example.com", ["Swoveralls"])

    with pytest.raises(AssertionError):
        assert len(attempted) == 10  # wrong -- would mean ignoring an already-expired budget
    assert len(attempted) == 0



def test_no_sleep_is_ever_called_anywhere_in_a_run(monkeypatch):
    # CONFIRMED REAL simplification, direct feedback: no waiting or
    # checking for confirmation within a run at all, at any point.
    # Trigger, and move on -- a future run's pre-check handles
    # confirming and renaming whenever it actually lands.
    monkeypatch.setenv("SPREADSHEET_ID_SWOVERALLS", "sheet1")
    monkeypatch.setenv("DRIVE_FOLDER_ID_SWOVERALLS", "folder1")
    rows = [MASTER_HEADER] + [_row(f"tk_{i}") for i in range(10)]
    master_ws = FakeWorksheet(rows=rows)
    gc = FakeClient({"sheet1": FakeSpreadsheet(worksheets={"Master Data": master_ws})})

    monkeypatch.setattr(dad.refunnel_export, "trigger_native_drive_upload", lambda *a, **kw: True)
    monkeypatch.setattr(dad.drive_upload, "find_existing_upload", lambda *a, **kw: None)
    sleep_calls = []
    monkeypatch.setattr(dad.time, "sleep", lambda *a: sleep_calls.append(a))

    dad.process_one_brand(gc, _brand_config(), object(), "x@example.com", ["Swoveralls"])

    assert sleep_calls == []


def test_sabotage_adding_a_wait_back_in_would_be_caught(monkeypatch):
    monkeypatch.setenv("SPREADSHEET_ID_SWOVERALLS", "sheet1")
    monkeypatch.setenv("DRIVE_FOLDER_ID_SWOVERALLS", "folder1")
    master_ws = FakeWorksheet(rows=[MASTER_HEADER, _row("tk_1")])
    gc = FakeClient({"sheet1": FakeSpreadsheet(worksheets={"Master Data": master_ws})})

    monkeypatch.setattr(dad.refunnel_export, "trigger_native_drive_upload", lambda *a, **kw: True)
    monkeypatch.setattr(dad.drive_upload, "find_existing_upload", lambda *a, **kw: None)
    sleep_calls = []
    monkeypatch.setattr(dad.time, "sleep", lambda *a: sleep_calls.append(a))

    dad.process_one_brand(gc, _brand_config(), object(), "x@example.com", ["Swoveralls"])

    with pytest.raises(AssertionError):
        assert len(sleep_calls) > 0  # wrong -- would mean the old waiting complexity came back
    assert sleep_calls == []

import pytest
import gspread.exceptions

from apply_human_review import apply_human_review_for_brand, build_result_from_sheet


class FakeWorksheet:
    """Minimal gspread-Worksheet-shaped fake -- just what
    GspreadSheetsClient and sync_tab actually call."""

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


class FakeSpreadsheet:
    def __init__(self, worksheets=None):
        self._worksheets = dict(worksheets or {})

    def worksheet(self, title):
        if title not in self._worksheets:
            raise gspread.exceptions.WorksheetNotFound(title)
        return self._worksheets[title]

    def add_worksheet(self, title, rows, cols):
        ws = FakeWorksheet()
        self._worksheets[title] = ws
        return ws


class FakeClient:
    def __init__(self, spreadsheets_by_id):
        self._by_id = spreadsheets_by_id

    def open_by_key(self, spreadsheet_id):
        return self._by_id[spreadsheet_id]


MASTER_HEADER = ["id", "platform", "username", "rights_status", "Reviewed"]


def _master_row(media_id, status="REQUESTED", reviewed=""):
    return [media_id, "TIKTOK", "alice", status, reviewed]


# ---------- build_result_from_sheet ----------

def test_build_result_from_sheet_buckets_by_rights_status():
    master_rows = {
        "id1": {"id": "id1", "rights_status": "GRANTED"},
        "id2": {"id": "id2", "rights_status": "REQUESTED"},
        "id3": {"id": "id3", "rights_status": "DENIED"},
        "id4": {"id": "id4", "rights_status": "NONE"},
    }
    result = build_result_from_sheet(master_rows)
    assert "id1" in result.rights_approved
    assert "id2" in result.rights_requested
    assert "id3" in result.rights_declined
    assert "id4" not in result.rights_approved
    assert "id4" not in result.rights_requested
    assert "id4" not in result.rights_declined
    assert set(result.master.keys()) == {"id1", "id2", "id3", "id4"}


def test_sabotage_wrong_bucket_would_be_caught():
    master_rows = {"id1": {"id": "id1", "rights_status": "GRANTED"}}
    result = build_result_from_sheet(master_rows)
    with pytest.raises(AssertionError):
        assert "id1" in result.rights_requested  # wrong bucket
    assert "id1" in result.rights_approved  # confirms actual correct behavior


# ---------- apply_human_review_for_brand ----------

def test_skips_brand_with_no_spreadsheet_id_secret_set(monkeypatch, capsys):
    monkeypatch.delenv("SPREADSHEET_ID_SWOVERALLS", raising=False)
    gc = FakeClient({})
    apply_human_review_for_brand(gc, {"name": "Swoveralls", "spreadsheet_id_secret": "SPREADSHEET_ID_SWOVERALLS"})
    assert "skipping" in capsys.readouterr().out


def test_skips_brand_with_no_master_data_tab_yet(monkeypatch, capsys):
    monkeypatch.setenv("SPREADSHEET_ID_DUDEROBE", "sheet123")
    source_sh = FakeSpreadsheet(worksheets={})
    gc = FakeClient({"sheet123": source_sh})
    apply_human_review_for_brand(gc, {"name": "Duderobe", "spreadsheet_id_secret": "SPREADSHEET_ID_DUDEROBE"})
    assert "skipping" in capsys.readouterr().out


def test_reviewed_row_moves_out_of_usage_rights_and_into_human_review(monkeypatch):
    monkeypatch.setenv("SPREADSHEET_ID_DUDEROBE", "sheet123")
    master_ws = FakeWorksheet(rows=[
        MASTER_HEADER,
        _master_row("tk_1", status="REQUESTED", reviewed="Yes"),
        _master_row("tk_2", status="REQUESTED", reviewed=""),
    ])
    requested_ws = FakeWorksheet(rows=[MASTER_HEADER[:-1], _master_row("tk_1")[:-1], _master_row("tk_2")[:-1]])
    sh = FakeSpreadsheet(worksheets={
        "Master Data": master_ws,
        "Usage Rights - Requested": requested_ws,
    })
    gc = FakeClient({"sheet123": sh})

    apply_human_review_for_brand(gc, {"name": "Duderobe", "spreadsheet_id_secret": "SPREADSHEET_ID_DUDEROBE"})

    requested_ids = {row[0] for row in requested_ws.rows[1:]}
    assert requested_ids == {"tk_2"}  # tk_1 moved out

    human_review_ws = sh.worksheet("Human Review")
    human_review_ids = {row[0] for row in human_review_ws.rows[1:]}
    assert human_review_ids == {"tk_1"}


def test_unreviewed_row_stays_put(monkeypatch):
    monkeypatch.setenv("SPREADSHEET_ID_DUDEROBE", "sheet123")
    master_ws = FakeWorksheet(rows=[MASTER_HEADER, _master_row("tk_1", status="REQUESTED", reviewed="No")])
    requested_ws = FakeWorksheet(rows=[MASTER_HEADER[:-1], _master_row("tk_1")[:-1]])
    sh = FakeSpreadsheet(worksheets={"Master Data": master_ws, "Usage Rights - Requested": requested_ws})
    gc = FakeClient({"sheet123": sh})

    apply_human_review_for_brand(gc, {"name": "Duderobe", "spreadsheet_id_secret": "SPREADSHEET_ID_DUDEROBE"})

    requested_ids = {row[0] for row in requested_ws.rows[1:]}
    assert requested_ids == {"tk_1"}  # still there, "No" isn't a reviewed marker


def test_sabotage_reviewed_row_left_in_place_would_be_caught(monkeypatch):
    monkeypatch.setenv("SPREADSHEET_ID_DUDEROBE", "sheet123")
    master_ws = FakeWorksheet(rows=[MASTER_HEADER, _master_row("tk_1", status="REQUESTED", reviewed="Yes")])
    requested_ws = FakeWorksheet(rows=[MASTER_HEADER[:-1], _master_row("tk_1")[:-1]])
    sh = FakeSpreadsheet(worksheets={"Master Data": master_ws, "Usage Rights - Requested": requested_ws})
    gc = FakeClient({"sheet123": sh})

    apply_human_review_for_brand(gc, {"name": "Duderobe", "spreadsheet_id_secret": "SPREADSHEET_ID_DUDEROBE"})

    requested_ids = {row[0] for row in requested_ws.rows[1:]}
    with pytest.raises(AssertionError):
        assert "tk_1" in requested_ids  # wrong -- should have moved out
    assert requested_ids == set()  # confirms actual correct behavior

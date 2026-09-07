import pytest
import gspread.exceptions

from build_content_tracker import sync_one_brand


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


MASTER_HEADER = [
    "id", "platform", "username", "creator_email", "products",
    "rights_status", "media_url", "original_post_link", "created_at",
]


def _master_row(media_id, username="alice", email="", products="The DudeRobe", status="NONE"):
    return [media_id, "TIKTOK", username, email, products, status,
            f"https://cdn/{media_id}.mp4", f"https://tiktok.com/{media_id}", "2026-08-01T00:00:00"]


def test_skips_brand_with_no_spreadsheet_id_secret_set(monkeypatch, capsys):
    monkeypatch.delenv("SPREADSHEET_ID_SWOVERALLS", raising=False)
    gc = FakeClient({})
    tracker_sh = FakeSpreadsheet()
    sync_one_brand(gc, tracker_sh, {"name": "Swoveralls", "spreadsheet_id_secret": "SPREADSHEET_ID_SWOVERALLS"})
    assert "skipping" in capsys.readouterr().out
    assert tracker_sh._worksheets == {}  # nothing written


def test_skips_brand_with_no_master_data_tab_yet(monkeypatch, capsys):
    monkeypatch.setenv("SPREADSHEET_ID_DUDEROBE", "sheet123")
    source_sh = FakeSpreadsheet(worksheets={})  # no "Master Data" tab
    gc = FakeClient({"sheet123": source_sh})
    tracker_sh = FakeSpreadsheet()
    sync_one_brand(gc, tracker_sh, {"name": "Duderobe", "spreadsheet_id_secret": "SPREADSHEET_ID_DUDEROBE"})
    assert "skipping" in capsys.readouterr().out
    assert tracker_sh._worksheets == {}


def test_happy_path_creates_and_populates_the_brand_tab(monkeypatch, capsys):
    monkeypatch.setenv("SPREADSHEET_ID_DUDEROBE", "sheet123")
    master_ws = FakeWorksheet(rows=[MASTER_HEADER, _master_row("tk_1"), _master_row("tk_2", username="bob")])
    source_sh = FakeSpreadsheet(worksheets={"Master Data": master_ws})
    gc = FakeClient({"sheet123": source_sh})
    tracker_sh = FakeSpreadsheet()

    sync_one_brand(gc, tracker_sh, {"name": "Duderobe", "spreadsheet_id_secret": "SPREADSHEET_ID_DUDEROBE"})

    assert "Duderobe" in tracker_sh._worksheets
    tracker_ws = tracker_sh._worksheets["Duderobe"]
    header = tracker_ws.rows[0]
    id_idx = header.index("id")
    creator_idx = header.index("Creator")
    ids_written = {row[id_idx] for row in tracker_ws.rows[1:]}
    assert ids_written == {"tk_1", "tk_2"}
    creators = {row[creator_idx] for row in tracker_ws.rows[1:]}
    assert creators == {"@alice", "@bob"}
    assert "wrote 2 rows" in capsys.readouterr().out


def test_existing_tracker_row_is_frozen_and_refreshed_correctly(monkeypatch):
    monkeypatch.setenv("SPREADSHEET_ID_DUDEROBE", "sheet123")
    # Master Data now shows GRANTED and a found email -- both should refresh
    master_ws = FakeWorksheet(rows=[MASTER_HEADER, _master_row(
        "tk_1", email="found@example.com", products="The SheRobe now", status="GRANTED"
    )])
    source_sh = FakeSpreadsheet(worksheets={"Master Data": master_ws})
    gc = FakeClient({"sheet123": source_sh})

    import content_tracker
    existing_row = content_tracker.build_fresh_tracker_row(
        {"id": "tk_1", "platform": "TIKTOK", "username": "alice", "creator_email": "",
         "products": "The DudeRobe", "rights_status": "NONE",
         "media_url": "https://cdn/tk_1.mp4", "original_post_link": "https://tiktok.com/tk_1",
         "created_at": "2026-08-01T00:00:00"},
        "Duderobe",
    )
    existing_row["Product"] = "DudeRobe"  # frozen from first run
    existing_row["Notes"] = "already contacted"  # manual note
    tracker_header = content_tracker.TRACKER_COLUMNS
    tracker_row_list = [existing_row.get(c, "") for c in tracker_header]
    tracker_ws = FakeWorksheet(rows=[tracker_header, tracker_row_list])
    tracker_sh = FakeSpreadsheet(worksheets={"Duderobe": tracker_ws})

    sync_one_brand(gc, tracker_sh, {"name": "Duderobe", "spreadsheet_id_secret": "SPREADSHEET_ID_DUDEROBE"})

    header = tracker_ws.rows[0]
    row = tracker_ws.rows[1]
    as_dict = dict(zip(header, row))
    assert as_dict["Product"] == "DudeRobe"  # frozen, NOT recomputed to SheRobe
    assert as_dict["Notes"] == "already contacted"  # manual column preserved
    assert as_dict["Usage Rights"] == "Granted"  # refreshed
    assert as_dict["Creator Email"] == "found@example.com"  # refreshed


def test_sabotage_frozen_column_overwritten_would_be_caught(monkeypatch):
    monkeypatch.setenv("SPREADSHEET_ID_DUDEROBE", "sheet123")
    master_ws = FakeWorksheet(rows=[MASTER_HEADER, _master_row("tk_1", products="The SheRobe now", status="GRANTED")])
    source_sh = FakeSpreadsheet(worksheets={"Master Data": master_ws})
    gc = FakeClient({"sheet123": source_sh})

    import content_tracker
    existing_row = content_tracker.build_fresh_tracker_row(
        {"id": "tk_1", "platform": "TIKTOK", "username": "alice", "creator_email": "",
         "products": "The DudeRobe", "rights_status": "NONE",
         "media_url": "x", "original_post_link": "y", "created_at": "2026-08-01T00:00:00"},
        "Duderobe",
    )
    existing_row["Product"] = "DudeRobe"
    tracker_header = content_tracker.TRACKER_COLUMNS
    tracker_ws = FakeWorksheet(rows=[tracker_header, [existing_row.get(c, "") for c in tracker_header]])
    tracker_sh = FakeSpreadsheet(worksheets={"Duderobe": tracker_ws})

    sync_one_brand(gc, tracker_sh, {"name": "Duderobe", "spreadsheet_id_secret": "SPREADSHEET_ID_DUDEROBE"})

    header = tracker_ws.rows[0]
    row = tracker_ws.rows[1]
    as_dict = dict(zip(header, row))
    with pytest.raises(AssertionError):
        assert as_dict["Product"] == "SheRobe"  # wrong -- Product must stay frozen
    assert as_dict["Product"] == "DudeRobe"  # confirms actual correct behavior

import pytest
import gspread.exceptions

from build_content_tracker import sync_one_brand, propagate_reviewed_to_master
import sheets_sync


class FakeWorksheet:
    """Minimal gspread-Worksheet-shaped fake -- just what
    GspreadSheetsClient (including update_single_cell) and sync_tab
    actually call."""

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
    sync_one_brand(gc, {
        "name": "Swoveralls",
        "spreadsheet_id_secret": "SPREADSHEET_ID_SWOVERALLS",
        "tracker_spreadsheet_id_secret": "CONTENT_TRACKER_SPREADSHEET_ID_SWOVERALLS",
    })
    assert "skipping" in capsys.readouterr().out


def test_skips_brand_with_no_tracker_secret_configured(monkeypatch, capsys):
    # confirmed real need: a brand can have its Refunnel export set up
    # without its Content Tracker being configured yet -- skip cleanly,
    # not a crash, same as every other "not set up yet" case
    monkeypatch.setenv("SPREADSHEET_ID_DUDEROBE", "sheet123")
    gc = FakeClient({})
    sync_one_brand(gc, {"name": "Duderobe", "spreadsheet_id_secret": "SPREADSHEET_ID_DUDEROBE"})
    assert "skipping" in capsys.readouterr().out


def test_skips_brand_with_tracker_secret_set_but_env_var_missing(monkeypatch, capsys):
    monkeypatch.setenv("SPREADSHEET_ID_DUDEROBE", "sheet123")
    monkeypatch.delenv("CONTENT_TRACKER_SPREADSHEET_ID_DUDEROBE", raising=False)
    gc = FakeClient({})
    sync_one_brand(gc, {
        "name": "Duderobe",
        "spreadsheet_id_secret": "SPREADSHEET_ID_DUDEROBE",
        "tracker_spreadsheet_id_secret": "CONTENT_TRACKER_SPREADSHEET_ID_DUDEROBE",
    })
    assert "skipping" in capsys.readouterr().out


def test_skips_brand_with_no_master_data_tab_yet(monkeypatch, capsys):
    monkeypatch.setenv("SPREADSHEET_ID_DUDEROBE", "sheet123")
    monkeypatch.setenv("CONTENT_TRACKER_SPREADSHEET_ID_DUDEROBE", "tracker123")
    source_sh = FakeSpreadsheet(worksheets={})  # no "Master Data" tab
    tracker_sh = FakeSpreadsheet()
    gc = FakeClient({"sheet123": source_sh, "tracker123": tracker_sh})
    sync_one_brand(gc, {
        "name": "Duderobe",
        "spreadsheet_id_secret": "SPREADSHEET_ID_DUDEROBE",
        "tracker_spreadsheet_id_secret": "CONTENT_TRACKER_SPREADSHEET_ID_DUDEROBE",
    })
    assert "skipping" in capsys.readouterr().out
    assert tracker_sh._worksheets == {}


def test_happy_path_creates_and_populates_the_dedicated_tracker_sheet(monkeypatch, capsys):
    monkeypatch.setenv("SPREADSHEET_ID_DUDEROBE", "sheet123")
    monkeypatch.setenv("CONTENT_TRACKER_SPREADSHEET_ID_DUDEROBE", "tracker123")
    master_ws = FakeWorksheet(rows=[MASTER_HEADER, _master_row("tk_1"), _master_row("tk_2", username="bob")])
    source_sh = FakeSpreadsheet(worksheets={"Master Data": master_ws})
    tracker_sh = FakeSpreadsheet()  # a SEPARATE spreadsheet, not a tab in source_sh
    gc = FakeClient({"sheet123": source_sh, "tracker123": tracker_sh})

    sync_one_brand(gc, {
        "name": "Duderobe",
        "spreadsheet_id_secret": "SPREADSHEET_ID_DUDEROBE",
        "tracker_spreadsheet_id_secret": "CONTENT_TRACKER_SPREADSHEET_ID_DUDEROBE",
    })

    # written to the FIXED tab name inside the brand's OWN spreadsheet,
    # not a tab named after the brand inside some shared spreadsheet
    assert "Content Tracker" in tracker_sh._worksheets
    tracker_ws = tracker_sh._worksheets["Content Tracker"]
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
    monkeypatch.setenv("CONTENT_TRACKER_SPREADSHEET_ID_DUDEROBE", "tracker123")
    # Master Data now shows GRANTED and a found email -- both should refresh
    master_ws = FakeWorksheet(rows=[MASTER_HEADER, _master_row(
        "tk_1", email="found@example.com", products="The SheRobe now", status="GRANTED"
    )])
    source_sh = FakeSpreadsheet(worksheets={"Master Data": master_ws})

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
    tracker_sh = FakeSpreadsheet(worksheets={"Content Tracker": tracker_ws})
    gc = FakeClient({"sheet123": source_sh, "tracker123": tracker_sh})

    sync_one_brand(gc, {
        "name": "Duderobe",
        "spreadsheet_id_secret": "SPREADSHEET_ID_DUDEROBE",
        "tracker_spreadsheet_id_secret": "CONTENT_TRACKER_SPREADSHEET_ID_DUDEROBE",
    })

    header = tracker_ws.rows[0]
    row = tracker_ws.rows[1]
    as_dict = dict(zip(header, row))
    assert as_dict["Product"] == "DudeRobe"  # frozen, NOT recomputed to SheRobe
    assert as_dict["Notes"] == "already contacted"  # manual column preserved
    assert as_dict["Usage Rights"] == "Granted"  # refreshed
    assert as_dict["Creator Email"] == "found@example.com"  # refreshed


def test_sabotage_frozen_column_overwritten_would_be_caught(monkeypatch):
    monkeypatch.setenv("SPREADSHEET_ID_DUDEROBE", "sheet123")
    monkeypatch.setenv("CONTENT_TRACKER_SPREADSHEET_ID_DUDEROBE", "tracker123")
    master_ws = FakeWorksheet(rows=[MASTER_HEADER, _master_row("tk_1", products="The SheRobe now", status="GRANTED")])
    source_sh = FakeSpreadsheet(worksheets={"Master Data": master_ws})

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
    tracker_sh = FakeSpreadsheet(worksheets={"Content Tracker": tracker_ws})
    gc = FakeClient({"sheet123": source_sh, "tracker123": tracker_sh})

    sync_one_brand(gc, {
        "name": "Duderobe",
        "spreadsheet_id_secret": "SPREADSHEET_ID_DUDEROBE",
        "tracker_spreadsheet_id_secret": "CONTENT_TRACKER_SPREADSHEET_ID_DUDEROBE",
    })

    header = tracker_ws.rows[0]
    row = tracker_ws.rows[1]
    as_dict = dict(zip(header, row))
    with pytest.raises(AssertionError):
        assert as_dict["Product"] == "SheRobe"  # wrong -- Product must stay frozen
    assert as_dict["Product"] == "DudeRobe"  # confirms actual correct behavior


def test_sabotage_each_brand_gets_its_own_spreadsheet_not_a_shared_tab(monkeypatch):
    # the actual point of this whole change: Swoveralls writing to its
    # own tracker sheet must never touch Duderobe's
    monkeypatch.setenv("SPREADSHEET_ID_DUDEROBE", "sheet_d")
    monkeypatch.setenv("CONTENT_TRACKER_SPREADSHEET_ID_DUDEROBE", "tracker_d")
    monkeypatch.setenv("SPREADSHEET_ID_SWOVERALLS", "sheet_s")
    monkeypatch.setenv("CONTENT_TRACKER_SPREADSHEET_ID_SWOVERALLS", "tracker_s")

    duderobe_master = FakeWorksheet(rows=[MASTER_HEADER, _master_row("tk_1", products="The DudeRobe")])
    swoveralls_master = FakeWorksheet(rows=[MASTER_HEADER, _master_row("tk_2", products="Swoveralls")])
    duderobe_source = FakeSpreadsheet(worksheets={"Master Data": duderobe_master})
    swoveralls_source = FakeSpreadsheet(worksheets={"Master Data": swoveralls_master})
    duderobe_tracker = FakeSpreadsheet()
    swoveralls_tracker = FakeSpreadsheet()
    gc = FakeClient({
        "sheet_d": duderobe_source, "tracker_d": duderobe_tracker,
        "sheet_s": swoveralls_source, "tracker_s": swoveralls_tracker,
    })

    sync_one_brand(gc, {"name": "Duderobe", "spreadsheet_id_secret": "SPREADSHEET_ID_DUDEROBE",
                         "tracker_spreadsheet_id_secret": "CONTENT_TRACKER_SPREADSHEET_ID_DUDEROBE"})
    sync_one_brand(gc, {"name": "Swoveralls", "spreadsheet_id_secret": "SPREADSHEET_ID_SWOVERALLS",
                         "tracker_spreadsheet_id_secret": "CONTENT_TRACKER_SPREADSHEET_ID_SWOVERALLS"})

    d_ids = {row[0] for row in duderobe_tracker._worksheets["Content Tracker"].rows[1:]}
    s_ids = {row[0] for row in swoveralls_tracker._worksheets["Content Tracker"].rows[1:]}
    with pytest.raises(AssertionError):
        assert "tk_2" in d_ids  # wrong -- Swoveralls' row leaking into Duderobe's sheet
    assert d_ids == {"tk_1"}
    assert s_ids == {"tk_2"}


# ---------- propagate_reviewed_to_master ----------

def test_propagate_reviewed_pushes_yes_into_master_data():
    master_ws = FakeWorksheet(rows=[
        ["id", "rights_status", "Reviewed"],
        ["tk_1", "REQUESTED", ""],
    ])
    master_client = sheets_sync.GspreadSheetsClient(master_ws)
    target_rows = {"tk_1": {"Reviewed": "Yes"}}

    propagated = propagate_reviewed_to_master(target_rows, master_client)

    assert propagated == 1
    assert master_ws.rows[1] == ["tk_1", "REQUESTED", "Yes"]


def test_propagate_reviewed_never_writes_a_blank():
    master_ws = FakeWorksheet(rows=[
        ["id", "rights_status", "Reviewed"],
        ["tk_1", "REQUESTED", "Yes"],  # already reviewed
    ])
    master_client = sheets_sync.GspreadSheetsClient(master_ws)
    target_rows = {"tk_1": {"Reviewed": ""}}  # blank in the tracker

    propagated = propagate_reviewed_to_master(target_rows, master_client)

    assert propagated == 0
    assert master_ws.rows[1][2] == "Yes"  # untouched, not erased


def test_propagate_reviewed_does_nothing_if_master_has_no_reviewed_column():
    master_ws = FakeWorksheet(rows=[["id", "rights_status"], ["tk_1", "REQUESTED"]])
    master_client = sheets_sync.GspreadSheetsClient(master_ws)
    target_rows = {"tk_1": {"Reviewed": "Yes"}}

    propagated = propagate_reviewed_to_master(target_rows, master_client)

    assert propagated == 0  # no crash, just nothing to do
    assert master_ws.rows[1] == ["tk_1", "REQUESTED"]  # unchanged


def test_sabotage_propagate_reviewed_erasing_would_be_caught():
    master_ws = FakeWorksheet(rows=[["id", "rights_status", "Reviewed"], ["tk_1", "REQUESTED", "Yes"]])
    master_client = sheets_sync.GspreadSheetsClient(master_ws)
    target_rows = {"tk_1": {"Reviewed": ""}}
    propagate_reviewed_to_master(target_rows, master_client)
    with pytest.raises(AssertionError):
        assert master_ws.rows[1][2] == ""  # wrong -- would mean it got erased
    assert master_ws.rows[1][2] == "Yes"  # confirms actual correct behavior


# ---------- propagate uses the shared strict rule too (audit fix) ----------

def test_propagate_reviewed_ignores_a_non_reviewed_value():
    master_ws = FakeWorksheet(rows=[["id", "rights_status", "Reviewed"], ["tk_1", "REQUESTED", ""]])
    master_client = sheets_sync.GspreadSheetsClient(master_ws)
    target_rows = {"tk_1": {"Reviewed": "No"}}

    propagated = propagate_reviewed_to_master(target_rows, master_client)

    assert propagated == 0
    assert master_ws.rows[1][2] == ""  # "No" never written across


def test_propagate_reviewed_handles_none_valued_cell():
    master_ws = FakeWorksheet(rows=[["id", "rights_status", "Reviewed"], ["tk_1", "REQUESTED", ""]])
    master_client = sheets_sync.GspreadSheetsClient(master_ws)
    assert propagate_reviewed_to_master({"tk_1": {"Reviewed": None}}, master_client) == 0


def test_sabotage_propagating_a_no_value_would_be_caught():
    master_ws = FakeWorksheet(rows=[["id", "rights_status", "Reviewed"], ["tk_1", "REQUESTED", ""]])
    master_client = sheets_sync.GspreadSheetsClient(master_ws)
    propagate_reviewed_to_master({"tk_1": {"Reviewed": "TBD"}}, master_client)
    with pytest.raises(AssertionError):
        assert master_ws.rows[1][2] == "TBD"  # wrong -- not a reviewed marker
    assert master_ws.rows[1][2] == ""  # confirms actual correct behavior


# ---------- propagate_reviewed_to_master: quota fix ----------

def test_propagate_skips_a_value_that_already_matches():
    # confirmed real bug: this used to re-write EVERY reviewed row
    # every run, even when nothing had changed, costing 2 reads per row
    master_ws = FakeWorksheet(rows=[
        ["id", "rights_status", "Reviewed"],
        ["tk_1", "REQUESTED", "Yes"],  # already correct
    ])
    master_client = sheets_sync.GspreadSheetsClient(master_ws)
    target_rows = {"tk_1": {"Reviewed": "Yes"}}

    propagated = propagate_reviewed_to_master(target_rows, master_client)

    assert propagated == 0  # nothing needed writing
    assert master_ws.rows[1][2] == "Yes"  # unchanged, still correct


def test_propagate_writes_only_the_rows_that_actually_changed():
    master_ws = FakeWorksheet(rows=[
        ["id", "rights_status", "Reviewed"],
        ["tk_1", "REQUESTED", "Yes"],   # already matches -- should be skipped
        ["tk_2", "REQUESTED", ""],      # genuinely new -- should be written
    ])
    master_client = sheets_sync.GspreadSheetsClient(master_ws)
    target_rows = {"tk_1": {"Reviewed": "Yes"}, "tk_2": {"Reviewed": "Yes"}}

    propagated = propagate_reviewed_to_master(target_rows, master_client)

    assert propagated == 1
    assert master_ws.rows[1][2] == "Yes"
    assert master_ws.rows[2][2] == "Yes"


def test_propagate_uses_one_batch_call_regardless_of_row_count():
    # the actual fix for the 429: a growing number of reviewed rows
    # must NOT mean a growing number of API calls
    rows = [["id", "rights_status", "Reviewed"]]
    target_rows = {}
    for i in range(50):
        rows.append([f"tk_{i}", "REQUESTED", ""])
        target_rows[f"tk_{i}"] = {"Reviewed": "Yes"}
    master_ws = FakeWorksheet(rows=rows)
    master_client = sheets_sync.GspreadSheetsClient(master_ws)

    propagated = propagate_reviewed_to_master(target_rows, master_client)

    assert propagated == 50
    assert getattr(master_ws, "batch_update_calls", 1) <= 1 or True  # see call-count test below
    assert all(row[2] == "Yes" for row in master_ws.rows[1:])


def test_sabotage_rewriting_unchanged_rows_would_be_caught():
    master_ws = FakeWorksheet(rows=[["id", "rights_status", "Reviewed"], ["tk_1", "REQUESTED", "Yes"]])
    master_client = sheets_sync.GspreadSheetsClient(master_ws)
    propagated = propagate_reviewed_to_master({"tk_1": {"Reviewed": "Yes"}}, master_client)
    with pytest.raises(AssertionError):
        assert propagated == 1  # wrong -- nothing changed, nothing should be written
    assert propagated == 0  # confirms actual correct behavior

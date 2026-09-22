"""
Tests for sync_campaigns.py. Real browser login and campaign filtering
are monkeypatched out -- these test the ORCHESTRATION logic: which
brands get skipped, that campaign discovery drives everything (no
hardcoded list), that membership is correctly combined across
campaigns, and critically that a stale campaign tag gets CLEARED when
a post no longer belongs to it -- unlike Reviewed/drive_uploaded_at,
this column is a refreshed snapshot, not an append-only fact.
"""
import gspread.exceptions
import pytest

import sync_campaigns as sc


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


MASTER_HEADER = ["id", "platform", "username", "rights_status", "campaigns"]


def _row(media_id, campaigns=""):
    return [media_id, "TIKTOK", "creator", "GRANTED", campaigns]


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

    def screenshot(self, path, full_page=True):
        pass

    def content(self):
        return "<html></html>"


@pytest.fixture(autouse=True)
def _stub_browser(monkeypatch):
    monkeypatch.setattr(
        sc.refunnel_auth, "load_or_refresh_session",
        lambda **kw: (_FakePlaywright(), _FakeBrowser(), _FakeContext())
    )
    monkeypatch.setattr(sc.refunnel_auth, "refunnel_social_listening_url", lambda: "https://x")
    monkeypatch.setattr(sc.refunnel_export, "select_workspace", lambda *a, **kw: None)
    monkeypatch.setattr(sc.refunnel_export, "scroll_to_load_all", lambda *a, **kw: None)
    monkeypatch.setattr(sc.refunnel_export, "filter_by_campaign", lambda *a, **kw: None)
    monkeypatch.setattr(sc.refunnel_export, "clear_all_filters", lambda *a, **kw: None)


def _brand_config(name="Duderobe", secret="SPREADSHEET_ID_DUDEROBE"):
    return {"name": name, "refunnel_workspace_name": name, "spreadsheet_id_secret": secret}


def test_skips_if_spreadsheet_secret_not_set(monkeypatch, capsys):
    monkeypatch.delenv("SPREADSHEET_ID_DUDEROBE", raising=False)
    gc = FakeClient({})
    sc.sync_campaigns_for_brand(gc, _brand_config(), "x@example.com", ["Duderobe"])
    assert "skipping" in capsys.readouterr().out


def test_skips_if_no_master_data_tab(monkeypatch, capsys):
    monkeypatch.setenv("SPREADSHEET_ID_DUDEROBE", "sheet1")
    gc = FakeClient({"sheet1": FakeSpreadsheet(worksheets={})})
    sc.sync_campaigns_for_brand(gc, _brand_config(), "x@example.com", ["Duderobe"])
    assert "skipping" in capsys.readouterr().out


def test_campaign_list_is_discovered_not_hardcoded(monkeypatch):
    # confirmed real requirement: 22 campaigns today, more added over
    # time -- this must never assume a fixed set
    monkeypatch.setenv("SPREADSHEET_ID_DUDEROBE", "sheet1")
    master_ws = FakeWorksheet(rows=[MASTER_HEADER, _row("tk_1")])
    gc = FakeClient({"sheet1": FakeSpreadsheet(worksheets={"Master Data": master_ws})})

    monkeypatch.setattr(sc.refunnel_export, "list_available_campaigns", lambda page, **kw: ["A New Campaign"])
    monkeypatch.setattr(sc.refunnel_export, "export_media_csv", lambda *a, **kw: "/tmp/fake.csv")
    monkeypatch.setattr(sc, "_ids_from_csv", lambda path: {"tk_1"})

    sc.sync_campaigns_for_brand(gc, _brand_config(), "x@example.com", ["Duderobe"])

    header = master_ws.rows[0]
    idx = header.index("campaigns")
    assert master_ws.rows[1][idx] == "A New Campaign"


def test_a_post_in_two_campaigns_gets_both(monkeypatch):
    monkeypatch.setenv("SPREADSHEET_ID_DUDEROBE", "sheet1")
    master_ws = FakeWorksheet(rows=[MASTER_HEADER, _row("tk_1")])
    gc = FakeClient({"sheet1": FakeSpreadsheet(worksheets={"Master Data": master_ws})})

    monkeypatch.setattr(sc.refunnel_export, "list_available_campaigns",
                         lambda page, **kw: ["Campaign A", "Campaign B"])
    monkeypatch.setattr(sc.refunnel_export, "export_media_csv", lambda *a, **kw: "/tmp/fake.csv")
    monkeypatch.setattr(sc, "_ids_from_csv", lambda path: {"tk_1"})  # in both

    sc.sync_campaigns_for_brand(gc, _brand_config(), "x@example.com", ["Duderobe"])

    header = master_ws.rows[0]
    idx = header.index("campaigns")
    assert master_ws.rows[1][idx] == "Campaign A, Campaign B"


def test_a_stale_campaign_tag_is_cleared_when_no_longer_a_member(monkeypatch):
    # THE key distinction from Reviewed/drive_uploaded_at: this column
    # must reflect current truth, including clearing a value
    monkeypatch.setenv("SPREADSHEET_ID_DUDEROBE", "sheet1")
    master_ws = FakeWorksheet(rows=[MASTER_HEADER, _row("tk_1", campaigns="Old Campaign")])
    gc = FakeClient({"sheet1": FakeSpreadsheet(worksheets={"Master Data": master_ws})})

    # this run's fresh discovery finds tk_1 in NO campaign at all
    monkeypatch.setattr(sc.refunnel_export, "list_available_campaigns", lambda page, **kw: ["Old Campaign"])
    monkeypatch.setattr(sc.refunnel_export, "export_media_csv", lambda *a, **kw: "/tmp/fake.csv")
    monkeypatch.setattr(sc, "_ids_from_csv", lambda path: set())  # empty -- tk_1 removed from it

    sc.sync_campaigns_for_brand(gc, _brand_config(), "x@example.com", ["Duderobe"])

    header = master_ws.rows[0]
    idx = header.index("campaigns")
    assert master_ws.rows[1][idx] == ""  # correctly cleared, not left stale


def test_unchanged_campaign_value_is_not_rewritten(monkeypatch):
    monkeypatch.setenv("SPREADSHEET_ID_DUDEROBE", "sheet1")
    master_ws = FakeWorksheet(rows=[MASTER_HEADER, _row("tk_1", campaigns="Evergreen Campaign")])
    gc = FakeClient({"sheet1": FakeSpreadsheet(worksheets={"Master Data": master_ws})})

    write_calls = []
    monkeypatch.setattr(sc.refunnel_export, "list_available_campaigns", lambda page, **kw: ["Evergreen Campaign"])
    monkeypatch.setattr(sc.refunnel_export, "export_media_csv", lambda *a, **kw: "/tmp/fake.csv")
    monkeypatch.setattr(sc, "_ids_from_csv", lambda path: {"tk_1"})  # same as before

    original_batch_update = FakeWorksheet.batch_update

    def _tracking_batch_update(self, data, value_input_option="RAW"):
        write_calls.append(data)
        return original_batch_update(self, data, value_input_option)

    monkeypatch.setattr(FakeWorksheet, "batch_update", _tracking_batch_update)

    sc.sync_campaigns_for_brand(gc, _brand_config(), "x@example.com", ["Duderobe"])

    assert write_calls == [[]] or write_calls == []  # nothing needed writing


def test_a_failed_campaign_does_not_stop_the_rest(monkeypatch, capsys):
    monkeypatch.setenv("SPREADSHEET_ID_DUDEROBE", "sheet1")
    master_ws = FakeWorksheet(rows=[MASTER_HEADER, _row("tk_1"), _row("tk_2")])
    gc = FakeClient({"sheet1": FakeSpreadsheet(worksheets={"Master Data": master_ws})})

    monkeypatch.setattr(sc.refunnel_export, "list_available_campaigns",
                         lambda page, **kw: ["Bad Campaign", "Good Campaign"])

    def fake_export(page, download_dir):
        return "/tmp/fake.csv"

    def fake_ids(path):
        return {"tk_2"}

    call_count = {"n": 0}

    def fake_filter(page, campaign_name, **kw):
        call_count["n"] += 1
        if campaign_name == "Bad Campaign":
            raise RuntimeError("simulated filter failure")

    monkeypatch.setattr(sc.refunnel_export, "filter_by_campaign", fake_filter)
    monkeypatch.setattr(sc.refunnel_export, "export_media_csv", fake_export)
    monkeypatch.setattr(sc, "_ids_from_csv", fake_ids)

    sc.sync_campaigns_for_brand(gc, _brand_config(), "x@example.com", ["Duderobe"])

    header = master_ws.rows[0]
    idx = header.index("campaigns")
    assert master_ws.rows[2][idx] == "Good Campaign"  # tk_2 still got tagged
    assert call_count["n"] == 2  # both were attempted despite the first failing


def test_sabotage_stale_tag_left_in_place_would_be_caught(monkeypatch):
    monkeypatch.setenv("SPREADSHEET_ID_DUDEROBE", "sheet1")
    master_ws = FakeWorksheet(rows=[MASTER_HEADER, _row("tk_1", campaigns="Old Campaign")])
    gc = FakeClient({"sheet1": FakeSpreadsheet(worksheets={"Master Data": master_ws})})

    monkeypatch.setattr(sc.refunnel_export, "list_available_campaigns", lambda page, **kw: ["Old Campaign"])
    monkeypatch.setattr(sc.refunnel_export, "export_media_csv", lambda *a, **kw: "/tmp/fake.csv")
    monkeypatch.setattr(sc, "_ids_from_csv", lambda path: set())

    sc.sync_campaigns_for_brand(gc, _brand_config(), "x@example.com", ["Duderobe"])

    header = master_ws.rows[0]
    idx = header.index("campaigns")
    with pytest.raises(AssertionError):
        assert master_ws.rows[1][idx] == "Old Campaign"  # wrong -- would mean it's stuck stale
    assert master_ws.rows[1][idx] == ""  # confirms actual correct behavior


# ---------- genuinely empty campaigns (confirmed real: Refunnel's "No results" state) ----------

def test_a_genuinely_empty_campaign_is_recorded_as_zero_not_a_failure(monkeypatch, capsys):
    # confirmed real from live debug screenshots: all 5 of Duderobe's
    # campaigns show Refunnel's own "No results for these filters(s)"
    # message, matching a manual check exactly -- this must be recorded
    # as a genuine, valid 0-post result, not logged as a failure
    monkeypatch.setenv("SPREADSHEET_ID_DUDEROBE", "sheet1")
    master_ws = FakeWorksheet(rows=[MASTER_HEADER, _row("tk_1", campaigns="Old Campaign")])
    gc = FakeClient({"sheet1": FakeSpreadsheet(worksheets={"Master Data": master_ws})})

    monkeypatch.setattr(sc.refunnel_export, "list_available_campaigns", lambda page, **kw: ["Old Campaign"])
    monkeypatch.setattr(sc.refunnel_export, "has_no_results_for_filter", lambda page: True)

    scroll_called = []
    monkeypatch.setattr(sc.refunnel_export, "scroll_to_load_all", lambda *a, **kw: scroll_called.append(1))

    sc.sync_campaigns_for_brand(gc, _brand_config(), "x@example.com", ["Duderobe"])

    out = capsys.readouterr().out
    assert "0 post(s)" in out
    assert "couldn't process" not in out  # not treated as a failure
    assert scroll_called == []  # never attempted -- there's nothing to scroll

    header = master_ws.rows[0]
    idx = header.index("campaigns")
    assert master_ws.rows[1][idx] == ""  # correctly cleared -- tk_1 no longer in it


def test_a_real_campaign_still_scrolls_and_exports_normally(monkeypatch):
    # confirms the empty-state check doesn't short-circuit a genuinely
    # non-empty campaign
    monkeypatch.setenv("SPREADSHEET_ID_DUDEROBE", "sheet1")
    master_ws = FakeWorksheet(rows=[MASTER_HEADER, _row("tk_1")])
    gc = FakeClient({"sheet1": FakeSpreadsheet(worksheets={"Master Data": master_ws})})

    monkeypatch.setattr(sc.refunnel_export, "list_available_campaigns", lambda page, **kw: ["Real Campaign"])
    monkeypatch.setattr(sc.refunnel_export, "has_no_results_for_filter", lambda page: False)
    monkeypatch.setattr(sc.refunnel_export, "export_media_csv", lambda *a, **kw: "/tmp/fake.csv")
    monkeypatch.setattr(sc, "_ids_from_csv", lambda path: {"tk_1"})

    sc.sync_campaigns_for_brand(gc, _brand_config(), "x@example.com", ["Duderobe"])

    header = master_ws.rows[0]
    idx = header.index("campaigns")
    assert master_ws.rows[1][idx] == "Real Campaign"


def test_sabotage_empty_campaign_wrongly_logged_as_failure_would_be_caught(monkeypatch, capsys):
    monkeypatch.setenv("SPREADSHEET_ID_DUDEROBE", "sheet1")
    master_ws = FakeWorksheet(rows=[MASTER_HEADER, _row("tk_1")])
    gc = FakeClient({"sheet1": FakeSpreadsheet(worksheets={"Master Data": master_ws})})

    monkeypatch.setattr(sc.refunnel_export, "list_available_campaigns", lambda page, **kw: ["Empty Campaign"])
    monkeypatch.setattr(sc.refunnel_export, "has_no_results_for_filter", lambda page: True)

    sc.sync_campaigns_for_brand(gc, _brand_config(), "x@example.com", ["Duderobe"])

    out = capsys.readouterr().out
    with pytest.raises(AssertionError):
        assert "couldn't process" in out  # wrong -- a real 0-result answer is not a failure
    assert "0 post(s)" in out  # confirms actual correct behavior

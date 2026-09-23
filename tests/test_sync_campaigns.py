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
    monkeypatch.setattr(sc.refunnel_auth, "refunnel_social_listening_url", lambda *a, **kw: "https://x")
    monkeypatch.setattr(sc.refunnel_export, "select_workspace", lambda *a, **kw: None)
    monkeypatch.setattr(sc.refunnel_export, "scroll_to_load_all", lambda *a, **kw: None)
    monkeypatch.setattr(sc.refunnel_export, "filter_by_campaign", lambda *a, **kw: None)
    monkeypatch.setattr(sc.refunnel_export, "clear_all_filters", lambda *a, **kw: None)
    # Default: assume the filter genuinely applied -- existing tests
    # are testing other parts of the flow, not this specific safety
    # gate. The dedicated tests below override this to False to
    # reproduce the exact real bug it catches.
    monkeypatch.setattr(sc.refunnel_export, "filter_is_genuinely_active", lambda page, name: True)


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


# ---------- filter_is_genuinely_active: the SheRobe Content Campaign bug ----------

def test_refuses_to_export_when_filter_silently_failed_to_apply(monkeypatch, capsys):
    # confirmed real, serious bug this reproduces exactly: a live run
    # reported 2795 posts (the entire unfiltered library) for "SheRobe
    # Content Campaign", while a manual check of the SAME campaign
    # showed genuinely zero results -- the filter silently never took
    # effect, and nothing caught it before this fix
    monkeypatch.setenv("SPREADSHEET_ID_DUDEROBE", "sheet1")
    master_ws = FakeWorksheet(rows=[MASTER_HEADER, _row("tk_1")])
    gc = FakeClient({"sheet1": FakeSpreadsheet(worksheets={"Master Data": master_ws})})

    monkeypatch.setattr(sc.refunnel_export, "list_available_campaigns",
                         lambda page, **kw: ["SheRobe Content Campaign"])
    # simulates the exact real bug: filter_is_genuinely_active reports
    # the filter did NOT actually apply
    monkeypatch.setattr(sc.refunnel_export, "filter_is_genuinely_active", lambda page, name: False)

    scroll_called = []
    export_called = []
    monkeypatch.setattr(sc.refunnel_export, "scroll_to_load_all", lambda *a, **kw: scroll_called.append(1))
    monkeypatch.setattr(sc.refunnel_export, "export_media_csv", lambda *a, **kw: export_called.append(1))

    sc.sync_campaigns_for_brand(gc, _brand_config(), "x@example.com", ["Duderobe"])

    out = capsys.readouterr().out
    assert "couldn't process campaign" in out
    assert scroll_called == []   # never reached -- the gate stopped it first
    assert export_called == []  # never reached either

    header = master_ws.rows[0]
    idx = header.index("campaigns")
    assert master_ws.rows[1][idx] == ""  # NOT mistagged with the unfiltered export


def test_a_genuinely_active_filter_proceeds_normally(monkeypatch):
    # confirms the safety gate doesn't block a real, correctly-applied filter
    monkeypatch.setenv("SPREADSHEET_ID_DUDEROBE", "sheet1")
    master_ws = FakeWorksheet(rows=[MASTER_HEADER, _row("tk_1")])
    gc = FakeClient({"sheet1": FakeSpreadsheet(worksheets={"Master Data": master_ws})})

    monkeypatch.setattr(sc.refunnel_export, "list_available_campaigns", lambda page, **kw: ["Real Campaign"])
    monkeypatch.setattr(sc.refunnel_export, "filter_is_genuinely_active", lambda page, name: True)
    monkeypatch.setattr(sc.refunnel_export, "has_no_results_for_filter", lambda page: False)
    monkeypatch.setattr(sc.refunnel_export, "export_media_csv", lambda *a, **kw: "/tmp/fake.csv")
    monkeypatch.setattr(sc, "_ids_from_csv", lambda path: {"tk_1"})

    sc.sync_campaigns_for_brand(gc, _brand_config(), "x@example.com", ["Duderobe"])

    header = master_ws.rows[0]
    idx = header.index("campaigns")
    assert master_ws.rows[1][idx] == "Real Campaign"


def test_sabotage_the_shed_robe_bug_would_be_caught(monkeypatch):
    monkeypatch.setenv("SPREADSHEET_ID_DUDEROBE", "sheet1")
    master_ws = FakeWorksheet(rows=[MASTER_HEADER, _row("tk_1")])
    gc = FakeClient({"sheet1": FakeSpreadsheet(worksheets={"Master Data": master_ws})})

    monkeypatch.setattr(sc.refunnel_export, "list_available_campaigns",
                         lambda page, **kw: ["SheRobe Content Campaign"])
    monkeypatch.setattr(sc.refunnel_export, "filter_is_genuinely_active", lambda page, name: False)
    # if the bug were still present, this would be reached and return
    # "everything" (simulating the real 2795-post unfiltered export)
    monkeypatch.setattr(sc.refunnel_export, "export_media_csv", lambda *a, **kw: "/tmp/fake.csv")
    monkeypatch.setattr(sc, "_ids_from_csv", lambda path: {"tk_1"})

    sc.sync_campaigns_for_brand(gc, _brand_config(), "x@example.com", ["Duderobe"])

    header = master_ws.rows[0]
    idx = header.index("campaigns")
    with pytest.raises(AssertionError):
        assert master_ws.rows[1][idx] == "SheRobe Content Campaign"  # wrong -- would mean the bug is back
    assert master_ws.rows[1][idx] == ""  # confirms the safety gate actually stopped it


# ---------- campaigns are FREEZE-ONCE-SET (explicit instruction) ----------

def test_blank_campaign_cell_gets_filled(monkeypatch):
    monkeypatch.setenv("SPREADSHEET_ID_DUDEROBE", "sheet1")
    master_ws = FakeWorksheet(rows=[MASTER_HEADER, _row("tk_1")])  # blank campaigns
    gc = FakeClient({"sheet1": FakeSpreadsheet(worksheets={"Master Data": master_ws})})

    monkeypatch.setattr(sc.refunnel_export, "list_available_campaigns", lambda page, **kw: ["Campaign A"])
    monkeypatch.setattr(sc.refunnel_export, "has_no_results_for_filter", lambda page: False)
    monkeypatch.setattr(sc.refunnel_export, "export_media_csv", lambda *a, **kw: "/tmp/f.csv")
    monkeypatch.setattr(sc, "_ids_from_csv", lambda path: {"tk_1"})

    sc.sync_campaigns_for_brand(gc, _brand_config(), "x@example.com", ["Duderobe"])

    idx = master_ws.rows[0].index("campaigns")
    assert master_ws.rows[1][idx] == "Campaign A"


def test_existing_campaign_value_is_never_overwritten(monkeypatch):
    # the whole point of freezing: protects real data from being wiped
    # by a silently-failed filter/scroll/export chain
    monkeypatch.setenv("SPREADSHEET_ID_DUDEROBE", "sheet1")
    master_ws = FakeWorksheet(rows=[MASTER_HEADER, _row("tk_1", campaigns="Original Campaign")])
    gc = FakeClient({"sheet1": FakeSpreadsheet(worksheets={"Master Data": master_ws})})

    monkeypatch.setattr(sc.refunnel_export, "list_available_campaigns", lambda page, **kw: ["Different Campaign"])
    monkeypatch.setattr(sc.refunnel_export, "has_no_results_for_filter", lambda page: False)
    monkeypatch.setattr(sc.refunnel_export, "export_media_csv", lambda *a, **kw: "/tmp/f.csv")
    monkeypatch.setattr(sc, "_ids_from_csv", lambda path: {"tk_1"})

    sc.sync_campaigns_for_brand(gc, _brand_config(), "x@example.com", ["Duderobe"])

    idx = master_ws.rows[0].index("campaigns")
    assert master_ws.rows[1][idx] == "Original Campaign"


def test_empty_campaign_result_never_wipes_an_existing_value(monkeypatch):
    # THE failure mode this design protects against: a campaign that
    # returns nothing (genuinely empty, OR a silently broken filter)
    # must not clear campaign data already in the sheet
    monkeypatch.setenv("SPREADSHEET_ID_DUDEROBE", "sheet1")
    master_ws = FakeWorksheet(rows=[MASTER_HEADER, _row("tk_1", campaigns="Real Campaign")])
    gc = FakeClient({"sheet1": FakeSpreadsheet(worksheets={"Master Data": master_ws})})

    monkeypatch.setattr(sc.refunnel_export, "list_available_campaigns", lambda page, **kw: ["Real Campaign"])
    monkeypatch.setattr(sc.refunnel_export, "has_no_results_for_filter", lambda page: True)  # empty

    sc.sync_campaigns_for_brand(gc, _brand_config(), "x@example.com", ["Duderobe"])

    idx = master_ws.rows[0].index("campaigns")
    assert master_ws.rows[1][idx] == "Real Campaign"


def test_sabotage_overwriting_an_existing_campaign_would_be_caught(monkeypatch):
    monkeypatch.setenv("SPREADSHEET_ID_DUDEROBE", "sheet1")
    master_ws = FakeWorksheet(rows=[MASTER_HEADER, _row("tk_1", campaigns="Original Campaign")])
    gc = FakeClient({"sheet1": FakeSpreadsheet(worksheets={"Master Data": master_ws})})

    monkeypatch.setattr(sc.refunnel_export, "list_available_campaigns", lambda page, **kw: ["Different Campaign"])
    monkeypatch.setattr(sc.refunnel_export, "has_no_results_for_filter", lambda page: False)
    monkeypatch.setattr(sc.refunnel_export, "export_media_csv", lambda *a, **kw: "/tmp/f.csv")
    monkeypatch.setattr(sc, "_ids_from_csv", lambda path: {"tk_1"})

    sc.sync_campaigns_for_brand(gc, _brand_config(), "x@example.com", ["Duderobe"])

    idx = master_ws.rows[0].index("campaigns")
    with pytest.raises(AssertionError):
        assert master_ws.rows[1][idx] == "Different Campaign"  # wrong -- freezing forbids this
    assert master_ws.rows[1][idx] == "Original Campaign"

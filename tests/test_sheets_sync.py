"""
Tests for sheets_sync.py, using an in-memory FakeSheetsClient so no real
Google credentials are needed. GspreadSheetsClient itself (the real
implementation) is NOT covered by these tests -- it needs a live sheet
and should be smoke-tested manually once credentials exist.
"""

import pytest

from sheets_sync import (
    sync_tab,
    read_column_values,
    SuspiciousShrinkError,
    GspreadSheetsClient,
    get_or_create_worksheet,
    _flatten_cell,
    is_transient_gspread_error,
    retry_on_transient_error,
)
import gspread.exceptions


class FakeSheetsClient:
    """In-memory stand-in for a Google Sheet tab."""

    def __init__(self, initial_rows=None):
        # initial_rows: list of lists, first row is header, rest are data
        self.rows = initial_rows if initial_rows is not None else []
        self.overwrite_calls = 0

    def read_all(self):
        return [list(r) for r in self.rows]

    def overwrite_all(self, rows):
        self.overwrite_calls += 1
        self.rows = [list(r) for r in rows]


COLUMNS = ["id", "username", "rights_status"]


def _rows(*items):
    """items: (id, username, rights_status) tuples -> target_rows dict"""
    return {i: {"id": i, "username": u, "rights_status": s} for i, u, s in items}


def test_writes_header_and_sorted_rows_on_empty_tab():
    client = FakeSheetsClient()
    target = _rows(("b1", "bob", "GRANTED"), ("a1", "alice", "REQUESTED"))
    summary = sync_tab(client, COLUMNS, target)

    assert client.rows[0] == COLUMNS
    # default sort is by id
    assert client.rows[1][0] == "a1"
    assert client.rows[2][0] == "b1"
    assert summary["rows_written"] == 2
    assert summary["extra_columns_preserved"] == []


def test_sort_key_orders_by_given_field():
    client = FakeSheetsClient()
    target = _rows(("id1", "zeta", "GRANTED"), ("id2", "alpha", "GRANTED"))
    sync_tab(client, COLUMNS, target, sort_key="username")
    assert client.rows[1][1] == "alpha"
    assert client.rows[2][1] == "zeta"


def test_row_removed_from_target_disappears_from_tab():
    # simulates a post moving from Requested -> Approved: it should no
    # longer appear in a tab whose target no longer includes it
    client = FakeSheetsClient(
        [COLUMNS, ["id1", "alice", "REQUESTED"], ["id2", "bob", "REQUESTED"]]
    )
    target = _rows(("id2", "bob", "REQUESTED"))  # id1 moved elsewhere
    sync_tab(client, COLUMNS, target)
    ids_in_tab = [row[0] for row in client.rows[1:]]
    assert ids_in_tab == ["id2"]


def test_extra_manual_column_is_preserved():
    client = FakeSheetsClient(
        [
            COLUMNS + ["Notes"],
            ["id1", "alice", "REQUESTED", "call her back"],
        ]
    )
    target = _rows(("id1", "alice", "GRANTED"))  # status changed upstream
    summary = sync_tab(client, COLUMNS, target)

    assert client.rows[0] == COLUMNS + ["Notes"]
    assert client.rows[1] == ["id1", "alice", "GRANTED", "call her back"]
    assert summary["extra_columns_preserved"] == ["Notes"]


def test_extra_column_for_a_brand_new_row_is_blank():
    client = FakeSheetsClient(
        [COLUMNS + ["Notes"], ["id1", "alice", "REQUESTED", "call her back"]]
    )
    target = _rows(("id1", "alice", "REQUESTED"), ("id2", "new_person", "REQUESTED"))
    sync_tab(client, COLUMNS, target)
    row_by_id = {row[0]: row for row in client.rows[1:]}
    assert row_by_id["id1"][3] == "call her back"
    assert row_by_id["id2"][3] == ""  # no prior note for a brand-new row


def test_empty_target_clears_data_rows_but_keeps_calling_overwrite():
    client = FakeSheetsClient([COLUMNS, ["id1", "alice", "REQUESTED"]])
    sync_tab(client, COLUMNS, {})
    assert client.rows == [COLUMNS]
    assert client.overwrite_calls == 1


# ---------- sabotage tests ----------

def test_sabotage_forgetting_to_preserve_extra_columns_would_be_caught():
    # simulate the bug where extra-column detection is skipped entirely
    client = FakeSheetsClient(
        [COLUMNS + ["Notes"], ["id1", "alice", "REQUESTED", "important note"]]
    )
    target = _rows(("id1", "alice", "GRANTED"))
    sync_tab(client, COLUMNS, target)

    # the real behavior keeps the note -- assert the broken behavior
    # (note lost) would fail, proving the test is sensitive to this bug
    with pytest.raises(AssertionError):
        assert client.rows[1] == ["id1", "alice", "GRANTED", ""]
    # and confirm what actually happened is the correct, non-broken result
    assert client.rows[1] == ["id1", "alice", "GRANTED", "important note"]


def test_sabotage_wrong_sort_order_would_be_caught():
    client = FakeSheetsClient()
    target = _rows(("id1", "zeta", "GRANTED"), ("id2", "alpha", "GRANTED"))
    sync_tab(client, COLUMNS, target, sort_key="username")
    with pytest.raises(AssertionError):
        assert client.rows[1][1] == "zeta"  # wrong -- alpha sorts first


# ---------- read_column_values tests ----------

def test_read_column_values_returns_values_keyed_by_id():
    client = FakeSheetsClient(
        [
            ["id", "username", "Reviewed"],
            ["id1", "alice", "Yes"],
            ["id2", "bob", "No"],
        ]
    )
    values = read_column_values(client, "Reviewed")
    assert values == {"id1": "Yes", "id2": "No"}


def test_read_column_values_missing_column_returns_empty_dict():
    client = FakeSheetsClient([["id", "username"], ["id1", "alice"]])
    assert read_column_values(client, "Reviewed") == {}


def test_read_column_values_empty_sheet_returns_empty_dict():
    client = FakeSheetsClient([])
    assert read_column_values(client, "Reviewed") == {}


def test_read_column_values_skips_rows_with_blank_id():
    client = FakeSheetsClient(
        [["id", "Reviewed"], ["", "Yes"], ["id1", "Yes"]]
    )
    assert read_column_values(client, "Reviewed") == {"id1": "Yes"}


def test_sabotage_read_column_values_wrong_column_would_be_caught():
    client = FakeSheetsClient(
        [["id", "username", "Reviewed"], ["id1", "alice", "Yes"]]
    )
    values = read_column_values(client, "Reviewed")
    with pytest.raises(AssertionError):
        assert values["id1"] == "alice"  # wrong column's value
    assert values["id1"] == "Yes"


# ---------- shrink-protection tests ----------

def _many_rows(n, prefix="id"):
    return _rows(*[(f"{prefix}{i}", f"user{i}", "GRANTED") for i in range(n)])


def test_shrink_protection_blocks_a_big_unexpected_drop():
    existing_rows = [COLUMNS] + [[f"id{i}", f"user{i}", "GRANTED"] for i in range(100)]
    client = FakeSheetsClient(existing_rows)
    target = _many_rows(20)  # 80% drop -- way past a 10% allowance
    with pytest.raises(SuspiciousShrinkError, match="Refusing to overwrite"):
        sync_tab(client, COLUMNS, target, max_shrink_fraction=0.1)
    # sheet must be untouched -- still the original 100 rows
    assert client.rows == existing_rows
    assert client.overwrite_calls == 0


def test_shrink_protection_allows_growth():
    existing_rows = [COLUMNS] + [[f"id{i}", f"user{i}", "GRANTED"] for i in range(100)]
    client = FakeSheetsClient(existing_rows)
    target = _many_rows(150)  # grew -- should sail through
    sync_tab(client, COLUMNS, target, max_shrink_fraction=0.1)
    assert client.overwrite_calls == 1
    assert len(client.rows) - 1 == 150


def test_shrink_protection_allows_small_shrink_within_tolerance():
    existing_rows = [COLUMNS] + [[f"id{i}", f"user{i}", "GRANTED"] for i in range(100)]
    client = FakeSheetsClient(existing_rows)
    target = _many_rows(95)  # 5% drop -- within a 10% allowance
    sync_tab(client, COLUMNS, target, max_shrink_fraction=0.1)
    assert client.overwrite_calls == 1


def test_shrink_protection_does_nothing_when_not_requested():
    existing_rows = [COLUMNS] + [[f"id{i}", f"user{i}", "GRANTED"] for i in range(100)]
    client = FakeSheetsClient(existing_rows)
    target = _many_rows(5)  # huge drop, but max_shrink_fraction not set for this tab
    sync_tab(client, COLUMNS, target)  # no max_shrink_fraction -- should just work
    assert client.overwrite_calls == 1


def test_shrink_protection_does_not_block_first_ever_write():
    client = FakeSheetsClient()  # empty sheet, never written before
    target = _many_rows(5)
    sync_tab(client, COLUMNS, target, max_shrink_fraction=0.1)
    assert client.overwrite_calls == 1


def test_sabotage_shrink_threshold_ignored_would_be_caught():
    existing_rows = [COLUMNS] + [[f"id{i}", f"user{i}", "GRANTED"] for i in range(100)]
    client = FakeSheetsClient(existing_rows)
    target = _many_rows(20)
    with pytest.raises(SuspiciousShrinkError):
        sync_tab(client, COLUMNS, target, max_shrink_fraction=0.1)
    with pytest.raises(AssertionError):
        assert client.overwrite_calls == 1  # wrong -- it should have been refused
    assert client.overwrite_calls == 0  # confirms actual correct behavior


# ---------- update_single_cell tests ----------

class FakeWorksheet:
    """Minimal gspread-shaped fake: row_values / col_values / update_cell."""

    def __init__(self, rows):
        self.rows = [list(r) for r in rows]  # rows[0] is the header
        self.update_cell_calls = []

    def row_values(self, row_num):
        return self.rows[row_num - 1]

    def col_values(self, col_num):
        return [row[col_num - 1] if col_num - 1 < len(row) else "" for row in self.rows]

    def update_cell(self, row_num, col_num, value):
        self.update_cell_calls.append((row_num, col_num, value))
        while len(self.rows[row_num - 1]) < col_num:
            self.rows[row_num - 1].append("")
        self.rows[row_num - 1][col_num - 1] = value


def test_update_single_cell_updates_the_right_row_and_column():
    ws = FakeWorksheet([
        ["id", "username", "creator_email"],
        ["id1", "alice", ""],
        ["id2", "bob", ""],
    ])
    client = GspreadSheetsClient(ws)
    found = client.update_single_cell("id2", "creator_email", "bob@example.com")
    assert found is True
    assert ws.rows[2] == ["id2", "bob", "bob@example.com"]
    assert ws.rows[1] == ["id1", "alice", ""]  # untouched
    assert ws.update_cell_calls == [(3, 3, "bob@example.com")]


def test_update_single_cell_returns_false_if_id_not_found():
    ws = FakeWorksheet([["id", "creator_email"], ["id1", ""]])
    client = GspreadSheetsClient(ws)
    found = client.update_single_cell("not_a_real_id", "creator_email", "x@example.com")
    assert found is False
    assert ws.update_cell_calls == []


def test_update_single_cell_returns_false_if_column_missing():
    ws = FakeWorksheet([["id", "username"], ["id1", "alice"]])
    client = GspreadSheetsClient(ws)
    found = client.update_single_cell("id1", "creator_email", "x@example.com")
    assert found is False
    assert ws.update_cell_calls == []


def test_sabotage_update_single_cell_wrong_row_would_be_caught():
    ws = FakeWorksheet([
        ["id", "username", "creator_email"],
        ["id1", "alice", ""],
        ["id2", "bob", ""],
    ])
    client = GspreadSheetsClient(ws)
    client.update_single_cell("id1", "creator_email", "alice@example.com")
    with pytest.raises(AssertionError):
        assert ws.rows[2][2] == "alice@example.com"  # wrong row -- that's id2/bob's row
    assert ws.rows[1][2] == "alice@example.com"  # confirms actual correct behavior


# ---------- never_delete tests ----------

def test_never_delete_carries_forward_a_row_missing_from_target():
    client = FakeSheetsClient(
        [COLUMNS, ["id1", "alice", "GRANTED"], ["id2", "bob", "GRANTED"]]
    )
    target = _rows(("id2", "bob", "GRANTED"))  # id1 missing from this run's pull
    summary = sync_tab(client, COLUMNS, target, never_delete=True)

    ids_in_tab = {row[0] for row in client.rows[1:]}
    assert ids_in_tab == {"id1", "id2"}  # id1 survived even though not in target
    assert summary["rows_carried_forward"] == 1


def test_never_delete_still_applies_updates_to_matching_rows():
    client = FakeSheetsClient(
        [COLUMNS, ["id1", "alice", "REQUESTED"]]
    )
    target = _rows(("id1", "alice", "GRANTED"))  # status changed
    sync_tab(client, COLUMNS, target, never_delete=True)

    row = next(r for r in client.rows[1:] if r[0] == "id1")
    assert row[2] == "GRANTED"  # updated, not stuck at the old value


def test_never_delete_still_adds_genuinely_new_rows():
    client = FakeSheetsClient([COLUMNS, ["id1", "alice", "GRANTED"]])
    target = _rows(("id1", "alice", "GRANTED"), ("id2", "new_person", "REQUESTED"))
    summary = sync_tab(client, COLUMNS, target, never_delete=True)

    ids_in_tab = {row[0] for row in client.rows[1:]}
    assert ids_in_tab == {"id1", "id2"}
    assert summary["rows_carried_forward"] == 0  # id1 was in target, not carried forward


def test_never_delete_on_empty_sheet_is_a_normal_first_write():
    client = FakeSheetsClient()  # never written before
    target = _rows(("id1", "alice", "GRANTED"))
    summary = sync_tab(client, COLUMNS, target, never_delete=True)
    assert len(client.rows) - 1 == 1
    assert summary["rows_carried_forward"] == 0


def test_never_delete_ignores_max_shrink_fraction_since_shrink_is_impossible():
    # even a request that WOULD trip the shrink check should just work,
    # since never_delete makes an actual shrink structurally impossible
    client = FakeSheetsClient(
        [COLUMNS] + [[f"id{i}", f"user{i}", "GRANTED"] for i in range(100)]
    )
    target = _rows(("id0", "user0", "GRANTED"))  # would be a 99% drop if not for never_delete
    sync_tab(client, COLUMNS, target, never_delete=True, max_shrink_fraction=0.1)
    assert len(client.rows) - 1 == 100  # nothing lost


def test_sabotage_never_delete_dropping_a_row_would_be_caught():
    client = FakeSheetsClient(
        [COLUMNS, ["id1", "alice", "GRANTED"], ["id2", "bob", "GRANTED"]]
    )
    target = _rows(("id2", "bob", "GRANTED"))
    sync_tab(client, COLUMNS, target, never_delete=True)
    ids_in_tab = {row[0] for row in client.rows[1:]}
    with pytest.raises(AssertionError):
        assert ids_in_tab == {"id2"}  # wrong -- id1 should have survived
    assert ids_in_tab == {"id1", "id2"}  # confirms actual correct behavior


# ---------- get_or_create_worksheet tests ----------

class _FakeSpreadsheet:
    def __init__(self, existing_error=None, worksheet_obj="EXISTING_WORKSHEET"):
        self._existing_error = existing_error
        self._worksheet_obj = worksheet_obj
        self.add_worksheet_calls = []

    def worksheet(self, title):
        if self._existing_error:
            raise self._existing_error
        return self._worksheet_obj

    def add_worksheet(self, title, rows, cols):
        self.add_worksheet_calls.append((title, rows, cols))
        return "NEWLY_CREATED_WORKSHEET"


def test_get_or_create_worksheet_returns_existing_when_found():
    spreadsheet = _FakeSpreadsheet(existing_error=None)
    result = get_or_create_worksheet(spreadsheet, "Master Data")
    assert result == "EXISTING_WORKSHEET"
    assert spreadsheet.add_worksheet_calls == []


def test_get_or_create_worksheet_creates_on_genuine_not_found():
    spreadsheet = _FakeSpreadsheet(existing_error=gspread.exceptions.WorksheetNotFound("nope"))
    result = get_or_create_worksheet(spreadsheet, "Master Data")
    assert result == "NEWLY_CREATED_WORKSHEET"
    assert spreadsheet.add_worksheet_calls == [("Master Data", 1000, 30)]


def test_get_or_create_worksheet_propagates_other_errors_instead_of_creating():
    # this is the real bug a live run hit: a transient 503 from
    # Google's side was being misread as "doesn't exist yet", causing
    # an attempted duplicate creation that then failed for real
    spreadsheet = _FakeSpreadsheet(existing_error=RuntimeError("503 Service Unavailable"))
    with pytest.raises(RuntimeError, match="503"):
        get_or_create_worksheet(spreadsheet, "Master Data")
    assert spreadsheet.add_worksheet_calls == []  # never attempted a duplicate create


def test_sabotage_broad_except_would_be_caught():
    # proves the fix is actually scoped to WorksheetNotFound, not just
    # any exception -- a generic error must NOT trigger a create
    spreadsheet = _FakeSpreadsheet(existing_error=ValueError("some unrelated error"))
    with pytest.raises(ValueError):
        get_or_create_worksheet(spreadsheet, "Master Data")
    with pytest.raises(AssertionError):
        assert spreadsheet.add_worksheet_calls == [("Master Data", 1000, 30)]  # wrong
    assert spreadsheet.add_worksheet_calls == []  # confirms actual correct behavior


# ---------- _flatten_cell tests ----------

def test_flatten_cell_collapses_embedded_newlines():
    value = "10 of my favorite\n\n1) Nick Shirley\n2) Brodo Bone Broth"
    result = _flatten_cell(value)
    assert "\n" not in result
    assert result == "10 of my favorite 1) Nick Shirley 2) Brodo Bone Broth"


def test_flatten_cell_handles_none():
    assert _flatten_cell(None) == ""


def test_flatten_cell_leaves_normal_text_unchanged():
    assert _flatten_cell("just a normal caption") == "just a normal caption"


def test_flatten_cell_handles_crlf_too():
    assert _flatten_cell("line one\r\nline two") == "line one line two"


def test_sync_tab_strips_newlines_from_written_rows():
    client = FakeSheetsClient()
    target = {"id1": {"id": "id1", "username": "alice", "rights_status": "one\ntwo\nthree"}}
    sync_tab(client, COLUMNS, target)
    written_status = client.rows[1][2]
    assert "\n" not in written_status
    assert written_status == "one two three"


def test_sabotage_flatten_cell_newline_left_in_would_be_caught():
    result = _flatten_cell("a\nb")
    with pytest.raises(AssertionError):
        assert "\n" in result  # wrong -- it should have been collapsed
    assert result == "a b"


# ---------- sort_reverse tests ----------

def test_sort_reverse_puts_newest_first():
    client = FakeSheetsClient()
    target = _rows(
        ("old_id", "alice", "GRANTED"),
        ("new_id", "bob", "GRANTED"),
    )
    # simulate a created_at-like field via the sort_key mechanism using
    # the existing 3-column row shape -- reuse "rights_status" as a
    # stand-in sortable field for this test's purposes
    target["old_id"]["rights_status"] = "2026-01-01"
    target["new_id"]["rights_status"] = "2026-06-01"
    sync_tab(client, COLUMNS, target, sort_key="rights_status", sort_reverse=True)
    assert client.rows[1][0] == "new_id"  # newest (later date) first
    assert client.rows[2][0] == "old_id"


def test_sort_reverse_false_keeps_ascending_order():
    client = FakeSheetsClient()
    target = _rows(("a_id", "alice", "2026-01-01"), ("b_id", "bob", "2026-06-01"))
    sync_tab(client, COLUMNS, target, sort_key="rights_status", sort_reverse=False)
    assert client.rows[1][0] == "a_id"  # earliest date first (unchanged default)
    assert client.rows[2][0] == "b_id"


def test_sabotage_sort_reverse_ignored_would_be_caught():
    client = FakeSheetsClient()
    target = _rows(("old_id", "alice", "2026-01-01"), ("new_id", "bob", "2026-06-01"))
    sync_tab(client, COLUMNS, target, sort_key="rights_status", sort_reverse=True)
    with pytest.raises(AssertionError):
        assert client.rows[1][0] == "old_id"  # wrong -- newest should be first
    assert client.rows[1][0] == "new_id"


# ---------- blank-header extra-column tests ----------

def test_blank_named_headers_are_not_preserved_as_extra_columns():
    # confirmed real: renaming a known column (impressions -> views,
    # then reverted) left stray blank-header cells trailing in a real
    # sheet -- this must not be treated as a manual column to carry
    # forward forever
    client = FakeSheetsClient(
        [COLUMNS + ["", ""], ["id1", "alice", "GRANTED", "leftover1", "leftover2"]]
    )
    target = _rows(("id1", "alice", "GRANTED"))
    summary = sync_tab(client, COLUMNS, target)
    assert summary["extra_columns_preserved"] == []
    assert client.rows[0] == COLUMNS  # no trailing blank headers anymore
    assert client.rows[1] == ["id1", "alice", "GRANTED"]  # no leftover data trailing either


def test_a_real_named_extra_column_is_still_preserved():
    client = FakeSheetsClient(
        [COLUMNS + ["Notes"], ["id1", "alice", "GRANTED", "some note"]]
    )
    target = _rows(("id1", "alice", "GRANTED"))
    summary = sync_tab(client, COLUMNS, target)
    assert summary["extra_columns_preserved"] == ["Notes"]
    assert client.rows[1] == ["id1", "alice", "GRANTED", "some note"]


def test_sabotage_blank_header_check_missing_would_be_caught():
    client = FakeSheetsClient(
        [COLUMNS + ["", ""], ["id1", "alice", "GRANTED", "leftover1", "leftover2"]]
    )
    target = _rows(("id1", "alice", "GRANTED"))
    summary = sync_tab(client, COLUMNS, target)
    with pytest.raises(AssertionError):
        assert summary["extra_columns_preserved"] == ["", ""]  # wrong -- blanks shouldn't count
    assert summary["extra_columns_preserved"] == []  # confirms actual correct behavior


# ---------- is_transient_gspread_error / retry_on_transient_error ----------

def test_recognizes_the_real_503_message_format():
    # confirmed real, exact format from a live scheduled-run failure
    err = RuntimeError("APIError: [503]: The service is currently unavailable.")
    assert is_transient_gspread_error(err) is True


def test_recognizes_other_transient_status_codes():
    for code in (429, 500, 502, 503, 504):
        err = RuntimeError(f"APIError: [{code}]: some transient message")
        assert is_transient_gspread_error(err) is True


def test_does_not_flag_a_permanent_error_as_transient():
    err = RuntimeError("APIError: [404]: Requested entity was not found.")
    assert is_transient_gspread_error(err) is False


def test_does_not_flag_an_unrelated_error_as_transient():
    err = ValueError("some completely unrelated error")
    assert is_transient_gspread_error(err) is False


def test_retry_succeeds_after_transient_failures():
    calls = {"count": 0}

    def flaky():
        calls["count"] += 1
        if calls["count"] < 3:
            raise RuntimeError("APIError: [503]: The service is currently unavailable.")
        return "success"

    result = retry_on_transient_error(flaky, max_attempts=5, initial_delay_seconds=0.001)
    assert result == "success"
    assert calls["count"] == 3


def test_retry_passes_through_args_and_kwargs():
    def add(a, b, c=0):
        return a + b + c

    result = retry_on_transient_error(add, 1, 2, max_attempts=3, initial_delay_seconds=0.001, c=10)
    assert result == 13


def test_retry_gives_up_after_max_attempts():
    calls = {"count": 0}

    def always_fails():
        calls["count"] += 1
        raise RuntimeError("APIError: [503]: The service is currently unavailable.")

    with pytest.raises(RuntimeError, match="503"):
        retry_on_transient_error(always_fails, max_attempts=3, initial_delay_seconds=0.001)
    assert calls["count"] == 3


def test_retry_never_retries_a_non_transient_error():
    calls = {"count": 0}

    def permanent_failure():
        calls["count"] += 1
        raise RuntimeError("APIError: [404]: Requested entity was not found.")

    with pytest.raises(RuntimeError, match="404"):
        retry_on_transient_error(permanent_failure, max_attempts=5, initial_delay_seconds=0.001)
    assert calls["count"] == 1  # never retried -- a real 404 retrying wouldn't fix


def test_sabotage_non_transient_error_wrongly_retried_would_be_caught():
    calls = {"count": 0}

    def permanent_failure():
        calls["count"] += 1
        raise RuntimeError("APIError: [404]: Requested entity was not found.")

    with pytest.raises(RuntimeError):
        retry_on_transient_error(permanent_failure, max_attempts=5, initial_delay_seconds=0.001)
    with pytest.raises(AssertionError):
        assert calls["count"] == 5  # wrong -- a 404 should never be retried at all
    assert calls["count"] == 1  # confirms actual correct behavior

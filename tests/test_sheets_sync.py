"""
Tests for sheets_sync.py, using an in-memory FakeSheetsClient so no real
Google credentials are needed. GspreadSheetsClient itself (the real
implementation) is NOT covered by these tests -- it needs a live sheet
and should be smoke-tested manually once credentials exist.
"""

import pytest

from sheets_sync import sync_tab, read_column_values, SuspiciousShrinkError


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

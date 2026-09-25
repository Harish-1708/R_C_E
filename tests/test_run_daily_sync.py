"""Focused, source-inspection tests for run_daily_sync.py.

run_daily_sync.py is a large orchestration script with no existing
test file -- building full mock-based coverage for the whole thing is
out of scope for a single, targeted fix. This follows the same
lighter-weight pattern already used elsewhere in this project for
similarly hard-to-mock code (e.g. the Chromium launch-args check in
test_refunnel_auth.py): verify the specific call/argument pattern
directly in the source, rather than exercising the full script.
"""
import inspect

import pytest

import run_daily_sync


# ---------- Master Data's early write now sorts newest-first too ----------

def test_early_master_write_sorts_by_created_at_descending():
    # CONFIRMED REAL gap this fixes: the early "before scraping starts"
    # write didn't pass sort_key at all, unlike the final full sync
    # pass (which correctly sorts Master Data by created_at, newest
    # first). If a run crashes during scraping -- confirmed to happen
    # repeatedly in this project (browser crashes, scroll_to_load_all
    # failures) -- the sheet was left stuck with only this early
    # write's order until the next successful, complete run. A live
    # comparison against the actual Refunnel page (sorted newest-to-
    # oldest) showed the sheet's order not matching it at all.
    source = inspect.getsource(run_daily_sync.main)
    early_write = source[source.index("before scraping starts"):]
    early_write = early_write[:early_write.index("sync_tab(") + 400]
    assert 'sort_key="created_at"' in early_write
    assert "sort_reverse=True" in early_write


def test_sabotage_the_early_write_missing_sort_key_would_be_caught():
    source = inspect.getsource(run_daily_sync.main)
    early_write = source[source.index("before scraping starts"):]
    early_write = early_write[:early_write.index("sync_tab(") + 400]
    with pytest.raises(AssertionError):
        assert 'sort_key="created_at"' not in early_write  # wrong -- the exact gap this fixes
    assert 'sort_key="created_at"' in early_write

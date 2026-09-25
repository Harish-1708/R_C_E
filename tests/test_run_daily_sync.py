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


# ---------- Master Data's early write (Phase 3) sorts newest-first too ----------

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
    source = inspect.getsource(run_daily_sync.main_phase3)
    early_write = source[source.index("before scraping starts"):]
    early_write = early_write[:early_write.index("sync_tab(") + 400]
    assert 'sort_key="created_at"' in early_write
    assert "sort_reverse=True" in early_write


def test_sabotage_the_early_write_missing_sort_key_would_be_caught():
    source = inspect.getsource(run_daily_sync.main_phase3)
    early_write = source[source.index("before scraping starts"):]
    early_write = early_write[:early_write.index("sync_tab(") + 400]
    with pytest.raises(AssertionError):
        assert 'sort_key="created_at"' not in early_write  # wrong -- the exact gap this fixes
    assert 'sort_key="created_at"' in early_write


# ---------- Phase 1 / Phase 3 split (confirmed real want: separate, visible steps) ----------
#
# CONFIRMED REAL want, direct request: a single combined "Phases 1+3"
# step made it genuinely hard to tell, from the log alone, whether
# today's export picked up new content at all before scraping's own
# output took over. Split into two independently runnable entry
# points -- main_phase1 (export + write Master Data) and main_phase3
# (scrape emails, Payments, the full six-tab sync) -- so the pipeline
# can call each as its own named GitHub Actions step, like Phase 2 and
# Phase 4 already are.

def test_main_phase1_does_not_scrape_emails():
    # Phase 1 is export-and-write only -- scraping is Phase 3's job
    # exclusively. If Phase 1's own source ever calls the scraper
    # directly, the split has leaked back into one combined step.
    source = inspect.getsource(run_daily_sync.main_phase1)
    assert "scrape_creator_emails" not in source


def test_sabotage_phase1_scraping_emails_would_be_caught():
    # models what main_phase1's source would look like if scraping
    # leaked back in
    fake_source = "def main_phase1():\n    refunnel_export.scrape_creator_emails(page, ...)\n"
    with pytest.raises(AssertionError):
        assert "scrape_creator_emails" not in fake_source  # wrong -- that's the leak this test catches
    assert "scrape_creator_emails" not in inspect.getsource(run_daily_sync.main_phase1)


def test_main_phase1_prints_a_clear_completion_summary():
    # CONFIRMED REAL want: the log itself should say, unambiguously,
    # that Phase 1 finished and how many rows it saw -- not just fall
    # silent into whatever Phase 3 prints next.
    source = inspect.getsource(run_daily_sync.main_phase1)
    assert "PHASE 1 COMPLETE" in source


def test_main_phase3_prints_a_clear_completion_summary():
    source = inspect.getsource(run_daily_sync.main_phase3)
    assert "PHASE 3 COMPLETE" in source


def test_main_dispatches_to_phase1_only_when_pipeline_phase_is_1(monkeypatch):
    monkeypatch.setenv("PIPELINE_PHASE", "1")
    calls = []
    monkeypatch.setattr(run_daily_sync, "main_phase1", lambda: calls.append("phase1") or 0)
    monkeypatch.setattr(run_daily_sync, "main_phase3", lambda: calls.append("phase3") or 0)

    result = run_daily_sync.main()

    assert calls == ["phase1"]
    assert result == 0


def test_main_dispatches_to_phase3_only_when_pipeline_phase_is_3(monkeypatch):
    monkeypatch.setenv("PIPELINE_PHASE", "3")
    calls = []
    monkeypatch.setattr(run_daily_sync, "main_phase1", lambda: calls.append("phase1") or 0)
    monkeypatch.setattr(run_daily_sync, "main_phase3", lambda: calls.append("phase3") or 0)

    result = run_daily_sync.main()

    assert calls == ["phase3"]
    assert result == 0


def test_main_runs_both_phases_in_order_when_pipeline_phase_is_unset(monkeypatch):
    # back-compat: anyone still invoking this script directly without
    # PIPELINE_PHASE set gets the original, single-step behavior --
    # both phases, one after another.
    monkeypatch.delenv("PIPELINE_PHASE", raising=False)
    calls = []
    monkeypatch.setattr(run_daily_sync, "main_phase1", lambda: calls.append("phase1") or 0)
    monkeypatch.setattr(run_daily_sync, "main_phase3", lambda: calls.append("phase3") or 0)

    result = run_daily_sync.main()

    assert calls == ["phase1", "phase3"]
    assert result == 0


def test_main_does_not_run_phase3_if_phase1_failed(monkeypatch):
    # CONFIRMED REAL need: if the export itself failed, running the
    # (slow) scraping phase afterward against stale or missing Master
    # Data data would waste hours for a run that can't succeed anyway.
    monkeypatch.delenv("PIPELINE_PHASE", raising=False)
    calls = []
    monkeypatch.setattr(run_daily_sync, "main_phase1", lambda: calls.append("phase1") or 1)
    monkeypatch.setattr(run_daily_sync, "main_phase3", lambda: calls.append("phase3") or 0)

    result = run_daily_sync.main()

    assert calls == ["phase1"]
    assert result == 1


def test_sabotage_running_phase3_after_a_failed_phase1_would_be_caught(monkeypatch):
    monkeypatch.delenv("PIPELINE_PHASE", raising=False)
    calls = []
    monkeypatch.setattr(run_daily_sync, "main_phase1", lambda: calls.append("phase1") or 1)
    monkeypatch.setattr(run_daily_sync, "main_phase3", lambda: calls.append("phase3") or 0)

    run_daily_sync.main()

    with pytest.raises(AssertionError):
        assert "phase3" in calls  # wrong -- would mean scraping ran despite a failed export
    assert calls == ["phase1"]

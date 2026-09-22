"""
build_content_tracker.py

Builds/updates the Content Tracker for every brand in
config/workspaces.yaml. Each brand gets its OWN dedicated Content
Tracker spreadsheet -- a completely separate sheet, not a tab inside a
shared one -- matching the same per-brand-secret pattern already used
for the Refunnel export spreadsheets.

Deliberately entirely separate from run_daily_sync.py -- different
schedule, no Playwright/browser involved at all (pure Google Sheets
read + write), so it can run independently and much more often if
wanted. Meant to run a couple of hours AFTER the main export, so each
brand's Master Data has had a chance to finish updating first.

Required env vars:
    GOOGLE_SERVICE_ACCOUNT_JSON -- same service account used by the
        main export pipeline (needs edit access to both every brand's
        Content Tracker spreadsheet and every brand's own Master Data
        spreadsheet).
    One tracker_spreadsheet_id_secret env var per brand that has one
        set in config/workspaces.yaml (e.g.
        CONTENT_TRACKER_SPREADSHEET_ID_DUDEROBE) -- each a brand-new
        sheet, NOT that brand's own Refunnel export sheet.

A brand is skipped (with a clear log line, not a failure) if:
    - it has no tracker_spreadsheet_id_secret configured, or that env
      var isn't set,
    - its Refunnel spreadsheet_id_secret env var isn't set, or
    - that spreadsheet has no "Master Data" tab yet (hasn't been run
      through the main export pipeline yet).
This means Defi Snacks/Kelson can sit in config/workspaces.yaml with
nothing configured yet, and this script just quietly does nothing for
them until they're set up, rather than failing the whole run over
brands that aren't ready.
"""
from __future__ import annotations

import os
import sys
import traceback

import gspread
import gspread.exceptions
import yaml

import content_tracker
import parse_refunnel
import sheets_sync

CONFIG_PATH = "config/workspaces.yaml"
MASTER_DATA_TAB = "Master Data"
# Every brand's Content Tracker has exactly one tab, and it always uses
# this name -- unlike the old shared-spreadsheet design, the brand name
# no longer needs to double as the tab name to keep brands apart, but
# keeping a fixed, recognizable name is simpler than inventing a new one.
TRACKER_TAB = "Content Tracker"


def load_workspaces(path: str = CONFIG_PATH) -> list:
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data.get("workspaces", [])


def propagate_reviewed_to_master(target_rows: dict, master_client: sheets_sync.GspreadSheetsClient) -> int:
    """After building this run's Content Tracker rows, push any
    "Reviewed" marking back into Master Data's own Reviewed column --
    confirmed real want: you review content in the richer Content
    Tracker view (with Product/Theme/Content Type context to help
    decide), and want that marking to also drive Master Data's EXISTING
    Human Review mechanism (see apply_human_review.py /
    run_daily_sync.py's own use of it) without typing "Yes" in two
    different sheets.

    Only pushes a value when Reviewed is genuinely non-blank -- never
    writes a blank over anything, and never touches any column other
    than Reviewed. Silently does nothing for an id if Master Data
    doesn't have a "Reviewed" column yet at all -- that's still a
    one-time manual setup step, same as it's always been.

    Only writes cells whose value has actually CHANGED -- confirmed
    real bug this fixes: this used to call update_single_cell() once
    per reviewed row unconditionally, re-reading the entire sheet
    twice per row every single run even when nothing had changed since
    yesterday. As reviewed rows accumulated over time that eventually
    exceeded Google's read quota and failed the whole run with a 429.
    Now reads Master Data's current Reviewed column ONCE, diffs
    against it, and writes only the rows that genuinely need it in a
    single batched call.

    Returns how many cells were actually updated, for logging.
    """
    current_master_values = sheets_sync.read_column_values(master_client, "Reviewed")
    updates = {}
    for media_id, row in target_rows.items():
        reviewed_value = (row.get("Reviewed") or "").strip()
        if parse_refunnel.is_reviewed_value(reviewed_value) and current_master_values.get(media_id) != reviewed_value:
            updates[media_id] = reviewed_value
    return master_client.update_cells_by_id(updates, "Reviewed")


def sync_one_brand(gc: "gspread.Client", brand_config: dict) -> None:
    brand = brand_config["name"]
    secret_name = brand_config["spreadsheet_id_secret"]
    source_spreadsheet_id = os.environ.get(secret_name)

    if not source_spreadsheet_id:
        print(f"{brand}: skipping -- {secret_name} isn't set.")
        return

    tracker_secret_name = brand_config.get("tracker_spreadsheet_id_secret")
    if not tracker_secret_name:
        print(f"{brand}: skipping -- no tracker_spreadsheet_id_secret configured "
              f"in config/workspaces.yaml for this brand's Content Tracker.")
        return
    tracker_spreadsheet_id = os.environ.get(tracker_secret_name)
    if not tracker_spreadsheet_id:
        print(f"{brand}: skipping -- {tracker_secret_name} isn't set.")
        return

    source_sh = sheets_sync.retry_on_transient_error(gc.open_by_key, source_spreadsheet_id)
    try:
        master_ws = source_sh.worksheet(MASTER_DATA_TAB)
    except gspread.exceptions.WorksheetNotFound:
        print(f"{brand}: skipping -- no '{MASTER_DATA_TAB}' tab yet "
              f"(hasn't been run through the main export pipeline).")
        return

    master_client = sheets_sync.GspreadSheetsClient(master_ws)
    master_all = master_client.read_all()
    if not master_all:
        print(f"{brand}: skipping -- '{MASTER_DATA_TAB}' tab is empty.")
        return
    master_header, master_data = master_all[0], master_all[1:]
    master_rows = sheets_sync.index_by_id(master_header, master_data, "id")

    # This brand's OWN dedicated Content Tracker spreadsheet -- opened
    # fresh here rather than passed in, since it's now a different
    # spreadsheet per brand instead of one shared spreadsheet with a
    # tab per brand.
    tracker_sh = sheets_sync.retry_on_transient_error(gc.open_by_key, tracker_spreadsheet_id)
    tracker_ws = sheets_sync.get_or_create_worksheet(tracker_sh, TRACKER_TAB)
    tracker_client = sheets_sync.GspreadSheetsClient(tracker_ws)
    tracker_all = tracker_client.read_all()
    tracker_header = tracker_all[0] if tracker_all else list(content_tracker.TRACKER_COLUMNS)
    tracker_data = tracker_all[1:] if len(tracker_all) > 1 else []
    existing_tracker_rows = sheets_sync.index_by_id(tracker_header, tracker_data, "id")

    target_rows = content_tracker.build_tracker_target_rows(master_rows, brand, existing_tracker_rows)

    reviewed_from_master = content_tracker.merge_reviewed_from_master(target_rows, master_rows)
    if reviewed_from_master:
        print(f"{brand}: pulled {reviewed_from_master} 'Reviewed' marking(s) in from Master Data.")

    reviewed_propagated = propagate_reviewed_to_master(target_rows, master_client)
    if reviewed_propagated:
        print(f"{brand}: propagated {reviewed_propagated} 'Reviewed' marking(s) back to Master Data.")

    # never_delete=True: same protection as Master Data -- a row already
    # in the tracker is never dropped just because it's momentarily
    # missing from master_rows (e.g. mid-run inconsistency).
    # sort_key="Created At": newest content at the top, same policy as
    # Master Data, safe here because Created At is the same ISO-format
    # timestamp Master Data uses.
    summary = sheets_sync.sync_tab(
        tracker_client, content_tracker.TRACKER_COLUMNS, target_rows,
        never_delete=True, sort_key="Created At", sort_reverse=True,
    )
    print(f"{brand}: wrote {summary['rows_written']} rows "
          f"(carried forward: {summary['rows_carried_forward']})")


def main() -> int:
    # Belt-and-suspenders alongside `python -u` in the workflow YML --
    # see run_daily_sync.py's main() for the full explanation. Same
    # fix, same reasoning, applied here too for consistency.
    sys.stdout.reconfigure(line_buffering=True)

    service_account_path = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")

    if not service_account_path:
        print("Missing required env var: GOOGLE_SERVICE_ACCOUNT_JSON.", file=sys.stderr)
        return 1

    try:
        gc = gspread.service_account(filename=service_account_path)

        workspaces = load_workspaces()
        for brand_config in workspaces:
            sync_one_brand(gc, brand_config)

        return 0
    except Exception as e:
        print(f"FAILURE: {type(e).__name__}: {e}\n{traceback.format_exc()}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

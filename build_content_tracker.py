"""
build_content_tracker.py

Builds/updates the Content Tracker -- a separate Google Sheet from any
Refunnel-export spreadsheet, with one tab per brand (Duderobe,
Swoveralls, Defi Snacks, Kelson -- whatever's in config/workspaces.yaml).
Each brand's tab is derived from that brand's own Master Data tab, using
content_tracker.py's freeze/refresh/manual column policy.

Deliberately entirely separate from run_daily_sync.py -- different
schedule, no Playwright/browser involved at all (pure Google Sheets
read + write), so it can run independently and much more often if
wanted. Meant to run a couple of hours AFTER the main export, so each
brand's Master Data has had a chance to finish updating first.

Required env vars:
    GOOGLE_SERVICE_ACCOUNT_JSON -- same service account used by the
        main export pipeline (needs edit access to both the tracker
        spreadsheet and every brand's own Master Data spreadsheet).
    CONTENT_TRACKER_SPREADSHEET_ID -- the ID of the dedicated tracker
        spreadsheet (a brand-new sheet, NOT any brand's own export sheet).

A brand is skipped (with a clear log line, not a failure) if:
    - its spreadsheet_id_secret env var isn't set, or
    - that spreadsheet has no "Master Data" tab yet (hasn't been run
      through the main export pipeline yet).
This means Swoveralls/Defi Snacks/Kelson can sit in
config/workspaces.yaml with nothing configured yet, and this script
just quietly does nothing for them until they're set up, rather than
failing the whole run over brands that aren't ready.
"""
from __future__ import annotations

import os
import sys
import traceback

import gspread
import gspread.exceptions
import yaml

import content_tracker
import sheets_sync

CONFIG_PATH = "config/workspaces.yaml"
MASTER_DATA_TAB = "Master Data"


def load_workspaces(path: str = CONFIG_PATH) -> list:
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data.get("workspaces", [])


def sync_one_brand(gc: "gspread.Client", tracker_sh, brand_config: dict) -> None:
    brand = brand_config["name"]
    secret_name = brand_config["spreadsheet_id_secret"]
    source_spreadsheet_id = os.environ.get(secret_name)

    if not source_spreadsheet_id:
        print(f"{brand}: skipping -- {secret_name} isn't set.")
        return

    source_sh = gc.open_by_key(source_spreadsheet_id)
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

    tracker_ws = sheets_sync.get_or_create_worksheet(tracker_sh, brand)
    tracker_client = sheets_sync.GspreadSheetsClient(tracker_ws)
    tracker_all = tracker_client.read_all()
    tracker_header = tracker_all[0] if tracker_all else list(content_tracker.TRACKER_COLUMNS)
    tracker_data = tracker_all[1:] if len(tracker_all) > 1 else []
    existing_tracker_rows = sheets_sync.index_by_id(tracker_header, tracker_data, "id")

    target_rows = content_tracker.build_tracker_target_rows(master_rows, brand, existing_tracker_rows)

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
    tracker_spreadsheet_id = os.environ.get("CONTENT_TRACKER_SPREADSHEET_ID")

    if not service_account_path or not tracker_spreadsheet_id:
        print("Missing required env vars: need GOOGLE_SERVICE_ACCOUNT_JSON, "
              "CONTENT_TRACKER_SPREADSHEET_ID.", file=sys.stderr)
        return 1

    try:
        gc = gspread.service_account(filename=service_account_path)
        tracker_sh = gc.open_by_key(tracker_spreadsheet_id)

        workspaces = load_workspaces()
        for brand_config in workspaces:
            sync_one_brand(gc, tracker_sh, brand_config)

        return 0
    except Exception as e:
        print(f"FAILURE: {type(e).__name__}: {e}\n{traceback.format_exc()}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

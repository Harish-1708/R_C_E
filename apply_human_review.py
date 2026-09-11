"""
apply_human_review.py

Moves rows marked "Reviewed" (a manual column you add to Master Data
yourself -- type "Yes" on any row) out of whichever Usage Rights tab
they're currently in and into Human Review instead -- entirely from
what's ALREADY in each brand's own Google Sheet.

Deliberately separate from run_daily_sync.py: this step is pure Google
Sheets read/write, no Refunnel login, no browser, no fresh CSV export
needed at all. So marking a row reviewed doesn't require waiting for
(or re-triggering) the next full Refunnel scrape just to see it move --
this can run fast and independently, same reasoning as
build_content_tracker.py being split out from the main export.

Note: this mechanism (the "Reviewed" column, apply_human_review_flags,
build_human_review_rows) already existed and is still used by
run_daily_sync.py's own daily run too -- this script doesn't replace
that, it's an additional, faster way to trigger the same effect without
waiting for a full Refunnel sync.

Required env vars:
    GOOGLE_SERVICE_ACCOUNT_JSON -- same service account used elsewhere.
    One SPREADSHEET_ID_* secret per brand, matching
    config/workspaces.yaml's spreadsheet_id_secret values.

A brand is skipped (not failed) if its secret isn't set or it has no
Master Data tab yet -- same pattern as build_content_tracker.py.
"""
from __future__ import annotations

import os
import sys
import traceback

import gspread
import gspread.exceptions
import yaml

import parse_refunnel
import sheets_sync

CONFIG_PATH = "config/workspaces.yaml"
MASTER_DATA_TAB = "Master Data"


def load_workspaces(path: str = CONFIG_PATH) -> list:
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data.get("workspaces", [])


def build_result_from_sheet(master_rows: dict) -> parse_refunnel.ParseResult:
    """Reconstructs a ParseResult purely from what's already in Master
    Data's sheet -- no CSV involved, unlike run_daily_sync.py's own
    parse. Each row is placed in the matching Usage Rights bucket based
    on its CURRENT rights_status, using the same RIGHTS_STATUS_MAP a
    real CSV parse would use, so apply_human_review_flags() works
    identically either way."""
    result = parse_refunnel.ParseResult()
    result.master = master_rows
    for media_id, row in master_rows.items():
        bucket_name = parse_refunnel.RIGHTS_STATUS_MAP.get(row.get("rights_status", ""))
        if bucket_name:
            getattr(result, bucket_name)[media_id] = row
    return result


def apply_human_review_for_brand(gc: "gspread.Client", brand_config: dict) -> None:
    brand = brand_config["name"]
    secret_name = brand_config["spreadsheet_id_secret"]
    spreadsheet_id = os.environ.get(secret_name)

    if not spreadsheet_id:
        print(f"{brand}: skipping -- {secret_name} isn't set.")
        return

    sh = sheets_sync.retry_on_transient_error(gc.open_by_key, spreadsheet_id)
    try:
        master_ws = sh.worksheet(MASTER_DATA_TAB)
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

    result = build_result_from_sheet(master_rows)

    reviewed_values = sheets_sync.read_column_values(master_client, "Reviewed")
    reviewed_ids = {
        mid for mid, val in reviewed_values.items()
        if val.strip().lower() in ("yes", "y", "true", "1")
    }
    moved = parse_refunnel.apply_human_review_flags(result, reviewed_ids)
    print(f"{brand}: {moved} row(s) moved into Human Review this run.")

    # Read the Human Review tab's CURRENT moved_to_human_review_at
    # values before rebuilding it -- confirmed real want: knowing WHEN
    # something was pushed here, to filter by it, which a full rewrite
    # would otherwise silently reset to "now" every run.
    human_review_ws = sheets_sync.get_or_create_worksheet(sh, "Human Review")
    human_review_client = sheets_sync.GspreadSheetsClient(human_review_ws)
    existing_moved_dates = sheets_sync.read_column_values(human_review_client, "moved_to_human_review_at")
    human_review_rows = parse_refunnel.build_human_review_rows(result, existing_moved_dates)

    # Usage Rights tabs and Human Review are expected to shrink/grow as
    # rows move between them by design -- no never_delete/max_shrink
    # protection here, same as run_daily_sync.py's own equivalent step.
    tab_plan = [
        ("Usage Rights - Approved", parse_refunnel.MASTER_COLUMNS, result.rights_approved),
        ("Usage Rights - Requested", parse_refunnel.MASTER_COLUMNS, result.rights_requested),
        ("Usage Rights - Declined", parse_refunnel.MASTER_COLUMNS, result.rights_declined),
        ("Human Review", parse_refunnel.HUMAN_REVIEW_COLUMNS, human_review_rows),
    ]
    for title, columns, rows in tab_plan:
        ws = sheets_sync.get_or_create_worksheet(sh, title)
        client = sheets_sync.GspreadSheetsClient(ws)
        summary = sheets_sync.sync_tab(client, columns, rows)
        print(f"{brand} / {title}: wrote {summary['rows_written']} row(s)")


def main() -> int:
    sys.stdout.reconfigure(line_buffering=True)
    service_account_path = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")

    if not service_account_path:
        print("Missing required env var: GOOGLE_SERVICE_ACCOUNT_JSON.", file=sys.stderr)
        return 1

    try:
        gc = gspread.service_account(filename=service_account_path)
        workspaces = load_workspaces()
        for brand_config in workspaces:
            apply_human_review_for_brand(gc, brand_config)
        return 0
    except Exception as e:
        print(f"FAILURE: {type(e).__name__}: {e}\n{traceback.format_exc()}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

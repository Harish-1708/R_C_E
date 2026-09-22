"""
sync_campaigns.py

Refunnel's CSV export has no campaign field at all (confirmed against
a real fresh export) -- the only way to know which campaign(s) a post
belongs to is the Campaign filter in the UI, so this discovers the
current campaign list dynamically (never hardcoded -- 22 exist today,
more will be added over time) and, for each one, applies just that
filter and exports the resulting CSV to find its members.

A post can belong to zero, one, or several campaigns, and most of the
~6000+ posts belong to none at all -- both confirmed real, so the
result is a genuine many-to-many relationship, not a single lookup.
Stored as one "campaigns" column on Master Data (an extra column, same
pattern as "Reviewed" and "drive_uploaded_at" -- not part of
MASTER_COLUMNS, carried forward automatically by sync_tab), with
multiple campaign names comma-joined in one cell rather than one
column per campaign, since a fixed set of campaign columns would need
editing the schema every time a new campaign appears.

Unlike "Reviewed" or "drive_uploaded_at", campaign membership is NOT a
"mark once, never touch again" fact -- a post's campaigns can change
over time (added to a new one, removed from an old one), so every run
recomputes the full picture and corrects any post whose value is now
wrong, including clearing a campaign that no longer applies. Only
cells that actually changed get written (see propagate step below),
same quota-conscious reasoning as the Reviewed-sync fix.

Deliberately its own separate, slow-running workflow: discovering N
campaigns and running a full filter-scroll-export cycle for each one
is real, sequential browser time that has nothing to do with the daily
export or Content Tracker, so it runs on a slower, independent
schedule (weekly, not daily) rather than competing with them.

Required env vars: same as download_approved_to_drive.py -- REFUNNEL_EMAIL,
GOOGLE_SERVICE_ACCOUNT_JSON, GMAIL_ADDRESS, GMAIL_APP_PASSWORD, and one
spreadsheet_id_secret env var per brand.

Nothing here has been run end-to-end against live Refunnel -- the
Campaign-filter selectors in refunnel_export.py (CAMPAIGN_FILTER_BUTTON_SELECTOR
and friends) are best guesses from a single screenshot, not verified
against real markup like export_media_csv's selectors are. See README
"Testing the campaign sync" before trusting the schedule unattended --
in particular, run once manually and check the log for how many
campaigns list_available_campaigns() actually found before trusting
the rest.
"""
from __future__ import annotations

import os
import re
import sys
import traceback
from pathlib import Path

import gspread
import gspread.exceptions
import yaml

import parse_refunnel
import refunnel_auth
import refunnel_export
import sheets_sync

CONFIG_PATH = "config/workspaces.yaml"
MASTER_DATA_TAB = "Master Data"
DOWNLOAD_DIR = "downloads/campaign_sync"


def load_workspaces(path: str = CONFIG_PATH) -> list:
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data.get("workspaces", [])


def _ids_from_csv(csv_path: str) -> set:
    """The one thing needed from a campaign's filtered export: which
    ids are in it. Everything else in that CSV is already captured by
    the main daily export -- this only cares about membership."""
    result = parse_refunnel.parse_media_csv(csv_path)
    return set(result.master.keys())


def sync_campaigns_for_brand(
    gc: "gspread.Client", brand_config: dict, email: str, known_workspace_names: list
) -> None:
    brand = brand_config["name"]
    refunnel_workspace_name = brand_config.get("refunnel_workspace_name", brand)
    secret_name = brand_config["spreadsheet_id_secret"]
    spreadsheet_id = os.environ.get(secret_name)
    if not spreadsheet_id:
        print(f"{brand}: skipping -- {secret_name} isn't set.")
        return

    sh = sheets_sync.retry_on_transient_error(gc.open_by_key, spreadsheet_id)
    try:
        master_ws = sh.worksheet(MASTER_DATA_TAB)
    except gspread.exceptions.WorksheetNotFound:
        print(f"{brand}: skipping -- no '{MASTER_DATA_TAB}' tab yet.")
        return

    master_client = sheets_sync.GspreadSheetsClient(master_ws)
    existing_campaigns = sheets_sync.read_column_values(master_client, "campaigns")

    download_dir = f"{DOWNLOAD_DIR}/{brand.replace(' ', '_')}"
    os.makedirs(download_dir, exist_ok=True)

    p, browser, context = refunnel_auth.load_or_refresh_session(email=email)
    try:
        page = context.new_page()
        page.goto(refunnel_auth.refunnel_social_listening_url())
        refunnel_export.select_workspace(page, refunnel_workspace_name, known_workspace_names)

        campaigns = refunnel_export.list_available_campaigns(page, debug_dir=f"{download_dir}/debug")
        print(f"{brand}: found {len(campaigns)} campaign(s) -- {', '.join(campaigns) if campaigns else '(none)'}")

        campaign_to_ids: dict = {}
        for campaign_name in campaigns:
            try:
                refunnel_export.filter_by_campaign(page, campaign_name, debug_dir=f"{download_dir}/debug")

                # HARD safety gate, checked FIRST -- confirmed real,
                # serious bug this prevents: a live run reported 2795
                # posts (the entire unfiltered library) for "SheRobe
                # Content Campaign", while a manual check of the exact
                # same campaign showed genuinely zero results. The
                # filter silently failed to take effect that one time,
                # and nothing caught it before this -- it would have
                # mistagged all 2795 posts as belonging to a campaign
                # they were never in. Never trust scroll/export output
                # without first confirming the filter is genuinely
                # showing THIS campaign, not "everything".
                if not refunnel_export.filter_is_genuinely_active(page, campaign_name):
                    raise refunnel_export.ExportError(
                        f"the Campaign filter doesn't appear to be showing {campaign_name!r} "
                        f"after applying it -- refusing to scroll/export, since that would risk "
                        f"exporting the unfiltered library and mistagging everything in it as "
                        f"belonging to this campaign."
                    )

                # Confirmed real, not a guess: live debug screenshots
                # showed Refunnel's own "No results for these
                # filters(s)" empty state for every one of Duderobe's
                # 5 campaigns, matching your own manual check exactly.
                # That's a genuine, valid "0 posts in this campaign"
                # answer -- there's no "<n> of <total> media" counter
                # to find at all in that state, so scroll_to_load_all()
                # would only ever fail trying to find one. Checked
                # BEFORE attempting to scroll/export, so a real empty
                # campaign is recorded correctly instead of logged as
                # a failure and skipped.
                if refunnel_export.has_no_results_for_filter(page):
                    campaign_to_ids[campaign_name] = set()
                    print(f"{brand} / {campaign_name!r}: 0 post(s) (Refunnel shows no matching content).")
                    continue

                refunnel_export.scroll_to_load_all(page)
                csv_path = refunnel_export.export_media_csv(page, download_dir)
                ids = _ids_from_csv(csv_path)
                campaign_to_ids[campaign_name] = ids
                print(f"{brand} / {campaign_name!r}: {len(ids)} post(s).")
            except Exception as e:
                print(f"{brand}: couldn't process campaign {campaign_name!r}: "
                      f"{type(e).__name__}: {e}. Skipping it this run.")
                # A snapshot per FAILING campaign, not just filter_by_campaign's
                # own post-Apply capture -- confirmed real need: a live run had
                # every campaign fail differently (one stuck on a disabled Apply
                # button, others stalling scroll_to_load_all identically at
                # "20 of 2795", the same as the full unfiltered library), and
                # telling those apart needs to see the page at the MOMENT each
                # one actually failed, not just after Apply was clicked.
                try:
                    safe_name = re.sub(r"[^A-Za-z0-9]+", "_", campaign_name)[:60]
                    debug_dir_path = Path(f"{download_dir}/debug")
                    debug_dir_path.mkdir(parents=True, exist_ok=True)
                    page.screenshot(path=str(debug_dir_path / f"failure_{safe_name}.png"), full_page=True)
                    (debug_dir_path / f"failure_{safe_name}.html").write_text(page.content(), encoding="utf-8")
                except Exception as snapshot_error:
                    print(f"{brand}: couldn't save a failure snapshot for {campaign_name!r} either: "
                          f"{snapshot_error}")

        refunnel_export.clear_all_filters(page)

    finally:
        try:
            page.close()
        except Exception:
            pass
        try:
            browser.close()
        except Exception:
            pass
        try:
            p.stop()
        except Exception:
            pass

    fresh = parse_refunnel.build_campaign_membership(campaign_to_ids)

    # Only writes what actually changed -- a post's fresh value differs
    # from what's currently in the sheet, including correctly writing
    # a BLANK when a post no longer belongs to anything it used to.
    # Unlike Reviewed/drive_uploaded_at, campaign membership is a
    # refreshed snapshot, not an append-only fact, so clearing a stale
    # value here is correct, not a bug.
    all_relevant_ids = set(existing_campaigns) | set(fresh)
    updates = {
        media_id: fresh.get(media_id, "")
        for media_id in all_relevant_ids
        if (existing_campaigns.get(media_id) or "") != fresh.get(media_id, "")
    }
    written = master_client.update_cells_by_id(updates, "campaigns")
    print(f"{brand}: updated campaign tags on {written} row(s).")


def main() -> int:
    sys.stdout.reconfigure(line_buffering=True)

    email = os.environ.get("REFUNNEL_EMAIL")
    service_account_path = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")

    if not email or not service_account_path:
        print("Missing required env vars: need REFUNNEL_EMAIL, GOOGLE_SERVICE_ACCOUNT_JSON.",
              file=sys.stderr)
        return 1

    try:
        gc = gspread.service_account(filename=service_account_path)

        workspaces = load_workspaces()
        known_workspace_names = [w.get("refunnel_workspace_name", w["name"]) for w in workspaces]
        for brand_config in workspaces:
            sync_campaigns_for_brand(gc, brand_config, email, known_workspace_names)

        return 0
    except Exception as e:
        print(f"FAILURE: {type(e).__name__}: {e}\n{traceback.format_exc()}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

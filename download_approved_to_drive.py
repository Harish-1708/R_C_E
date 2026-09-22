"""
download_approved_to_drive.py

For every brand with a Drive folder configured, saves any Approved
(GRANTED usage rights) video not yet in Drive, using Refunnel's OWN
native "Upload to Google Drive" feature -- confirmed real, replacing
an earlier custom download-then-upload design entirely. Refunnel
handles the actual file transfer server-side (already connected from
your own account); this script only triggers it, then finds and
renames the resulting file to "<Brand> | @<handle> | <media_id>.mp4",
since Refunnel's own upload doesn't offer naming control.

Deliberately its own separate script/workflow, same reasoning as
Content Tracker and Human Review being split out from the main sync:
this is genuinely slow (one UI flow per video, plus waiting for each
upload to land in Drive), so it runs on its own schedule and never
blocks or slows down the main daily export.

Incremental by design, matching the explicit requirement that new
approvals get ADDED, not "delete the old and start from the
beginning": each brand's Master Data gets an extra "drive_uploaded_at"
column (not part of MASTER_COLUMNS, carried forward automatically by
sync_tab's existing extra-column preservation, same as "Reviewed"). A
media_id already marked there is never reprocessed. Marked one at a
time, immediately after each confirmed upload -- not batched at the
end -- so a crash partway through a long run doesn't lose track of
what's already safely in Drive.

BATCH_SIZE bounds how many videos one run attempts, so a large backlog
is worked through gradually across several scheduled runs rather than
risking one run timing out partway through; already-uploaded ids are
always skipped on the next run regardless, so nothing is lost or
repeated by capping this.

Required env vars:
    REFUNNEL_EMAIL, GOOGLE_SERVICE_ACCOUNT_JSON, GMAIL_ADDRESS,
    GMAIL_APP_PASSWORD -- same as run_daily_sync.py, for the Refunnel
        login and Gmail-OTP fallback.
    One spreadsheet_id_secret env var per brand (that brand's Refunnel
        export sheet) and one drive_folder_id_secret env var per brand
        (the target Drive folder's real Drive API id, used to search
        for and rename the file Refunnel uploaded) -- both from
        config/workspaces.yaml. drive_folder_name (also in
        config/workspaces.yaml, not a secret -- it's not sensitive) is
        the folder's name AS SHOWN in Refunnel's own "All folders"
        picker, used to select it there; defaults to
        "Refunnel - <brand name>" if not set. A brand missing the
        spreadsheet or folder-id secret is skipped cleanly, not a
        failure, same pattern as apply_human_review.py.

Nothing here has been run end-to-end against a live Refunnel/Drive
account -- see README "Testing the Drive backfill" before trusting the
schedule unattended. The card-menu and modal selectors in
refunnel_export.py's trigger_native_drive_upload() are best guesses
(see its own docstring) and will very likely need adjusting against
the real page, the same way earlier UI-automation features in this
project did.
"""
from __future__ import annotations

import os
import sys
import traceback
from datetime import datetime, timezone

import gspread
import gspread.exceptions
import yaml

import drive_upload
import parse_refunnel
import refunnel_auth
import refunnel_export
import sheets_sync

CONFIG_PATH = "config/workspaces.yaml"
MASTER_DATA_TAB = "Master Data"
DOWNLOAD_DIR = "downloads/drive_backfill"
BATCH_SIZE = int(os.environ.get("DRIVE_BACKFILL_BATCH_SIZE", "50"))
DRIVE_UPLOAD_WAIT_SECONDS = float(os.environ.get("DRIVE_UPLOAD_WAIT_SECONDS", "30"))


def load_workspaces(path: str = CONFIG_PATH) -> list:
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data.get("workspaces", [])


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def process_one_brand(
    gc: "gspread.Client", brand_config: dict, drive_service, email: str, known_workspace_names: list
) -> None:
    brand = brand_config["name"]
    refunnel_workspace_name = brand_config.get("refunnel_workspace_name", brand)
    secret_name = brand_config["spreadsheet_id_secret"]
    spreadsheet_id = os.environ.get(secret_name)
    if not spreadsheet_id:
        print(f"{brand}: skipping -- {secret_name} isn't set.")
        return

    drive_secret_name = brand_config.get("drive_folder_id_secret")
    if not drive_secret_name:
        print(f"{brand}: skipping -- no drive_folder_id_secret configured "
              f"in config/workspaces.yaml for this brand's Drive backfill.")
        return
    folder_id = os.environ.get(drive_secret_name)
    if not folder_id:
        print(f"{brand}: skipping -- {drive_secret_name} isn't set.")
        return

    drive_folder_name = brand_config.get("drive_folder_name") or f"Refunnel - {brand}"

    sh = sheets_sync.retry_on_transient_error(gc.open_by_key, spreadsheet_id)
    try:
        master_ws = sh.worksheet(MASTER_DATA_TAB)
    except gspread.exceptions.WorksheetNotFound:
        print(f"{brand}: skipping -- no '{MASTER_DATA_TAB}' tab yet.")
        return

    master_client = sheets_sync.GspreadSheetsClient(master_ws)
    master_all = master_client.read_all()
    if not master_all:
        print(f"{brand}: skipping -- '{MASTER_DATA_TAB}' tab is empty.")
        return
    master_header, master_data = master_all[0], master_all[1:]
    master_rows = sheets_sync.index_by_id(master_header, master_data, "id")

    existing_uploads = sheets_sync.read_column_values(master_client, "drive_uploaded_at")
    to_upload = parse_refunnel.rows_needing_drive_upload(master_rows, existing_uploads)

    if not to_upload:
        print(f"{brand}: nothing new to upload -- every Approved video is already in Drive.")
        return

    target_ids = list(to_upload.keys())[:BATCH_SIZE]
    print(f"{brand}: {len(to_upload)} Approved video(s) not yet in Drive -- "
          f"processing {len(target_ids)} this run (batch size {BATCH_SIZE}), "
          f"target folder {drive_folder_name!r}.")

    debug_dir = f"{DOWNLOAD_DIR}/{brand.replace(' ', '_')}/debug"

    p, browser, context = refunnel_auth.load_or_refresh_session(email=email)
    try:
        page = context.new_page()
        refunnel_export.goto_social_listening_for_workspace(page, refunnel_workspace_name, known_workspace_names)

        uploaded, not_yet_confirmed, failed = 0, 0, 0
        for media_id in target_ids:
            row = master_rows[media_id]
            username = row.get("username", "") or "unknown"
            try:
                trigger_ts = _now_iso()
                triggered = refunnel_export.trigger_native_drive_upload(
                    page, media_id, drive_folder_name, debug_dir=debug_dir
                )
                if not triggered:
                    print(f"{brand}: couldn't locate media_id={media_id!r} on the page -- "
                          f"leaving it unmarked, will retry on a future run.")
                    failed += 1
                    continue

                filename = parse_refunnel.build_drive_filename(brand, username, media_id)
                fragment = parse_refunnel.drive_match_fragment(media_id)
                file_id = drive_upload.find_and_rename_uploaded_file(
                    drive_service, folder_id, fragment, filename,
                    uploaded_after=trigger_ts, max_wait_seconds=DRIVE_UPLOAD_WAIT_SECONDS,
                )

                if file_id is None:
                    print(f"{brand}: triggered the upload for media_id={media_id!r}, but it "
                          f"hadn't shown up in Drive within {DRIVE_UPLOAD_WAIT_SECONDS:.0f}s -- "
                          f"leaving it unmarked, will retry on a future run rather than "
                          f"assume it failed.")
                    not_yet_confirmed += 1
                    continue

                # Marked immediately, one at a time -- not batched at
                # the end -- so a crash partway through a long run
                # doesn't lose track of videos already confirmed in
                # Drive.
                master_client.update_single_cell(media_id, "drive_uploaded_at", _now_iso())
                uploaded += 1
            except Exception as e:
                print(f"{brand}: couldn't process media_id={media_id!r}: {type(e).__name__}: {e}")
                failed += 1

        print(f"{brand}: confirmed {uploaded}, not yet confirmed {not_yet_confirmed}, "
              f"failed {failed}, {len(to_upload) - len(target_ids)} still pending for a future run.")
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
        drive_service = drive_upload.build_drive_service(service_account_path)

        workspaces = load_workspaces()
        known_workspace_names = [w.get("refunnel_workspace_name", w["name"]) for w in workspaces]
        for brand_config in workspaces:
            process_one_brand(gc, brand_config, drive_service, email, known_workspace_names)

        return 0
    except Exception as e:
        print(f"FAILURE: {type(e).__name__}: {e}\n{traceback.format_exc()}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

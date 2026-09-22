"""
download_approved_to_drive.py

For every brand with a Drive folder configured, downloads any Approved
(GRANTED usage rights) video not yet uploaded, and uploads it to that
brand's Google Drive folder as "<Brand> | @<handle> | <media_id>.mp4".

Deliberately its own separate script/workflow, same reasoning as
Content Tracker and Human Review being split out from the main sync:
this can be genuinely slow (downloading and uploading real video files,
not just reading/writing a spreadsheet), so it runs on its own schedule
and never blocks or slows down the main daily export.

Incremental by design, matching the explicit requirement that new
approvals get ADDED, not "delete the old and start from the
beginning": each brand's Master Data gets an extra "drive_uploaded_at"
column (not part of MASTER_COLUMNS, carried forward automatically by
sync_tab's existing extra-column preservation, same as "Reviewed"). A
media_id already marked there is never re-downloaded or re-uploaded.
Marked one at a time, immediately after each successful upload -- not
batched at the end -- so a crash partway through a long run doesn't
lose track of what's already safely in Drive.

BATCH_SIZE bounds how many videos one run attempts, so a large backlog
(hundreds of already-Approved videos) is worked through gradually
across several scheduled runs rather than risking one run timing out
partway through; already-uploaded ids are always skipped on the next
run regardless, so nothing is lost or repeated by capping this.

Required env vars:
    REFUNNEL_EMAIL, GOOGLE_SERVICE_ACCOUNT_JSON, GMAIL_ADDRESS,
    GMAIL_APP_PASSWORD -- same as run_daily_sync.py, for the Refunnel
        login and Gmail-OTP fallback.
    One spreadsheet_id_secret env var per brand (that brand's Refunnel
        export sheet) and one drive_folder_id_secret env var per brand
        (the target Drive folder for that brand's videos) -- both from
        config/workspaces.yaml. A brand missing either is skipped
        cleanly, not a failure, same pattern as apply_human_review.py.

Nothing here has been run end-to-end against a live Refunnel/Drive
account -- see README "Testing the Drive backfill" before trusting the
schedule unattended. In particular, DOWNLOAD_BUTTON_SELECTOR in
refunnel_export.py is a best guess (see its own docstring) and may need
adjusting against the real page.
"""
from __future__ import annotations

import os
import sys
import traceback

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


def load_workspaces(path: str = CONFIG_PATH) -> list:
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data.get("workspaces", [])


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
          f"processing {len(target_ids)} this run (batch size {BATCH_SIZE}).")

    download_dir = f"{DOWNLOAD_DIR}/{brand.replace(' ', '_')}"
    os.makedirs(download_dir, exist_ok=True)

    p, browser, context = refunnel_auth.load_or_refresh_session(email=email)
    try:
        page = context.new_page()
        page.goto(refunnel_auth.refunnel_social_listening_url())
        refunnel_export.select_workspace(page, refunnel_workspace_name, known_workspace_names)

        uploaded, failed = 0, 0
        for media_id in target_ids:
            row = master_rows[media_id]
            username = row.get("username", "") or "unknown"
            local_path = None
            succeeded = False
            try:
                local_path = refunnel_export.download_approved_video(page, media_id, download_dir)
                if local_path is None:
                    print(f"{brand}: couldn't locate media_id={media_id!r} on the page -- "
                          f"leaving it unmarked, will retry on a future run.")
                    failed += 1
                    continue

                filename = parse_refunnel.build_drive_filename(brand, username, media_id, local_path.suffix.lstrip("."))
                drive_upload.upload_file(drive_service, str(local_path), filename, folder_id)

                # Marked immediately, one at a time -- not batched at
                # the end -- so a crash partway through a long run
                # doesn't lose track of videos already safely in Drive.
                master_client.update_single_cell(media_id, "drive_uploaded_at", _now_iso())
                uploaded += 1
                succeeded = True
            except Exception as e:
                print(f"{brand}: couldn't process media_id={media_id!r}: {type(e).__name__}: {e}")
                failed += 1
            finally:
                # Deleted once safely in Drive -- no reason to also
                # keep a local copy. Kept on failure, though: a video
                # that failed partway through is exactly what's worth
                # inspecting from the debug artifact, and deleting it
                # unconditionally (as an earlier version of this
                # function did) would leave nothing to debug a failed
                # run with.
                try:
                    if succeeded and local_path is not None and local_path.exists():
                        local_path.unlink()
                except Exception:
                    pass

        print(f"{brand}: uploaded {uploaded}, failed {failed}, "
              f"{len(to_upload) - len(target_ids)} still pending for a future run.")
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


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


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

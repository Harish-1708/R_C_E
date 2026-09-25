"""
download_approved_to_drive.py

For every brand with a Drive folder configured, downloads any Approved
(GRANTED usage rights) video not yet in Drive directly (Playwright's
own browser download, the same proven mechanism this project already
uses for CSV exports), then uploads it to that brand's Google Drive
folder itself as "<Brand> | @<handle> | <media_id>.mp4" -- the correct
name set at upload time, not applied afterward.

CONFIRMED REAL, direct decision from live evidence: this project spent
a long stretch on Refunnel's own native "Save to Drive" feature
instead. Every piece of that click-through was eventually proven
correct -- confirmed byte-identical to a version that once worked,
confirmed against the right Drive folder, confirmed accepted by
Refunnel's own "Uploading to Google Drive -- it will appear shortly"
toast -- and the file still never landed, across many separate runs,
on a transfer that happens entirely on Refunnel's own servers once
that toast appears. Nothing past that point is something this
codebase can see or control. This script goes back to owning the
whole pipeline directly.

Deliberately its own separate script/workflow, same reasoning as
Content Tracker and Human Review being split out from the main sync:
this is genuinely slow (downloading and uploading real video files,
not just reading/writing a spreadsheet), so it runs on its own
schedule and never blocks or slows down the main daily export.

Incremental by design, matching the explicit requirement that new
approvals get ADDED, not "delete the old and start from the
beginning": each brand's Master Data gets an extra "drive_uploaded_at"
column (not part of MASTER_COLUMNS, carried forward automatically by
sync_tab's existing extra-column preservation, same as "Reviewed"). A
media_id already marked there is never re-downloaded or re-uploaded.
Marked one at a time, immediately after each successful upload -- not
batched at the end -- so a crash partway through a long run doesn't
lose track of what's already safely in Drive.

The queue keeps going past a failing or skipped id to reach real
successes rather than stopping at a fixed slice -- CONFIRMED REAL bug
this avoids repeating: an earlier version took a single, fixed slice
of the first BATCH_SIZE ids every run, so any id that failed stayed
first in line forever, permanently blocking everything behind it.
MAX_ATTEMPTS_PER_RUN and DRIVE_BACKFILL_TIME_BUDGET_MINUTES both bound
how far a single run goes regardless, so a bad batch can't run away.

Required env vars:
    REFUNNEL_EMAIL, GOOGLE_SERVICE_ACCOUNT_JSON, GMAIL_ADDRESS,
    GMAIL_APP_PASSWORD -- same as run_daily_sync.py, for the Refunnel
        login and Gmail-OTP fallback.
    One spreadsheet_id_secret env var per brand (that brand's Refunnel
        export sheet) and one drive_folder_id_secret env var per brand
        (the target Drive folder's real Drive API id) -- both from
        config/workspaces.yaml. A brand missing either is skipped
        cleanly, not a failure, same pattern as apply_human_review.py.

DOWNLOAD_BUTTON_SELECTOR in refunnel_export.py is a reasonable
starting guess (see its own docstring) and may need adjusting against
the real page -- if a run's debug snapshots show it matching the
wrong element or nothing, that specific selector, not this overall
approach, is what needs fixing.
"""
from __future__ import annotations

import os
import sys
import time
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
# CONFIRMED REAL bug this fixes: target_ids used to be a single, fixed
# slice of the first BATCH_SIZE ids in the queue, taken once. Any id
# that fails (couldn't locate, status changed since export) never gets
# drive_uploaded_at set, so it's still first in line on the very next
# run -- meaning a persistently-stuck front of the queue was NEVER
# skipped past. A live run confirmed this exactly: the same 50 ids,
# same order, same 0 successes, across multiple separate runs and
# days, while hundreds of videos sat unprocessed the whole time. This
# caps total ATTEMPTS per run (bounding worst-case runtime even if
# everything fails) while letting the run keep going past
# failures/skips to actually reach BATCH_SIZE real successes -- or
# exhaust the queue trying.
MAX_ATTEMPTS_PER_RUN = int(os.environ.get("DRIVE_BACKFILL_MAX_ATTEMPTS", str(BATCH_SIZE * 8)))
# CONFIRMED REAL risk this closes: MAX_ATTEMPTS_PER_RUN keeping the
# loop going past failures means a run where many items are genuinely
# slow to download/upload could still run long, even though the
# job-level timeout-minutes cap would eventually force-kill it anyway.
# Same proven pattern as email scraping's own SCRAPE_TIME_BUDGET_MINUTES:
# stop cleanly with whatever progress was made, rather than run right up
# against (or past) the external cap. Comfortably inside
# drive-backfill.yml's job timeout.
DRIVE_BACKFILL_TIME_BUDGET_MINUTES = float(os.environ.get("DRIVE_BACKFILL_TIME_BUDGET_MINUTES", "45"))


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

    target_ids = list(to_upload.keys())
    print(f"{brand}: {len(to_upload)} Approved video(s) not yet in Drive -- "
          f"aiming for {BATCH_SIZE} successful upload(s) this run (trying up to "
          f"{min(MAX_ATTEMPTS_PER_RUN, len(target_ids))} of them if needed).")

    download_dir = f"{DOWNLOAD_DIR}/{brand.replace(' ', '_')}"
    debug_dir = f"{download_dir}/debug"
    os.makedirs(download_dir, exist_ok=True)

    p, browser, context = refunnel_auth.load_or_refresh_session(email=email)
    try:
        page = context.new_page()
        # Approved-only grid, confirmed real via the page URL -- far fewer
        # cards to search, and every card is an Approved card.
        refunnel_export.goto_social_listening_for_workspace(
            page, refunnel_workspace_name, known_workspace_names, usage_rights="GRANTED"
        )
        # Reset to the top ONCE for the whole batch, matching the same
        # proven pattern already used for email scraping -- one reset
        # here plus each item's own forward-only search (reset_scroll=
        # False below) covers the whole batch, rather than a full
        # reset-and-rescroll per item.
        refunnel_export.scroll_to_top(page)

        uploaded, already_present, failed, status_changed, attempted = 0, 0, 0, 0, 0
        deadline = time.monotonic() + DRIVE_BACKFILL_TIME_BUDGET_MINUTES * 60
        for media_id in target_ids:
            if uploaded >= BATCH_SIZE:
                break
            if attempted >= MAX_ATTEMPTS_PER_RUN:
                print(f"{brand}: reached the {MAX_ATTEMPTS_PER_RUN}-attempt safety cap for this "
                      f"run with {uploaded} confirmed -- stopping here rather than risking an "
                      f"unbounded run; the rest of the queue is picked up on a future run.")
                break
            if time.monotonic() >= deadline:
                print(f"{brand}: reached this run's {DRIVE_BACKFILL_TIME_BUDGET_MINUTES:.0f}-minute "
                      f"time budget with {uploaded} confirmed -- stopping cleanly here rather than "
                      f"risking a run right up against the job's own hard timeout; the rest of the "
                      f"queue is picked up on a future run.")
                break
            attempted += 1
            row = master_rows[media_id]
            username = row.get("username", "") or "unknown"
            local_path = None
            succeeded = False
            try:
                # CONFIRMED REAL gap this closes: a run that downloaded
                # and uploaded successfully but crashed before marking
                # drive_uploaded_at would otherwise download and
                # upload this SAME media_id again on the next run -- a
                # real duplicate file. The final filename already
                # embeds media_id, so checking for it directly, before
                # ever downloading anything, catches that case and
                # just marks the sheet instead.
                existing = drive_upload.find_existing_upload(drive_service, folder_id, media_id)
                if existing is not None:
                    master_client.update_single_cell(media_id, "drive_uploaded_at", _now_iso(),
                                                     create_if_missing=True)
                    already_present += 1
                    uploaded += 1
                    print(f"{brand}: media_id={media_id!r} was already uploaded by an earlier "
                          f"run -- marking it instead of downloading and uploading it again.")
                    continue

                local_path = refunnel_export.download_approved_video(
                    page, media_id, download_dir, debug_dir=debug_dir,
                    username=username, created_at=row.get("created_at", ""),
                    reset_scroll=False,
                )
                # CONFIRMED REAL distinction this makes: None means the
                # card WAS found, but its real current status on
                # Refunnel's live page no longer matches what our
                # Master Data says -- Refunnel's own team confirmed
                # their Approved filter can include Pending review
                # posts. Not a failure, same as Pending review is
                # already treated for email scraping -- a fresh export
                # corrects this on its own.
                if local_path is None:
                    status_changed += 1
                    continue
                if not local_path:
                    print(f"{brand}: couldn't locate media_id={media_id!r} on the page -- "
                          f"leaving it unmarked, will retry on a future run.")
                    failed += 1
                    continue

                # CONFIRMED REAL: the filename can only be built now,
                # once local_path's REAL extension is known -- matching
                # the original, proven version of this function
                # (local_path.suffix.lstrip(".")), not a hardcoded
                # ".mp4" guess computed before the download even ran.
                filename = parse_refunnel.build_drive_filename(
                    brand, username, media_id, local_path.suffix.lstrip(".")
                )
                drive_upload.upload_file(drive_service, str(local_path), filename, folder_id)

                # Marked immediately, one at a time -- not batched at
                # the end -- so a crash partway through a long run
                # doesn't lose track of videos already safely in Drive.
                master_client.update_single_cell(media_id, "drive_uploaded_at", _now_iso(),
                                                 create_if_missing=True)
                uploaded += 1
                succeeded = True
            except Exception as e:
                print(f"{brand}: couldn't process media_id={media_id!r}: {type(e).__name__}: {e}")
                failed += 1
            finally:
                # Deleted once safely in Drive -- no reason to also
                # keep a local copy. Kept on failure, though: a video
                # that failed partway through is exactly what's worth
                # inspecting from the debug artifact.
                try:
                    if succeeded and local_path is not None and local_path.exists():
                        local_path.unlink()
                except Exception:
                    pass

        print(f"{brand}: uploaded {uploaded} ({already_present} already present from an earlier "
              f"run), failed {failed}, status changed since export {status_changed}, "
              f"{len(to_upload) - attempted} still pending for a future run.")
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

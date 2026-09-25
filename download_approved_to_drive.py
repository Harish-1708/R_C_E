"""
download_approved_to_drive.py

For every brand with a Drive folder configured, downloads any Approved
(GRANTED usage rights) video not yet in Drive directly, then uploads it
to that brand's Google Drive folder.

The process is incremental:
- Approved videos already marked in Drive are skipped.
- Successful uploads are immediately marked with drive_uploaded_at.
- Failed videos remain eligible for a future run.
- The queue continues past failed items.
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

BATCH_SIZE = int(
    os.environ.get("DRIVE_BACKFILL_BATCH_SIZE", "50")
)

MAX_ATTEMPTS_PER_RUN = int(
    os.environ.get(
        "DRIVE_BACKFILL_MAX_ATTEMPTS",
        str(BATCH_SIZE * 8),
    )
)

DRIVE_BACKFILL_TIME_BUDGET_MINUTES = float(
    os.environ.get(
        "DRIVE_BACKFILL_TIME_BUDGET_MINUTES",
        "45",
    )
)


def load_workspaces(path: str = CONFIG_PATH) -> list:
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)

    return data.get("workspaces", [])


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def process_one_brand(
    gc: "gspread.Client",
    brand_config: dict,
    drive_service,
    email: str,
    known_workspace_names: list,
) -> None:

    brand = brand_config["name"]

    print(
        f"DRIVE BACKFILL: processing brand={brand!r}",
        flush=True,
    )

    refunnel_workspace_name = brand_config.get(
        "refunnel_workspace_name",
        brand,
    )

    secret_name = brand_config["spreadsheet_id_secret"]

    spreadsheet_id = os.environ.get(secret_name)

    if not spreadsheet_id:
        print(
            f"{brand}: skipping -- {secret_name} isn't set.",
            flush=True,
        )
        return

    drive_secret_name = brand_config.get(
        "drive_folder_id_secret"
    )

    if not drive_secret_name:
        print(
            f"{brand}: skipping -- no drive_folder_id_secret "
            f"configured in config/workspaces.yaml.",
            flush=True,
        )
        return

    folder_id = os.environ.get(drive_secret_name)

    if not folder_id:
        print(
            f"{brand}: skipping -- {drive_secret_name} isn't set.",
            flush=True,
        )
        return

    print(
        f"{brand}: spreadsheet secret={secret_name!r}",
        flush=True,
    )

    print(
        f"{brand}: Drive folder secret={drive_secret_name!r} "
        f"is SET",
        flush=True,
    )

    sh = sheets_sync.retry_on_transient_error(
        gc.open_by_key,
        spreadsheet_id,
    )

    print(
        f"{brand}: Google Sheet opened successfully.",
        flush=True,
    )

    try:
        master_ws = sh.worksheet(MASTER_DATA_TAB)

    except gspread.exceptions.WorksheetNotFound:
        print(
            f"{brand}: skipping -- no "
            f"'{MASTER_DATA_TAB}' tab yet.",
            flush=True,
        )
        return

    master_client = sheets_sync.GspreadSheetsClient(
        master_ws
    )

    master_all = master_client.read_all()

    if not master_all:
        print(
            f"{brand}: skipping -- "
            f"'{MASTER_DATA_TAB}' tab is empty.",
            flush=True,
        )
        return

    master_header = master_all[0]
    master_data = master_all[1:]

    print(
        f"{brand}: Master Data contains "
        f"{len(master_data)} data row(s).",
        flush=True,
    )

    master_rows = sheets_sync.index_by_id(
        master_header,
        master_data,
        "id",
    )

    existing_uploads = sheets_sync.read_column_values(
        master_client,
        "drive_uploaded_at",
    )

    to_upload = parse_refunnel.rows_needing_drive_upload(
        master_rows,
        existing_uploads,
    )

    if not to_upload:
        print(
            f"{brand}: nothing new to upload -- every Approved "
            f"video is already in Drive.",
            flush=True,
        )
        return

    target_ids = list(to_upload.keys())

    print(
        f"{brand}: {len(to_upload)} Approved video(s) "
        f"not yet in Drive -- aiming for "
        f"{BATCH_SIZE} successful upload(s) this run "
        f"(trying up to "
        f"{min(MAX_ATTEMPTS_PER_RUN, len(target_ids))} "
        f"of them if needed).",
        flush=True,
    )

    download_dir = (
        f"{DOWNLOAD_DIR}/{brand.replace(' ', '_')}"
    )

    debug_dir = f"{download_dir}/debug"

    os.makedirs(
        download_dir,
        exist_ok=True,
    )

    print(
        f"{brand}: loading Refunnel session...",
        flush=True,
    )

    p, browser, context = (
        refunnel_auth.load_or_refresh_session(
            email=email
        )
    )

    print(
        f"{brand}: Refunnel session loaded.",
        flush=True,
    )

    try:

        page = context.new_page()

        print(
            f"{brand}: opening Approved Refunnel view...",
            flush=True,
        )

        refunnel_export.goto_social_listening_for_workspace(
            page,
            refunnel_workspace_name,
            known_workspace_names,
            usage_rights="GRANTED",
        )

        print(
            f"{brand}: Approved Refunnel view loaded.",
            flush=True,
        )

        refunnel_export.scroll_to_top(page)

        uploaded = 0
        already_present = 0
        failed = 0
        status_changed = 0
        attempted = 0

        deadline = (
            time.monotonic()
            + DRIVE_BACKFILL_TIME_BUDGET_MINUTES * 60
        )

        for media_id in target_ids:

            if uploaded >= BATCH_SIZE:
                break

            if attempted >= MAX_ATTEMPTS_PER_RUN:
                print(
                    f"{brand}: reached the "
                    f"{MAX_ATTEMPTS_PER_RUN}-attempt safety cap "
                    f"with {uploaded} confirmed -- stopping.",
                    flush=True,
                )
                break

            if time.monotonic() >= deadline:
                print(
                    f"{brand}: reached the "
                    f"{DRIVE_BACKFILL_TIME_BUDGET_MINUTES:.0f}-minute "
                    f"time budget with {uploaded} confirmed -- "
                    f"stopping.",
                    flush=True,
                )
                break

            attempted += 1

            print(
                f"{brand}: attempting "
                f"{attempted}/{min(MAX_ATTEMPTS_PER_RUN, len(target_ids))} "
                f"media_id={media_id!r}",
                flush=True,
            )

            row = master_rows[media_id]

            username = (
                row.get("username", "")
                or "unknown"
            )

            local_path = None
            succeeded = False

            try:

                print(
                    f"{brand}: checking whether "
                    f"media_id={media_id!r} already exists in Drive...",
                    flush=True,
                )

                existing = drive_upload.find_existing_upload(
                    drive_service,
                    folder_id,
                    media_id,
                )

                if existing is not None:

                    print(
                        f"{brand}: media_id={media_id!r} "
                        f"already exists in Drive as "
                        f"{existing.get('name')!r}.",
                        flush=True,
                    )

                    master_client.update_single_cell(
                        media_id,
                        "drive_uploaded_at",
                        _now_iso(),
                        create_if_missing=True,
                    )

                    already_present += 1
                    uploaded += 1

                    continue

                print(
                    f"{brand}: media_id={media_id!r} "
                    f"not found in Drive. Downloading from Refunnel...",
                    flush=True,
                )

                local_path = (
                    refunnel_export.download_approved_video(
                        page,
                        media_id,
                        download_dir,
                        debug_dir=debug_dir,
                        username=username,
                        created_at=row.get(
                            "created_at",
                            "",
                        ),
                        reset_scroll=False,
                    )
                )

                if local_path is None:

                    print(
                        f"{brand}: media_id={media_id!r} "
                        f"was found but its current status no "
                        f"longer matches Approved.",
                        flush=True,
                    )

                    status_changed += 1
                    continue

                if not local_path:

                    print(
                        f"{brand}: couldn't locate "
                        f"media_id={media_id!r} on the page -- "
                        f"leaving it unmarked.",
                        flush=True,
                    )

                    failed += 1
                    continue

                print(
                    f"{brand}: downloaded media_id="
                    f"{media_id!r} to {local_path}",
                    flush=True,
                )

                extension = local_path.suffix.lstrip(".")

                filename = parse_refunnel.build_drive_filename(
                    brand,
                    username,
                    media_id,
                    extension,
                )

                print(
                    f"{brand}: uploading "
                    f"{filename!r} to Drive...",
                    flush=True,
                )

                drive_file_id = drive_upload.upload_file(
                    drive_service,
                    str(local_path),
                    filename,
                    folder_id,
                )

                print(
                    f"{brand}: Drive upload succeeded. "
                    f"file_id={drive_file_id!r}",
                    flush=True,
                )

                master_client.update_single_cell(
                    media_id,
                    "drive_uploaded_at",
                    _now_iso(),
                    create_if_missing=True,
                )

                print(
                    f"{brand}: marked media_id="
                    f"{media_id!r} as uploaded in Master Data.",
                    flush=True,
                )

                uploaded += 1
                succeeded = True

            except Exception as e:

                print(
                    f"{brand}: couldn't process "
                    f"media_id={media_id!r}: "
                    f"{type(e).__name__}: {e}",
                    flush=True,
                )

                print(
                    traceback.format_exc(),
                    flush=True,
                )

                failed += 1

            finally:

                try:

                    if (
                        succeeded
                        and local_path is not None
                        and local_path.exists()
                    ):
                        local_path.unlink()

                        print(
                            f"{brand}: deleted local copy "
                            f"after successful Drive upload.",
                            flush=True,
                        )

                except Exception:
                    pass

        print(
            f"{brand}: uploaded {uploaded} "
            f"({already_present} already present from an earlier run), "
            f"failed {failed}, "
            f"status changed since export {status_changed}, "
            f"{len(to_upload) - attempted} still pending "
            f"for a future run.",
            flush=True,
        )

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

    sys.stdout.reconfigure(
        line_buffering=True
    )

    print(
        "==============================================",
        flush=True,
    )

    print(
        "DRIVE BACKFILL PYTHON SCRIPT STARTED",
        flush=True,
    )

    print(
        "==============================================",
        flush=True,
    )

    email = os.environ.get(
        "REFUNNEL_EMAIL"
    )

    service_account_path = os.environ.get(
        "GOOGLE_SERVICE_ACCOUNT_JSON"
    )

    print(
        f"DRIVE BACKFILL: "
        f"GOOGLE_SERVICE_ACCOUNT_JSON="
        f"{service_account_path!r}",
        flush=True,
    )

    print(
        "DRIVE BACKFILL: "
        f"REFUNNEL_EMAIL="
        f"{'SET' if email else 'NOT SET'}",
        flush=True,
    )

    print(
        "DRIVE BACKFILL: "
        f"DRIVE_FOLDER_ID_SWOVERALLS="
        f"{'SET' if os.environ.get('DRIVE_FOLDER_ID_SWOVERALLS') else 'NOT SET'}",
        flush=True,
    )

    print(
        f"DRIVE BACKFILL: BATCH_SIZE={BATCH_SIZE}",
        flush=True,
    )

    print(
        f"DRIVE BACKFILL: "
        f"MAX_ATTEMPTS_PER_RUN="
        f"{MAX_ATTEMPTS_PER_RUN}",
        flush=True,
    )

    if not email or not service_account_path:

        print(
            "Missing required env vars: "
            "need REFUNNEL_EMAIL, "
            "GOOGLE_SERVICE_ACCOUNT_JSON.",
            file=sys.stderr,
            flush=True,
        )

        return 1

    try:

        print(
            "DRIVE BACKFILL: creating Google Sheets client...",
            flush=True,
        )

        gc = gspread.service_account(
            filename=service_account_path
        )

        print(
            "DRIVE BACKFILL: Google Sheets authentication succeeded.",
            flush=True,
        )

        print(
            "DRIVE BACKFILL: creating Google Drive service...",
            flush=True,
        )

        drive_service = (
            drive_upload.build_drive_service(
                service_account_path
            )
        )

        print(
            "DRIVE BACKFILL: Google Drive service created.",
            flush=True,
        )

        print(
            f"DRIVE BACKFILL: loading workspaces "
            f"from {CONFIG_PATH!r}...",
            flush=True,
        )

        workspaces = load_workspaces()

        print(
            f"DRIVE BACKFILL: loaded "
            f"{len(workspaces)} workspace(s): "
            f"{[w.get('name') for w in workspaces]}",
            flush=True,
        )

        known_workspace_names = [
            w.get(
                "refunnel_workspace_name",
                w["name"],
            )
            for w in workspaces
        ]

        for brand_config in workspaces:

            print(
                "DRIVE BACKFILL: --------------------------------",
                flush=True,
            )

            print(
                f"DRIVE BACKFILL: starting workspace "
                f"{brand_config.get('name')!r}",
                flush=True,
            )

            process_one_brand(
                gc,
                brand_config,
                drive_service,
                email,
                known_workspace_names,
            )

        print(
            "==============================================",
            flush=True,
        )

        print(
            "DRIVE BACKFILL PYTHON SCRIPT FINISHED "
            "SUCCESSFULLY",
            flush=True,
        )

        print(
            "==============================================",
            flush=True,
        )

        return 0

    except Exception as e:

        print(
            "==============================================",
            flush=True,
        )

        print(
            f"FAILURE: {type(e).__name__}: {e}",
            file=sys.stderr,
            flush=True,
        )

        print(
            traceback.format_exc(),
            file=sys.stderr,
            flush=True,
        )

        print(
            "==============================================",
            flush=True,
        )

        return 1


if __name__ == "__main__":
    sys.exit(main())

"""
run_daily_sync.py

The script GitHub Actions actually runs on a schedule. Ties together:

    refunnel_auth   -> get a logged-in page (saved session, or fresh
                       login via Gmail-OTP fallback if it expired)
    refunnel_export -> scroll-to-load-all, download both CSVs, (optionally)
                       scrape creator emails for usage-rights rows
    parse_refunnel  -> turn those CSVs into the 5 tabs' target row-sets
    sheets_sync     -> push each tab to Google Sheets, preserving any
                       manual columns already there

Required environment variables (set as GitHub Actions secrets):
    REFUNNEL_EMAIL            -- your Refunnel login email
    GOOGLE_SERVICE_ACCOUNT_JSON -- path to (or inline JSON of) a service
                                   account with edit access to the sheet
    SPREADSHEET_ID            -- target Google Sheet's id
    GMAIL_ADDRESS / GMAIL_APP_PASSWORD
                              -- only needed for the Gmail-OTP fallback
                                 path (see gmail_otp.py)

Optional:
    SLACK_WEBHOOK_URL         -- if set, posts a message on hard failure
    REFUNNEL_SESSION_FILE     -- defaults to refunnel_session.json

On any hard failure this script exits non-zero, so the GitHub Actions
run itself shows as failed (visible in the Actions tab / failure emails)
even without Slack configured. Nothing here has been run end-to-end
against live Refunnel/Google/Gmail -- see README "Testing the full
pipeline" for the manual verification checklist before trusting the
schedule unattended.
"""

from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path

import gspread

import parse_refunnel
import refunnel_auth
import refunnel_export
import sheets_sync

DOWNLOAD_DIR = "downloads"


def notify_failure(message: str) -> None:
    print(f"FAILURE: {message}", file=sys.stderr)
    webhook = os.environ.get("SLACK_WEBHOOK_URL")
    if not webhook:
        return
    try:
        import requests

        requests.post(webhook, json={"text": f":x: Refunnel sync failed: {message}"}, timeout=10)
    except Exception as e:
        print(f"(also failed to post Slack notification: {e})", file=sys.stderr)


def main() -> int:
    email = os.environ.get("REFUNNEL_EMAIL")
    spreadsheet_id = os.environ.get("SPREADSHEET_ID")
    service_account_path = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    workspace_name = os.environ.get("WORKSPACE_NAME", "default")
    refunnel_workspace_name = os.environ.get("REFUNNEL_WORKSPACE_NAME", workspace_name)
    known_workspace_names = [
        n.strip() for n in os.environ.get("KNOWN_WORKSPACE_NAMES", refunnel_workspace_name).split(",") if n.strip()
    ]
    session_file = os.environ.get("REFUNNEL_SESSION_FILE", refunnel_auth.DEFAULT_SESSION_FILE)
    download_dir = f"{DOWNLOAD_DIR}/{workspace_name}"

    if not email or not spreadsheet_id or not service_account_path:
        notify_failure(
            "Missing required env vars: need REFUNNEL_EMAIL, SPREADSHEET_ID, "
            "GOOGLE_SERVICE_ACCOUNT_JSON."
        )
        return 1

    p = browser = context = None
    try:
        print(f"=== Syncing workspace: {workspace_name} (Refunnel workspace: {refunnel_workspace_name}) ===")
        # --- 1. auth ---
        p, browser, context = refunnel_auth.load_or_refresh_session(
            email=email, session_file=session_file, headless=True
        )
        page = context.new_page()

        # --- 2. select the right workspace, then export ---
        page.goto(refunnel_auth.REFUNNEL_LOGIN_URL.replace("/login", "/social-listening"))
        refunnel_export.select_workspace(page, refunnel_workspace_name, known_workspace_names)
        refunnel_export.scroll_to_load_all(page)
        media_csv_path = refunnel_export.export_media_csv(page, download_dir)

        page.goto(refunnel_auth.REFUNNEL_LOGIN_URL.replace("/login", "/payments"))
        payments_csv_path = refunnel_export.export_payments_csv(page, download_dir)

        # --- 3. parse ---
        result = parse_refunnel.parse_media_csv(media_csv_path)
        result = parse_refunnel.parse_payments_csv(payments_csv_path, result=result)

        if refunnel_export.SCRAPE_EMAILS_ENABLED:
            target_ids = parse_refunnel.rows_needing_email_scrape(result)
            emails = refunnel_export.scrape_creator_emails(page, target_ids)
            updated = parse_refunnel.apply_creator_emails(result, emails)
            print(f"Scraped {updated} creator email(s) for usage-rights rows.")

        print(
            f"Parsed: {len(result.master)} media rows "
            f"({len(result.rights_approved)} approved, "
            f"{len(result.rights_requested)} requested, "
            f"{len(result.rights_declined)} declined), "
            f"{len(result.payments)} payment rows."
        )

        # --- 4. push to sheets ---
        gc = gspread.service_account(filename=service_account_path)
        sh = gc.open_by_key(spreadsheet_id)

        tab_plan = [
            ("Master Data", parse_refunnel.MASTER_COLUMNS, result.master),
            ("Usage Rights - Approved", parse_refunnel.MASTER_COLUMNS, result.rights_approved),
            ("Usage Rights - Requested", parse_refunnel.MASTER_COLUMNS, result.rights_requested),
            ("Usage Rights - Declined", parse_refunnel.MASTER_COLUMNS, result.rights_declined),
            ("Payments", parse_refunnel.PAYMENT_COLUMNS, result.payments),
        ]

        for title, columns, rows in tab_plan:
            ws = sheets_sync.get_or_create_worksheet(sh, title)
            client = sheets_sync.GspreadSheetsClient(ws)
            summary = sheets_sync.sync_tab(client, columns, rows)
            print(f"{title}: wrote {summary['rows_written']} rows "
                  f"(preserved columns: {summary['extra_columns_preserved']})")

        return 0

    except Exception as e:
        notify_failure(f"{type(e).__name__}: {e}\n{traceback.format_exc()}")
        return 1

    finally:
        if context:
            context.close()
        if browser:
            browser.close()
        if p:
            p.stop()


if __name__ == "__main__":
    Path(DOWNLOAD_DIR).mkdir(exist_ok=True)
    sys.exit(main())

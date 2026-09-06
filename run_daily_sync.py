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


def _save_debug_snapshot(page, workspace_name: str) -> None:
    """On failure, capture what the browser actually saw -- a screenshot
    and the raw HTML -- so we can tell "wrong page", "Cloudflare/bot
    challenge", "still on login", etc. apart without guessing blind.
    Never lets a screenshot failure hide the real error."""
    if page is None:
        print("No page object available at failure time -- nothing to snapshot "
              "(failure happened before or during login).", file=sys.stderr)
        return

    debug_dir = Path(DOWNLOAD_DIR) / workspace_name / "debug"
    debug_dir.mkdir(parents=True, exist_ok=True)

    try:
        print(f"Page URL at failure: {page.url}", file=sys.stderr)
    except Exception as e:
        print(f"Couldn't read page.url: {e}", file=sys.stderr)

    try:
        screenshot_path = debug_dir / "failure_screenshot.png"
        page.screenshot(path=str(screenshot_path), full_page=True)
        print(f"Saved failure screenshot to {screenshot_path}", file=sys.stderr)
    except Exception as e:
        print(f"Couldn't save failure screenshot: {e}", file=sys.stderr)

    try:
        html_path = debug_dir / "failure_page.html"
        html_path.write_text(page.content(), encoding="utf-8")
        print(f"Saved failure page HTML to {html_path}", file=sys.stderr)
    except Exception as e:
        print(f"Couldn't save failure page HTML: {e}", file=sys.stderr)


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

    p = browser = context = page = None
    try:
        print(f"=== Syncing workspace: {workspace_name} (Refunnel workspace: {refunnel_workspace_name}) ===")
        # --- 1. auth ---
        p, browser, context = refunnel_auth.load_or_refresh_session(
            email=email, session_file=session_file, headless=True
        )
        page = context.new_page()

        # --- 2. select the right workspace, then export media ---
        page.goto(refunnel_auth.REFUNNEL_SOCIAL_LISTENING_URL)
        refunnel_export.select_workspace(page, refunnel_workspace_name, known_workspace_names)
        refunnel_export.scroll_to_load_all(page)
        media_csv_path = refunnel_export.export_media_csv(page, download_dir)

        # --- 3. parse media, connect to sheets, and scrape emails --
        # ALL of this must happen while `page` is STILL on the Social
        # Listening grid, since scrape_creator_emails() searches for
        # post cards there. A real run confirmed this the hard way: the
        # scraper was previously called after navigating to the
        # Payments page, so every single scroll-search failed -- there
        # were no post cards to find on that page at all. Payments
        # export now happens AFTER this block, not before it.
        result = parse_refunnel.parse_media_csv(media_csv_path)

        duplicate_links = parse_refunnel.find_duplicate_post_links(result)
        if duplicate_links:
            print(f"WARNING: {len(duplicate_links)} original_post_link value(s) are shared "
                  f"across multiple different ids -- this usually means genuine duplicate "
                  f"content under two ids. Not removed automatically. Examples: "
                  f"{dict(list(duplicate_links.items())[:5])}")

        gc = gspread.service_account(filename=service_account_path)
        sh = gc.open_by_key(spreadsheet_id)
        master_ws = sheets_sync.get_or_create_worksheet(sh, "Master Data")
        master_client = sheets_sync.GspreadSheetsClient(master_ws)

        # Don't re-scrape an email we already found on a previous run --
        # load whatever's already in the sheet's creator_email column
        # first, so rows_needing_email_scrape() only returns genuinely
        # still-missing ones.
        existing_emails = sheets_sync.read_column_values(master_client, "creator_email")
        preloaded = parse_refunnel.apply_creator_emails(result, existing_emails)
        if preloaded:
            print(f"Loaded {preloaded} previously-found creator email(s) from the sheet -- won't re-scrape those.")

        if refunnel_export.SCRAPE_EMAILS_ENABLED:
            # Write Master Data NOW, before scraping starts, so there
            # are actual rows in the sheet for update_single_cell() to
            # find. Each email found during scraping is then saved to
            # the sheet immediately (one targeted cell update, not a
            # full-tab rewrite) -- so if the run gets interrupted
            # partway through scraping, emails already found aren't
            # lost. The final full sync pass at the end (all 6 tabs,
            # including Master Data again) reconciles everything
            # regardless, so this is a safety net, not the only write.
            print("Writing Master Data once before scraping starts, so progress can be saved incrementally...")
            sheets_sync.sync_tab(master_client, parse_refunnel.MASTER_COLUMNS, result.master, never_delete=True)

            def _save_email_incrementally(media_id: str, email: str) -> None:
                found = master_client.update_single_cell(media_id, "creator_email", email)
                if not found:
                    print(f"(incremental save: media_id={media_id!r} not found in Master Data yet -- "
                          f"will still be saved in the final full sync at the end)")

            target_ids = parse_refunnel.rows_needing_email_scrape(result)
            try:
                emails = refunnel_export.scrape_creator_emails(
                    page, result.master, target_ids,
                    debug_dir=f"{download_dir}/debug",
                    on_email_found=_save_email_incrementally,
                )
                updated = parse_refunnel.apply_creator_emails(result, emails)
                print(f"Scraped {updated} new creator email(s) for usage-rights rows.")
            except Exception as e:
                # Don't let a scraping crash (e.g. the browser itself
                # crashing) take down the rest of the run -- whatever
                # was found before the crash is already saved to Master
                # Data via the incremental callback above. Continue on
                # to the re-sync step below and the rest of the
                # pipeline (payments, other tabs) rather than aborting.
                print(f"WARNING: email scraping did not finish cleanly ({type(e).__name__}: {e}). "
                      f"Continuing with whatever was incrementally saved to Master Data so far.")

            # Master Data is the only tab any scraping/email logic ever
            # touches directly. Every other tab's creator_email just
            # gets copied from whatever's ACTUALLY in Master Data's
            # sheet right now -- re-read here rather than trusting the
            # in-memory `result` to have survived the scraping step
            # uninterrupted. This makes the other tabs correct even if
            # scraping above crashed partway through: Usage Rights /
            # Human Review will reflect real, saved progress, not stale
            # data from before this run started.
            refreshed_emails = sheets_sync.read_column_values(master_client, "creator_email")
            resynced = parse_refunnel.apply_creator_emails(result, refreshed_emails)
            print(f"Re-synced {resynced} creator email(s) from Master Data's current sheet state.")

            # Confirm the browser actually survived scraping before
            # trying to keep using it -- confirmed from a real run: it
            # crashed mid-scrape, the circuit breaker correctly stopped
            # scraping and returned cleanly, but the NEXT action
            # (navigating to Payments) then failed too, because it
            # tried to reuse the same already-dead page. If it's dead,
            # get a fresh logged-in session (reusing the saved cookies,
            # no full re-login needed) rather than limping forward with
            # a browser that's already gone.
            try:
                page.evaluate("() => 1")
            except Exception:
                print("Browser appears to have crashed during scraping -- "
                      "recovering with a fresh session before continuing.")
                for cleanup in (context.close, browser.close, p.stop):
                    try:
                        cleanup()
                    except Exception:
                        pass
                p, browser, context = refunnel_auth.load_or_refresh_session(
                    email=email, session_file=session_file, headless=True
                )
                page = context.new_page()
                page.goto(refunnel_auth.REFUNNEL_SOCIAL_LISTENING_URL)
                refunnel_export.select_workspace(page, refunnel_workspace_name, known_workspace_names)

        # --- 4. NOW it's safe to navigate away and export payments ---
        page.goto(refunnel_auth.REFUNNEL_PAYMENTS_URL)
        payments_csv_path = refunnel_export.export_payments_csv(page, download_dir)
        result = parse_refunnel.parse_payments_csv(payments_csv_path, result=result)

        # Move anything you've marked "Reviewed" (a manual column you
        # add to Master Data yourself) out of Approved/Requested/Declined
        # and into Human Review instead.
        reviewed_values = sheets_sync.read_column_values(master_client, "Reviewed")
        reviewed_ids = {mid for mid, val in reviewed_values.items() if val.strip().lower() in ("yes", "y", "true", "1")}
        moved = parse_refunnel.apply_human_review_flags(result, reviewed_ids)
        if moved:
            print(f"Moved {moved} reviewed row(s) into Human Review.")

        print(
            f"Parsed: {len(result.master)} media rows "
            f"({len(result.rights_approved)} approved, "
            f"{len(result.rights_requested)} requested, "
            f"{len(result.rights_declined)} declined, "
            f"{len(result.rights_reviewed)} reviewed), "
            f"{len(result.payments)} payment rows."
        )

        # --- 5. push to sheets ---
        human_review_rows = parse_refunnel.build_human_review_rows(result)

        # max_shrink_fraction is only set for tabs that should only ever
        # grow or hold steady (Master Data, Payments) -- Usage Rights
        # tabs and Human Review are expected to shrink by design (rows
        # move out when marked Reviewed, or between status tabs), so a
        # shrink there is normal, not a sign of an incomplete pull.
        # never_delete=True for Master Data and Payments: an id already
        # in the sheet is carried forward even if it's missing from
        # this run's pull, so a bad export can never quietly delete
        # real rows -- only add new ones or update existing ones.
        # Usage Rights tabs and Human Review are NOT protected this way
        # on purpose -- rows moving out of them (a status changing, a
        # post marked Reviewed) is correct, intended behavior, not data
        # loss.
        tab_plan = [
            ("Master Data", parse_refunnel.MASTER_COLUMNS, result.master, True),
            ("Usage Rights - Approved", parse_refunnel.MASTER_COLUMNS, result.rights_approved, False),
            ("Usage Rights - Requested", parse_refunnel.MASTER_COLUMNS, result.rights_requested, False),
            ("Usage Rights - Declined", parse_refunnel.MASTER_COLUMNS, result.rights_declined, False),
            ("Human Review", parse_refunnel.HUMAN_REVIEW_COLUMNS, human_review_rows, False),
            ("Payments", parse_refunnel.PAYMENT_COLUMNS, result.payments, True),
        ]

        for title, columns, rows, never_delete in tab_plan:
            ws = sheets_sync.get_or_create_worksheet(sh, title)
            client = sheets_sync.GspreadSheetsClient(ws)
            summary = sheets_sync.sync_tab(client, columns, rows, never_delete=never_delete)
            print(f"{title}: wrote {summary['rows_written']} rows "
                  f"(preserved columns: {summary['extra_columns_preserved']}, "
                  f"carried forward: {summary['rows_carried_forward']})")

        return 0

    except Exception as e:
        notify_failure(f"{type(e).__name__}: {e}\n{traceback.format_exc()}")
        _save_debug_snapshot(page, workspace_name)
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

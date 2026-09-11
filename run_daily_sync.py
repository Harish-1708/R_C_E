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
    # Belt-and-suspenders alongside `python -u` in the workflow YML --
    # confirmed real problem: Python fully buffers stdout when it isn't
    # connected to a real terminal (exactly the case in GitHub Actions),
    # so every print() in this whole script -- including the new
    # scraping progress lines -- was silently sitting in a buffer for
    # 20+ minutes with nothing visible in the live log, not because
    # anything was stuck, but because nothing had flushed yet. This
    # keeps working even if this script is ever invoked a different way
    # (without -u) in the future.
    sys.stdout.reconfigure(line_buffering=True)

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
        page.goto(refunnel_auth.refunnel_social_listening_url())
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
        sh = sheets_sync.retry_on_transient_error(gc.open_by_key, spreadsheet_id)
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

        # An email belongs to the creator, not the individual post --
        # propagate any known email to every other post by that same
        # username before deciding what still needs scraping. Confirmed
        # real opportunity: 342 of 1360 unique usernames in a real
        # export appear on 2+ posts.
        propagated = parse_refunnel.propagate_emails_by_username(result)
        if propagated:
            print(f"Propagated {propagated} creator email(s) to other posts by the same username.")

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

            # A crashed browser no longer just gets one recovery to
            # limp to Payments -- confirmed from real runs that a
            # single scheduled run can hit the browser-crash circuit
            # breaker repeatedly, each time only getting through a
            # fraction of what's left, requiring you to keep manually
            # re-triggering. Now it automatically recovers AND resumes
            # scraping the remaining ids, up to a bounded number of
            # restarts, so one scheduled run gets much further on its
            # own. Each loop also re-checks target_ids fresh (shrunk by
            # both newly-scraped AND newly-propagated emails), and
            # propagates after every attempt, not just once at the top.
            #
            # already_confirmed_empty accumulates across every restart
            # in THIS run -- confirmed real, serious bug: without this,
            # a crash-triggered restart re-scanned the ENTIRE target
            # list from scratch, including hundreds of ids already
            # confirmed to have no email earlier in the SAME run, pure
            # wasted work (a real run showed exactly this: 875 ids
            # re-checked identically after one restart). Ids that hit a
            # genuine exception are deliberately NOT added here -- their
            # true status is still unknown, so they get another chance
            # on the next attempt, unlike confirmed-empty ones.
            # max_scrape_restarts raised again, 8 -> 40 -- confirmed
            # real, explicit, repeated instruction: this must reach the
            # end of the full backlog every time, not give up after a
            # limited number of crash-cycles. Combined with removing
            # the in-loop circuit breakers above and the re-scroll
            # give-up below, restarts are now the ONLY thing standing
            # between a crash and completion, so the budget needs to be
            # generous enough to absorb realistically many crashes
            # across a ~2771-post backlog. Still a bounded number, not
            # literally infinite, as a last-resort guard against a
            # truly stuck loop (e.g. Refunnel itself being down) never
            # actually finishing.
            already_confirmed_empty: set = set()
            max_scrape_restarts = 40
            for attempt in range(max_scrape_restarts + 1):
                target_ids = [
                    mid for mid in parse_refunnel.rows_needing_email_scrape(result)
                    if mid not in already_confirmed_empty
                ]
                if not target_ids:
                    print("No posts left needing an email -- scraping is done for this run.")
                    break

                try:
                    emails, empty_ids = refunnel_export.scrape_creator_emails(
                        page, result.master, target_ids,
                        debug_dir=f"{download_dir}/debug",
                        on_email_found=_save_email_incrementally,
                    )
                    already_confirmed_empty |= empty_ids
                    updated = parse_refunnel.apply_creator_emails(result, emails)
                    print(f"Scraped {updated} new creator email(s), confirmed "
                          f"{len(empty_ids)} with no email on file (attempt {attempt + 1}).")
                except Exception as e:
                    # Don't let a scraping crash take down the rest of
                    # the run -- whatever was found before the crash is
                    # already saved to Master Data via the incremental
                    # callback above.
                    print(f"WARNING: email scraping did not finish cleanly ({type(e).__name__}: {e}).")

                propagated = parse_refunnel.propagate_emails_by_username(result)
                if propagated:
                    print(f"Propagated {propagated} creator email(s) to other posts by the same username.")

                # Master Data is the only tab any scraping/email logic
                # ever touches directly. Every other tab's creator_email
                # just gets copied from whatever's ACTUALLY in Master
                # Data's sheet right now -- re-read here rather than
                # trusting the in-memory `result` to have survived the
                # scraping step uninterrupted.
                refreshed_emails = sheets_sync.read_column_values(master_client, "creator_email")
                resynced = parse_refunnel.apply_creator_emails(result, refreshed_emails)
                print(f"Re-synced {resynced} creator email(s) from Master Data's current sheet state.")

                try:
                    page.evaluate("() => 1")
                    page_survived = True
                except Exception:
                    page_survived = False

                if page_survived:
                    # Scraping stopped for a reason other than a crash
                    # (e.g. the circuit breaker tripped on a genuine,
                    # non-crash problem) -- retrying won't help there,
                    # so don't burn a restart on it.
                    break

                if attempt >= max_scrape_restarts:
                    print(f"Browser crashed and the {max_scrape_restarts}-restart budget for "
                          f"this run is used up -- moving on with what was found. The rest "
                          f"will be picked up on a future run.")

                else:
                    print(f"Browser crashed during scraping -- recovering and resuming "
                          f"(restart {attempt + 1} of {max_scrape_restarts})...")

                # Either way (final giveup or about to retry), we need a
                # healthy page again -- for the next scrape attempt, or
                # for Payments right after this loop.
                for cleanup in (context.close, browser.close, p.stop):
                    try:
                        cleanup()
                    except Exception:
                        pass
                p, browser, context = refunnel_auth.load_or_refresh_session(
                    email=email, session_file=session_file, headless=True
                )
                page = context.new_page()
                page.goto(refunnel_auth.refunnel_social_listening_url())
                refunnel_export.select_workspace(page, refunnel_workspace_name, known_workspace_names)

                if attempt >= max_scrape_restarts:
                    break

                # Confirmed real bug: recovery re-selected the workspace
                # but never re-scrolled the grid back down, so a fresh
                # page reload after a crash always starts back at only
                # the first ~20-120 loaded items. Every subsequent
                # per-item scrape then had to rely on its own slow,
                # incremental scrolling to reach anything further down
                # a now much-longer 2771-item list -- easily explaining
                # an hour of real elapsed time with almost no email
                # count growth.
                #
                # If the re-scroll itself fails, this NO LONGER gives up
                # on the rest of the run -- confirmed real, explicit,
                # repeated instruction: nothing should end scraping
                # early except genuinely exhausting max_scrape_restarts.
                # Continuing anyway means the next scrape attempt starts
                # from whatever's currently loaded and falls back on its
                # own per-item incremental scrolling (slower for
                # far-down items, but it keeps trying rather than
                # quitting) -- and the loop will attempt another full
                # recovery (fresh session + re-scroll) again next time
                # it detects a dead page regardless.
                try:
                    refunnel_export.scroll_to_load_all(page)
                except Exception as e:
                    print(f"WARNING: re-scroll after recovery didn't finish cleanly "
                          f"({type(e).__name__}: {e}). Continuing anyway with whatever's "
                          f"currently loaded -- NOT giving up on the rest of this run.")

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
        # Read the Human Review tab's CURRENT moved_to_human_review_at
        # values before rebuilding it -- confirmed real want: knowing
        # WHEN something was pushed here, to filter by it, which a full
        # rewrite would otherwise silently reset to "now" every run.
        human_review_ws = sheets_sync.get_or_create_worksheet(sh, "Human Review")
        human_review_client = sheets_sync.GspreadSheetsClient(human_review_ws)
        existing_moved_dates = sheets_sync.read_column_values(human_review_client, "moved_to_human_review_at")
        human_review_rows = parse_refunnel.build_human_review_rows(result, existing_moved_dates)

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
        # sort_key/sort_reverse puts newest content at the top -- safe
        # here because created_at/updated_at are ISO-format timestamps,
        # which sort correctly as plain strings. Payments' date field
        # ("Sep 4, 2026") does NOT sort correctly that way, so it's left
        # on the default id-based sort rather than risk a wrong order.
        tab_plan = [
            ("Master Data", parse_refunnel.MASTER_COLUMNS, result.master, True, "created_at"),
            ("Usage Rights - Approved", parse_refunnel.MASTER_COLUMNS, result.rights_approved, False, "created_at"),
            ("Usage Rights - Requested", parse_refunnel.MASTER_COLUMNS, result.rights_requested, False, "created_at"),
            ("Usage Rights - Declined", parse_refunnel.MASTER_COLUMNS, result.rights_declined, False, "created_at"),
            ("Human Review", parse_refunnel.HUMAN_REVIEW_COLUMNS, human_review_rows, False, "updated_at"),
            ("Payments", parse_refunnel.PAYMENT_COLUMNS, result.payments, True, None),
        ]

        for title, columns, rows, never_delete, sort_key in tab_plan:
            ws = sheets_sync.get_or_create_worksheet(sh, title)
            client = sheets_sync.GspreadSheetsClient(ws)
            summary = sheets_sync.sync_tab(
                client, columns, rows, never_delete=never_delete,
                sort_key=sort_key, sort_reverse=(sort_key is not None),
            )
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

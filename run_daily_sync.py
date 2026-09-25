"""
run_daily_sync.py

Split into two independently runnable phases, so the Actions UI and
the log itself show, unambiguously, whether the export succeeded on
its own -- separate from whether email scraping afterward ran into
trouble. Confirmed real want: a combined "Phases 1+3" step made it
genuinely hard to tell, from the log alone, whether today's export
picked up new content at all before scraping's own output took over.

    Phase 1 (main_phase1): log in, export all media from Refunnel, and
        write Master Data. Fast, and the part that answers "did
        today's sync actually pick up new content."

    Phase 3 (main_phase3): scrape creator emails, export Payments, and
        do the full six-tab sync (Master Data again, Usage Rights x3,
        Human Review, Payments). The slow part -- can take hours on a
        large backlog.

Both phases run as SEPARATE GitHub Actions steps (separate processes),
so neither an open browser page nor an in-memory `result` object
carries over between them. Each phase does its own independent
login/navigate/scroll/export/parse -- see _setup_and_parse_media's own
docstring for the full reasoning. Set PIPELINE_PHASE=1 or
PIPELINE_PHASE=3 to run just that phase; leave it unset to run both in
one process, one after another -- this script's original, single-step
behavior, kept for anyone still invoking it directly.

Ties together:

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
    PIPELINE_PHASE            -- "1", "3", or unset (runs both)

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
import time
import traceback
from pathlib import Path
from typing import Optional

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


def _load_config() -> Optional[dict]:
    """Reads and validates every env var either phase needs. Returns
    None (after calling notify_failure itself) if something required
    is missing, so main_phase1/main_phase3 share one validation path
    instead of two copies that could drift apart."""
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
        return None

    return {
        "email": email,
        "spreadsheet_id": spreadsheet_id,
        "service_account_path": service_account_path,
        "workspace_name": workspace_name,
        "refunnel_workspace_name": refunnel_workspace_name,
        "known_workspace_names": known_workspace_names,
        "session_file": session_file,
        "download_dir": download_dir,
    }


def _setup_and_parse_media(config: dict, state: dict):
    """Shared by both phases: log in, select the workspace, scroll to
    load every post, export+parse the media CSV, then load and
    propagate any creator emails already known from a previous run.
    Returns (page, result, gc, sh, master_client, master_ws).

    CONFIRMED REAL design point: both phases call this independently,
    each doing its own fresh login/navigate/scroll/export -- not one
    call whose result is shared between them. GitHub Actions steps are
    separate process invocations; neither an open Playwright page nor
    an in-memory `result` object survives from one step to the next.
    Phase 3 re-exporting the CSV costs a little time, but it also means
    Phase 3 always works from the freshest possible export rather than
    Phase 1's, which may be stale by the time a long scraping run
    finally gets to it.

    state is a plain dict this writes into (state["p"], state["browser"],
    state["context"], state["page"]) as soon as each resource exists --
    so if this raises partway through, the caller's own finally block
    can still find and close whatever was actually created, exactly
    like the original single-phase main() could when everything lived
    in one function's local variables.
    """
    email = config["email"]
    session_file = config["session_file"]
    refunnel_workspace_name = config["refunnel_workspace_name"]
    known_workspace_names = config["known_workspace_names"]
    download_dir = config["download_dir"]

    # --- 1. auth ---
    p, browser, context = refunnel_auth.load_or_refresh_session(
        email=email, session_file=session_file, headless=True
    )
    state["p"], state["browser"], state["context"] = p, browser, context
    page = context.new_page()
    state["page"] = page

    # --- 2. select the right workspace, then export media ---
    refunnel_export.goto_social_listening_for_workspace(page, refunnel_workspace_name, known_workspace_names)
    refunnel_export.scroll_to_load_all(page)
    media_csv_path = refunnel_export.export_media_csv(page, download_dir)

    # --- 3. parse media, connect to sheets ---
    # ALL of this must happen while `page` is STILL on the Social
    # Listening grid, since scrape_creator_emails() (Phase 3 only)
    # searches for post cards there. A real run confirmed this the
    # hard way: the scraper was previously called after navigating to
    # the Payments page, so every single scroll-search failed -- there
    # were no post cards to find on that page at all. Payments export
    # happens after Phase 3's scraping loop, never before it.
    result = parse_refunnel.parse_media_csv(media_csv_path)

    duplicate_links = parse_refunnel.find_duplicate_post_links(result)
    if duplicate_links:
        print(f"WARNING: {len(duplicate_links)} original_post_link value(s) are shared "
              f"across multiple different ids -- this usually means genuine duplicate "
              f"content under two ids. Not removed automatically. Examples: "
              f"{dict(list(duplicate_links.items())[:5])}")

    gc = gspread.service_account(filename=config["service_account_path"])
    sh = sheets_sync.retry_on_transient_error(gc.open_by_key, config["spreadsheet_id"])
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

    return page, result, gc, sh, master_client, master_ws


def _close_browser(state: dict) -> None:
    if state.get("context"):
        try:
            state["context"].close()
        except Exception:
            pass
    if state.get("browser"):
        try:
            state["browser"].close()
        except Exception:
            pass
    if state.get("p"):
        try:
            state["p"].stop()
        except Exception:
            pass


def main_phase1() -> int:
    """Phase 1: export all media from Refunnel and write Master Data.
    Does NOT scrape creator emails, export Payments, or touch Usage
    Rights / Human Review -- that's all Phase 3. Kept as its own,
    separate GitHub Actions step (rather than silently bundled into
    Phase 3, which is how this used to work) specifically so the
    Actions UI and this step's own log show, unambiguously and on
    their own, whether the export succeeded and how many rows it saw
    -- independent of whether email scraping afterward runs into
    trouble.
    """
    sys.stdout.reconfigure(line_buffering=True)

    config = _load_config()
    if config is None:
        return 1

    state: dict = {}
    try:
        print(f"=== PHASE 1: Exporting media -- {config['workspace_name']} "
              f"(Refunnel workspace: {config['refunnel_workspace_name']}) ===")

        page, result, gc, sh, master_client, master_ws = _setup_and_parse_media(config, state)

        # Write Master Data NOW -- this IS Phase 1's actual output.
        # sort_key/sort_reverse puts newest content at the top, matching
        # the final full sync pass Phase 3 does later and the actual
        # Refunnel page's own newest-to-oldest order. never_delete=True:
        # an id already in the sheet is carried forward even if it's
        # missing from this run's pull, so a bad export can never
        # quietly delete real rows -- only add new ones or update
        # existing ones.
        existing_ids = set(sheets_sync.read_column_values(master_client, "id"))
        summary = sheets_sync.sync_tab(
            master_client, parse_refunnel.MASTER_COLUMNS, result.master, never_delete=True,
            preserve_columns=parse_refunnel.SHEET_OWNED_COLUMNS,
            sort_key="created_at", sort_reverse=True,
        )
        new_ids = set(result.master.keys()) - existing_ids

        print(
            f"=== PHASE 1 COMPLETE: {len(result.master)} media row(s) exported "
            f"({len(new_ids)} new since the last run, {summary['rows_carried_forward']} "
            f"carried forward unchanged). ==="
        )
        return 0

    except Exception as e:
        notify_failure(f"{type(e).__name__}: {e}\n{traceback.format_exc()}")
        _save_debug_snapshot(state.get("page"), config["workspace_name"])
        return 1

    finally:
        _close_browser(state)


def main_phase3() -> int:
    """Phase 3: scrape creator emails, export Payments, and do the full
    six-tab sync (Master Data again, Usage Rights x3, Human Review,
    Payments). Runs its own independent login/export/parse first --
    see _setup_and_parse_media's docstring for why -- so it's never
    dependent on Phase 1 having just run in the same process.
    """
    sys.stdout.reconfigure(line_buffering=True)

    config = _load_config()
    if config is None:
        return 1

    email = config["email"]
    session_file = config["session_file"]
    refunnel_workspace_name = config["refunnel_workspace_name"]
    known_workspace_names = config["known_workspace_names"]
    download_dir = config["download_dir"]
    workspace_name = config["workspace_name"]

    state: dict = {}
    try:
        print(f"=== PHASE 3: Scraping creator emails -- {workspace_name} "
              f"(Refunnel workspace: {refunnel_workspace_name}) ===")

        page, result, gc, sh, master_client, master_ws = _setup_and_parse_media(config, state)
        context = state["context"]
        browser = state["browser"]
        p = state["p"]

        emails_found_this_run = 0

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
            sheets_sync.sync_tab(master_client, parse_refunnel.MASTER_COLUMNS, result.master, never_delete=True,
                                 preserve_columns=parse_refunnel.SHEET_OWNED_COLUMNS,
                                 sort_key="created_at", sort_reverse=True)

            # scrape_creator_emails() now owns its own scroll reset
            # entirely (both at its own start and after every failed
            # search) -- see its docstring and scroll_to_top()'s for
            # the full confirmed evidence. No separate call needed
            # here anymore.

            def _save_email_incrementally(media_id: str, email_addr: str) -> None:
                found = master_client.update_single_cell(media_id, "creator_email", email_addr)
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

            # CONFIRMED REAL need: a live run was killed outright by
            # GitHub's 6-hour job cap ("Error: The operation was
            # canceled") while still mid-scrape. Everything scraped up
            # to that point WAS safe (emails are written to the sheet
            # incrementally as they're found), but the run never got to
            # export payments or write the Usage Rights / Human Review
            # tabs at all -- those only happen after this loop.
            #
            # So scraping now stops itself at a budget comfortably
            # inside the job cap, letting the rest of the run finish
            # cleanly. This is NOT "giving up early" -- every unscraped
            # id is still in the sheet with a blank email and is picked
            # up by the next scheduled run, exactly like the restart
            # logic already does. Tune with SCRAPE_TIME_BUDGET_MINUTES.
            scrape_budget_minutes = float(os.environ.get("SCRAPE_TIME_BUDGET_MINUTES", "270"))
            scrape_deadline = time.monotonic() + scrape_budget_minutes * 60

            # SCRAPE_MAX_POSTS caps how many posts one run attempts -- for a
            # quick live smoke test (e.g. 25) after a change, instead of
            # committing to all ~4,000 and cancelling by hand. The allowed
            # set is fixed ONCE, here, so a crash-restart mid-test can't
            # quietly start another batch. Unset or 0 means no cap;
            # anything left keeps its blank email and is picked up next run.
            max_posts = int(os.environ.get("SCRAPE_MAX_POSTS", "0") or "0")
            smoke_test_ids = None
            if max_posts > 0:
                smoke_test_ids = set(parse_refunnel.rows_needing_email_scrape(result)[:max_posts])
                print(f"SCRAPE_MAX_POSTS={max_posts}: this run will attempt at most "
                      f"{len(smoke_test_ids)} post(s) in total.")

            for attempt in range(max_scrape_restarts + 1):
                if time.monotonic() >= scrape_deadline:
                    print(f"Scraping has used its {scrape_budget_minutes:.0f}-minute budget for this "
                          f"run -- stopping here so payments and the remaining sheet tabs still get "
                          f"written. Nothing is lost: every post still missing an email is picked up "
                          f"automatically by the next scheduled run.")
                    break
                target_ids = [
                    mid for mid in parse_refunnel.rows_needing_email_scrape(result)
                    if mid not in already_confirmed_empty
                ]
                if smoke_test_ids is not None:
                    target_ids = [mid for mid in target_ids if mid in smoke_test_ids]
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
                    emails_found_this_run += updated
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
                    page_alive = True
                except Exception:
                    page_alive = False

                # A logged-out page is ALIVE but useless -- confirmed
                # real: a session expired mid-run, page.evaluate() kept
                # answering fine (it's a perfectly healthy login page),
                # so this check said "survived", broke out of the retry
                # loop without re-authenticating, and the run then died
                # on the Payments export. Being bounced to the login
                # screen needs exactly the same recovery as a crash --
                # a fresh session -- so it must not count as survival.
                logged_out = page_alive and refunnel_export._is_logged_out(page)
                page_survived = page_alive and not logged_out

                if page_survived:
                    # Scraping stopped for a reason other than a crash
                    # or a lost session (e.g. the circuit breaker
                    # tripped on a genuine, non-crash problem) --
                    # retrying won't help there, so don't burn a
                    # restart on it.
                    break

                reason = "The session was logged out" if logged_out else "Browser crashed"

                if attempt >= max_scrape_restarts:
                    print(f"{reason} during scraping and the {max_scrape_restarts}-restart "
                          f"budget for this run is used up -- moving on with what was found. "
                          f"The rest will be picked up on a future run.")

                else:
                    print(f"{reason} during scraping -- recovering and resuming "
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
                state["p"], state["browser"], state["context"] = p, browser, context
                page = context.new_page()
                state["page"] = page
                refunnel_export.goto_social_listening_for_workspace(page, refunnel_workspace_name, known_workspace_names)

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
                #
                # But "continue anyway with whatever's loaded" turned
                # out to be actively harmful -- CONFIRMED REAL from a
                # live run: after restart 6 the re-scroll stopped at
                # 1760/5878, leaving the container at scrollHeight
                # 207346 when the full grid is ~692005 (about 30%
                # loaded). Every one of the remaining 2206 posts then
                # failed with "couldn't locate", 25 per progress line,
                # because they genuinely were not in the DOM to find --
                # burning the rest of the job's wall-clock on posts
                # that COULD NOT succeed, until GitHub's 6-hour cap
                # cancelled the whole thing.
                #
                # So: retry the re-scroll a few times, and if it still
                # can't load the full grid, DON'T grind -- treat it as
                # another crash-style recovery (fresh browser + fresh
                # re-scroll) on the next loop pass. That's still "never
                # give up early"; it just doesn't waste the remaining
                # budget on a page that's structurally incapable of
                # answering.
                rescroll_ok = False
                for rescroll_attempt in range(3):
                    try:
                        refunnel_export.scroll_to_load_all(page)
                        rescroll_ok = True
                        break
                    except Exception as e:
                        print(f"WARNING: re-scroll after recovery didn't finish cleanly "
                              f"(attempt {rescroll_attempt + 1} of 3) -- {type(e).__name__}: {e}")
                        try:
                            refunnel_export.goto_social_listening_for_workspace(
                                page, refunnel_workspace_name, known_workspace_names
                            )
                        except Exception:
                            break  # page is in worse shape; let the next loop pass recover it properly

                if not rescroll_ok:
                    print("Re-scroll couldn't load the full grid after 3 tries. Forcing another "
                          "full recovery (fresh browser) rather than scraping against a "
                          "partially-loaded page, which can only produce 'couldn't locate' "
                          "failures for everything that isn't loaded.")
                    continue

        # --- NOW it's safe to navigate away and export payments ---
        # A failure here NO LONGER kills the entire run -- confirmed
        # real: a logged-out session made this Export click time out,
        # and that single exception threw away everything the run had
        # just spent over an hour doing (no sheet tabs written at all,
        # not even Master Data's finished state). Payments is one small
        # tab; media data is the bulk of the value. The Payments tab is
        # written with never_delete=True, so skipping it simply carries
        # the existing rows forward untouched rather than clearing them.
        try:
            page.goto(refunnel_auth.REFUNNEL_PAYMENTS_URL)
            payments_csv_path = refunnel_export.export_payments_csv(page, download_dir)
            result = parse_refunnel.parse_payments_csv(payments_csv_path, result=result)
        except Exception as e:
            print(f"WARNING: couldn't export Payments this run ({type(e).__name__}: {e}). "
                  f"Continuing anyway and writing every other tab -- the Payments tab keeps "
                  f"its existing rows (never_delete=True) rather than being cleared.")

        # Move anything you've marked "Reviewed" (a manual column you
        # add to Master Data yourself) out of Approved/Requested/Declined
        # and into Human Review instead.
        reviewed_values = sheets_sync.read_column_values(master_client, "Reviewed")
        reviewed_ids = {mid for mid, val in reviewed_values.items() if parse_refunnel.is_reviewed_value(val)}
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

        # --- push to sheets ---
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
            ("Human Review", parse_refunnel.HUMAN_REVIEW_COLUMNS, human_review_rows, False, "moved_to_human_review_at"),
            ("Payments", parse_refunnel.PAYMENT_COLUMNS, result.payments, True, None),
        ]

        # Copy each post's campaign from Master Data onto every
        # Master-schema tab (Approved / Requested / Declined), so the
        # campaign is visible wherever the post appears -- not only on
        # Master Data. Read once here, after scraping, from the sheet
        # itself: it's sheet-owned data that the CSV never contains.
        try:
            master_campaigns = sheets_sync.read_column_values(master_client, "campaigns")
        except Exception:
            master_campaigns = {}
        for bucket in (result.master, result.rights_approved,
                       result.rights_requested, result.rights_declined):
            for mid, row in bucket.items():
                if master_campaigns.get(mid):
                    row["campaigns"] = master_campaigns[mid]

        rows_written_summary = {}
        for title, columns, rows, never_delete, sort_key in tab_plan:
            ws = sheets_sync.get_or_create_worksheet(sh, title)
            client = sheets_sync.GspreadSheetsClient(ws)
            preserve = (parse_refunnel.SHEET_OWNED_COLUMNS
                        if columns is parse_refunnel.MASTER_COLUMNS else None)
            summary = sheets_sync.sync_tab(
                client, columns, rows, never_delete=never_delete,
                sort_key=sort_key, sort_reverse=(sort_key is not None),
                preserve_columns=preserve,
            )
            rows_written_summary[title] = summary["rows_written"]
            print(f"{title}: wrote {summary['rows_written']} rows "
                  f"(preserved columns: {summary['extra_columns_preserved']}, "
                  f"carried forward: {summary['rows_carried_forward']})")

        still_missing = len(parse_refunnel.rows_needing_email_scrape(result))
        print(
            f"=== PHASE 3 COMPLETE: {emails_found_this_run} email(s) found this run, "
            f"{still_missing} still missing. All 6 tabs written. ==="
        )
        return 0

    except Exception as e:
        notify_failure(f"{type(e).__name__}: {e}\n{traceback.format_exc()}")
        _save_debug_snapshot(state.get("page"), workspace_name)
        return 1

    finally:
        _close_browser(state)


def main() -> int:
    """Back-compat entry point: runs Phase 1 then Phase 3 in one
    process, one after another -- this script's original, single-step
    behavior. Kept for anyone still invoking this script directly
    without PIPELINE_PHASE set. The pipeline itself now calls
    main_phase1() and main_phase3() as two separate GitHub Actions
    steps instead (see full-pipeline-*.yml)."""
    phase = os.environ.get("PIPELINE_PHASE")
    if phase == "1":
        return main_phase1()
    if phase == "3":
        return main_phase3()
    result = main_phase1()
    if result != 0:
        return result
    return main_phase3()


if __name__ == "__main__":
    Path(DOWNLOAD_DIR).mkdir(exist_ok=True)
    sys.exit(main())

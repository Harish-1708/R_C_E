"""
refunnel_auth.py

Handles getting a logged-in Refunnel browser session, two ways:

1. capture_session_interactive() -- run this ONCE, by hand, on your own
   machine. It opens a real (visible) browser, you log in yourself
   (enter email, get the code from your own inbox, paste it, submit),
   and once you confirm you're in, it saves the session (cookies/storage)
   to a file. That file is what the daily job reuses.

2. load_or_refresh_session() -- what the daily job actually calls. It
   tries the saved session first. If Refunnel rejects it (session
   expired), it falls back to a fully automated login: request a fresh
   code, pull that code out of Gmail via gmail_otp.py, submit it, and
   save the new session for next time.

IMPORTANT -- selectors below are my best guess at Refunnel's real login
form, based on your description of the flow (enter email -> code is
emailed -> paste code -> login). I have not seen the actual login page,
so these element locators (SELECTORS dict) may need adjusting. They're
centralized at the top specifically so that's a quick fix rather than a
logic change. If a selector is wrong, the functions below raise a clear
error naming which step failed rather than hanging or failing silently.

This entire module is UNTESTED against the live site (no network access
to app.refunnel.com from this sandbox). Before relying on the scheduled
job, run capture_session_interactive() yourself locally and confirm it
completes; see README "Testing the login flow".
"""

from __future__ import annotations

import time
from datetime import date, timedelta
from pathlib import Path

from playwright.sync_api import BrowserContext, Page, sync_playwright

import gmail_otp

REFUNNEL_BASE_URL = "https://app.refunnel.com"
REFUNNEL_LOGIN_URL = f"{REFUNNEL_BASE_URL}/login"
# Confirmed from real analytics data embedded in a session export you
# shared -- the actual dashboard path includes /dashboard/content/, not
# just the page name.
#
# RE-ENABLED "Last 12 months": confirmed you can now scroll all the way
# down without stalling -- Refunnel resolved the platform-side bug that
# previously capped this at ~120 items regardless of automation vs
# manual scrolling (see git history / prior conversation for the full
# diagnosis). Matches the exact URL you captured from your address bar:
# Refunnel's "last12months" preset sets explicit from_date/to_date
# (to_date = today, from_date = exactly 365 days earlier) plus
# sort_by/snv params -- all included here, computed fresh each call so
# it stays a true rolling window, not a date frozen at whatever day
# this was written. If this ever breaks again on Refunnel's side, the
# safe fallback is reverting this one function back to a plain
# f-string with no query params at all (Refunnel's own "Last 3 months"
# default) -- see git history for that exact version.
def refunnel_social_listening_url() -> str:
    today = date.today()
    from_date = today - timedelta(days=365)
    return (
        f"{REFUNNEL_BASE_URL}/dashboard/content/social-listening"
        f"?sort_by=%22BY_DATE%22"
        f"&from_date=%22{from_date.isoformat()}%22"
        f"&to_date=%22{today.isoformat()}%22"
        f"&snv=true"
        f"&insights_timeline=%22last12months%22"
    )


# Confirmed for real (you sent the exact URL from your address bar):
# https://app.refunnel.com/dashboard/payments/history -- the earlier
# guess (`/dashboard/payments`, missing `/history`) was wrong, which is
# why a real run stayed on the Social Listening page and never found
# an Export button at all.
REFUNNEL_PAYMENTS_URL = f"{REFUNNEL_BASE_URL}/dashboard/payments/history"
# A URL/path Refunnel redirects logged-in users to, used to sanity-check
# whether a session is still valid. Adjust if your dashboard's home
# route is different.
REFUNNEL_LOGGED_IN_URL_FRAGMENT = "app.refunnel.com"
REFUNNEL_LOGIN_URL_FRAGMENT = "login"

DEFAULT_SESSION_FILE = "refunnel_session.json"

# --- CONFIG: centralize all login-form selectors here for easy fixing ---
SELECTORS = {
    "email_input": "input[type=email], input[placeholder*='email' i]",
    "send_code_button": "button:has-text('Send code'), button:has-text('Continue'), button:has-text('Send')",
    "code_input": "input[placeholder*='code' i], input[autocomplete='one-time-code']",
    "submit_code_button": "button:has-text('Verify'), button:has-text('Log in'), button:has-text('Continue')",
}


class LoginError(RuntimeError):
    pass


def _find_first(page: Page, selector: str, step_name: str, timeout_ms: int = 10000):
    try:
        el = page.locator(selector).first
        el.wait_for(state="visible", timeout=timeout_ms)
        return el
    except Exception as e:
        raise LoginError(
            f"Couldn't find the '{step_name}' element (selector: {selector!r}). "
            f"Refunnel's page markup may not match SELECTORS in refunnel_auth.py -- "
            f"run `playwright codegen {REFUNNEL_LOGIN_URL}` to find the real one. "
            f"Original error: {e}"
        ) from e


def is_session_valid(context: BrowserContext) -> bool:
    """Open a fresh page in this context and check whether Refunnel
    treats us as logged in (doesn't bounce to /login)."""
    page = context.new_page()
    try:
        page.goto(REFUNNEL_LOGIN_URL, wait_until="domcontentloaded", timeout=20000)
        # If already logged in, Refunnel should redirect away from /login.
        page.wait_for_timeout(2000)
        still_on_login = REFUNNEL_LOGIN_URL_FRAGMENT in page.url
        return not still_on_login
    except Exception:
        return False
    finally:
        page.close()


def _perform_login(page: Page, email: str, otp_wait_seconds: int = 90) -> None:
    """Full automated login: enter email, request code, fetch it from
    Gmail, submit it. Raises LoginError on any step failure."""
    page.goto(REFUNNEL_LOGIN_URL, wait_until="domcontentloaded", timeout=20000)

    email_input = _find_first(page, SELECTORS["email_input"], "email input")
    email_input.fill(email)

    request_ts = time.time()
    send_button = _find_first(page, SELECTORS["send_code_button"], "send code button")
    send_button.click()

    try:
        code = gmail_otp.fetch_latest_code(
            requested_after_ts=request_ts,
            max_wait_seconds=otp_wait_seconds,
        )
    except gmail_otp.OtpNotFoundError as e:
        raise LoginError(f"Automated login couldn't get a code from Gmail: {e}") from e

    code_input = _find_first(page, SELECTORS["code_input"], "code input")
    code_input.fill(code)

    submit_button = _find_first(page, SELECTORS["submit_code_button"], "submit code button")
    submit_button.click()

    page.wait_for_timeout(3000)
    if REFUNNEL_LOGIN_URL_FRAGMENT in page.url:
        raise LoginError(
            "Submitted the code but Refunnel didn't redirect away from /login -- "
            "the code may have been wrong, expired, or a selector matched the wrong element."
        )


def capture_session_interactive(session_file: str = DEFAULT_SESSION_FILE) -> None:
    """Run this by hand, once, on a machine with a display. Opens a real
    visible browser; YOU log in manually (this script does not touch
    your email or type anything); once you're in, press Enter in the
    terminal and it saves the session."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()
        page.goto(REFUNNEL_LOGIN_URL)

        print("A browser window has opened to the Refunnel login page.")
        print("Log in yourself: enter your email, get the code from your own inbox,")
        print("paste it in, and get to the dashboard.")
        input("Once you're logged in and see your dashboard, press Enter here...")

        if is_session_valid(context):
            context.storage_state(path=session_file)
            print(f"Session saved to {session_file}. Keep this file secret (it's a login credential).")
        else:
            print("Doesn't look like you're logged in yet (still seeing /login). Nothing was saved.")

        browser.close()


def load_or_refresh_session(
    email: str,
    session_file: str = DEFAULT_SESSION_FILE,
    headless: bool = True,
) -> tuple:
    """Returns (playwright, browser, context) with a valid logged-in
    Refunnel session -- reusing the saved one if it still works, or
    doing a full automated re-login (via Gmail OTP) if not. Caller is
    responsible for closing browser/playwright when done.

    Raises LoginError if neither the saved session nor a fresh login works.
    """
    p = sync_playwright().start()
    browser = p.chromium.launch(headless=headless)

    session_path = Path(session_file)
    if session_path.exists():
        context = browser.new_context(storage_state=str(session_path))
        if is_session_valid(context):
            return p, browser, context
        context.close()

    # saved session missing or expired -- fall back to a fresh login
    context = browser.new_context()
    page = context.new_page()
    try:
        _perform_login(page, email)
    finally:
        page.close()

    if not is_session_valid(context):
        browser.close()
        p.stop()
        raise LoginError("Fresh login via Gmail-OTP fallback did not result in a valid session.")

    context.storage_state(path=session_file)
    return p, browser, context

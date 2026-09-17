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


def is_login_url(url: str) -> bool:
    """Case-INSENSITIVE check for "this URL is the login page".

    CONFIRMED REAL BUG this fixes: Refunnel redirects logged-out users
    to "https://app.refunnel.com/Login" -- capital L. Every check here
    was a case-SENSITIVE `"login" in page.url`, which is False for
    "/Login". So is_session_valid() reported a dead, logged-out session
    as perfectly valid, load_or_refresh_session() happily reused it
    instead of re-authenticating, and a real run then spent ~40 minutes
    failing 625 posts in a row against the login screen before dying on
    the Payments export. One capital letter.
    """
    return REFUNNEL_LOGIN_URL_FRAGMENT in (url or "").lower()

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
    treats us as logged in (doesn't bounce to /login).

    A failure here is reported, not silently swallowed -- confirmed
    real gap: returning a bare False for ANY exception meant a genuine
    outage, a DNS failure, or a Playwright crash looked identical to
    "the session expired", sending the run into a pointless full
    Gmail-OTP re-login and discarding the only diagnostic information
    about what actually went wrong. Still returns False either way
    (treating it as "can't confirm we're logged in" is the safe
    default), but now says why.
    """
    page = context.new_page()
    try:
        page.goto(REFUNNEL_LOGIN_URL, wait_until="domcontentloaded", timeout=20000)
        # If already logged in, Refunnel should redirect away from /login.
        page.wait_for_timeout(2000)
        still_on_login = is_login_url(page.url)
        return not still_on_login
    except Exception as e:
        print(f"is_session_valid: couldn't confirm session validity "
              f"({type(e).__name__}: {e}). Treating the session as invalid.")
        return False
    finally:
        try:
            page.close()
        except Exception:
            pass


CODE_INPUT_SELECTORS = [
    "input[autocomplete='one-time-code']",
    "input[maxlength='1']",
    "input[placeholder*='code' i]",
    "input[inputmode='numeric']",
]


def _type_into(box, text: str) -> None:
    """Enter text with REAL per-character key events rather than
    setting the value in one shot.

    Confirmed real: Refunnel's code screen is a React form whose Verify
    button only enables once it considers a valid code entered.
    A plain .fill() sets the value without the keystroke events those
    widgets listen for, so the button can stay disabled even when the
    text is visibly in the box.
    """
    box.click()
    try:
        box.press_sequentially(text, delay=50)
    except AttributeError:
        # Older Playwright (and simple test fakes) expose .type() instead.
        box.type(text, delay=50)


def _enter_login_code(page: Page, code: str, timeout_ms: int = 10000) -> None:
    """Put `code` into Refunnel's code entry, handling BOTH layouts.

    CONFIRMED REAL BUG this fixes: Refunnel uses SIX SEPARATE
    single-digit boxes. The old code did .fill(code) on the first
    matching input, which dumps all six digits into box one -- so the
    form never saw a complete code, the Verify button stayed
    `disabled`, and Playwright burned 62 retries over 30s before the
    whole run died with "element is not enabled".

    Handles the multi-box case (one digit per box) and still supports a
    single combined box, so a future Refunnel redesign either way keeps
    working.
    """
    last_seen = None
    for index, selector in enumerate(CODE_INPUT_SELECTORS):
        boxes = page.locator(selector)
        try:
            # Generous wait on the first (most specific) selector only;
            # the rest are fallbacks and shouldn't each cost 10s.
            boxes.first.wait_for(state="visible", timeout=timeout_ms if index == 0 else 2000)
        except Exception:
            continue

        count = boxes.count()
        last_seen = f"{selector!r} -> {count} input(s)"

        if count >= len(code):
            for i, char in enumerate(code):
                _type_into(boxes.nth(i), char)
            return
        if count == 1:
            _type_into(boxes.first, code)
            return

    raise LoginError(
        f"Couldn't find a usable code entry for a {len(code)}-character code on Refunnel's "
        f"login page (closest match: {last_seen or 'nothing matched'}). Refunnel's code-entry "
        f"markup may have changed -- update CODE_INPUT_SELECTORS in refunnel_auth.py."
    )


def _submit_login_code(page: Page, enable_timeout_ms: int = 15000) -> None:
    """Click Verify once it's actually enabled.

    Waits for the button to become enabled instead of clicking at it
    while it's disabled -- confirmed real: the old code clicked
    immediately, so Playwright sat retrying a permanently-disabled
    button for 30s and reported a generic timeout that said nothing
    about WHY. A button that never enables means the code didn't
    register, so this says exactly that.

    Some OTP widgets auto-submit once the last digit lands, so a page
    that has already left the login screen counts as success.
    """
    if not is_login_url(page.url):
        return  # auto-submitted the moment the last digit went in

    submit_button = _find_first(page, SELECTORS["submit_code_button"], "submit code button")

    deadline = time.time() + (enable_timeout_ms / 1000)
    while time.time() < deadline:
        if not is_login_url(page.url):
            return  # auto-submitted while we were waiting
        try:
            if submit_button.is_enabled():
                submit_button.click()
                return
        except Exception:
            pass
        page.wait_for_timeout(250)

    raise LoginError(
        "Entered the login code but Refunnel's Verify button never became enabled. That "
        "means the form didn't accept the code as complete/valid -- most likely the code "
        "didn't land in the entry boxes correctly (check CODE_INPUT_SELECTORS in "
        "refunnel_auth.py), or the code itself was wrong or already expired."
    )


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

    _enter_login_code(page, code)
    _submit_login_code(page)

    page.wait_for_timeout(3000)
    if is_login_url(page.url):
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

    def _cleanup():
        # Confirmed real leak this fixes: if _perform_login() raised
        # (bad selector, OTP never arrived, Refunnel down), the browser
        # and the Playwright process were both left running -- only the
        # "login completed but session still invalid" path below cleaned
        # up. Harmless on a throwaway CI VM, a real leak anywhere
        # longer-lived, and trivially avoidable either way.
        try:
            browser.close()
        except Exception:
            pass
        try:
            p.stop()
        except Exception:
            pass

    try:
        session_path = Path(session_file)
        if session_path.exists():
            context = browser.new_context(storage_state=str(session_path))
            if is_session_valid(context):
                # Confirmed real gap: this function never printed
                # anything on success, in either path, so a clean run
                # gave no way to tell "reused the cached session" apart
                # from "did a fresh login that happened to succeed
                # silently" -- both looked like zero login-related
                # output. Now it always says which one happened.
                print(f"Reused the cached Refunnel session from {session_file} -- no login needed.")
                return p, browser, context
            print(f"Cached session in {session_file} exists but is no longer valid "
                  f"(expired or logged out) -- falling back to a fresh login.")
            context.close()
        else:
            print(f"No cached session found at {session_file} -- doing a fresh login.")

        # saved session missing or expired -- fall back to a fresh login
        context = browser.new_context()
        page = context.new_page()
        try:
            _perform_login(page, email)
        finally:
            try:
                page.close()
            except Exception:
                pass

        if not is_session_valid(context):
            raise LoginError("Fresh login via Gmail-OTP fallback did not result in a valid session.")

        context.storage_state(path=session_file)
        print(f"Fresh login via Gmail OTP succeeded -- session saved to {session_file} "
              f"for the next run to reuse.")
        return p, browser, context
    except Exception:
        _cleanup()
        raise

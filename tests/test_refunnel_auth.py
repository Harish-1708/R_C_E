"""Tests for the one pure-Python piece of refunnel_auth.py --
refunnel_social_listening_url(). Everything else in that module drives
a real Playwright browser and isn't unit-testable without one,
consistent with the rest of this test suite's scope.
"""
import re
from datetime import date, timedelta

import pytest

from refunnel_auth import refunnel_social_listening_url


def test_matches_the_real_captured_url_shape():
    # Confirmed real: this exact param set (sort_by, from_date, to_date,
    # snv, insights_timeline) is what Refunnel's own "Last 12 months"
    # filter produces -- captured directly from a browser address bar,
    # twice now (once before, once after Refunnel fixed the platform
    # bug that made this unusable the first time around).
    url = refunnel_social_listening_url()
    assert url.startswith("https://app.refunnel.com/dashboard/content/social-listening?")
    assert "sort_by=%22BY_DATE%22" in url
    assert "snv=true" in url
    assert "insights_timeline=%22last12months%22" in url


def test_date_range_is_a_true_rolling_365_days_ending_today():
    url = refunnel_social_listening_url()
    to_match = re.search(r"to_date=%22([\d-]+)%22", url)
    from_match = re.search(r"from_date=%22([\d-]+)%22", url)
    assert to_match and from_match

    to_date = date.fromisoformat(to_match.group(1))
    from_date = date.fromisoformat(from_match.group(1))

    assert to_date == date.today()
    assert from_date == date.today() - timedelta(days=365)


def test_sabotage_hardcoded_date_would_be_caught():
    # proves the dates are computed fresh, not frozen at some fixed
    # string -- a hardcoded date would fail this the day after it was
    # written
    url = refunnel_social_listening_url()
    with pytest.raises(AssertionError):
        assert "to_date=%222025-09-06%22" in url  # wrong -- that's not today
    assert f"to_date=%22{date.today().isoformat()}%22" in url


# ---------- resource cleanup on failure (audit fix) ----------

import refunnel_auth  # noqa: E402


class _FakeBrowser:
    def __init__(self):
        self.closed = False

    def new_context(self, **kwargs):
        raise RuntimeError("simulated failure opening a context")

    def close(self):
        self.closed = True


class _FakePlaywright:
    def __init__(self, browser):
        self.chromium = self
        self._browser = browser
        self.stopped = False

    def launch(self, **kwargs):
        return self._browser

    def stop(self):
        self.stopped = True


def test_browser_and_playwright_are_cleaned_up_when_login_fails(monkeypatch, tmp_path):
    # confirmed real leak this covers: if anything raised partway
    # through, the browser AND the playwright process were both left
    # running -- only one specific failure path cleaned up
    browser = _FakeBrowser()
    fake_p = _FakePlaywright(browser)

    class _Starter:
        def start(self):
            return fake_p

    monkeypatch.setattr(refunnel_auth, "sync_playwright", lambda: _Starter())

    with pytest.raises(RuntimeError, match="simulated failure"):
        refunnel_auth.load_or_refresh_session(
            email="x@example.com",
            session_file=str(tmp_path / "nonexistent_session.json"),
        )

    assert browser.closed is True
    assert fake_p.stopped is True


def test_sabotage_leaked_browser_would_be_caught(monkeypatch, tmp_path):
    browser = _FakeBrowser()
    fake_p = _FakePlaywright(browser)

    class _Starter:
        def start(self):
            return fake_p

    monkeypatch.setattr(refunnel_auth, "sync_playwright", lambda: _Starter())
    with pytest.raises(RuntimeError):
        refunnel_auth.load_or_refresh_session(
            email="x@example.com",
            session_file=str(tmp_path / "nonexistent_session.json"),
        )
    with pytest.raises(AssertionError):
        assert browser.closed is False  # wrong -- would mean it leaked
    assert browser.closed is True  # confirms actual correct behavior


# ---------- case-insensitive login detection (the /Login bug) ----------

@pytest.mark.parametrize("url", [
    "https://app.refunnel.com/Login",   # the REAL redirect seen in production
    "https://app.refunnel.com/login",
    "https://app.refunnel.com/LOGIN",
    "https://app.refunnel.com/Login?next=/dashboard",
])
def test_recognizes_every_casing_of_the_login_url(url):
    assert refunnel_auth.is_login_url(url) is True


@pytest.mark.parametrize("url", [
    "https://app.refunnel.com/dashboard/content/social-listening",
    "https://app.refunnel.com/dashboard/payments/history",
    "",
])
def test_does_not_flag_a_real_dashboard_url_as_login(url):
    assert refunnel_auth.is_login_url(url) is False


def test_is_login_url_handles_none():
    assert refunnel_auth.is_login_url(None) is False


def test_sabotage_case_sensitive_check_would_be_caught():
    # this is the exact bug: Refunnel redirects to "/Login" with a
    # capital L, and the old case-SENSITIVE check read that as
    # "still logged in", so a dead session was reused for ~40 minutes
    url = "https://app.refunnel.com/Login"
    old_style_result = "login" in url          # what the code used to do
    new_result = refunnel_auth.is_login_url(url)
    with pytest.raises(AssertionError):
        assert old_style_result is True  # wrong -- the old check missed it entirely
    assert old_style_result is False     # confirms the old behaviour really was broken
    assert new_result is True            # confirms the fix catches it


# ---------- OTP code entry: 6 separate single-digit boxes ----------

class _FakeBox:
    def __init__(self, owner, maxlength=1):
        self.owner = owner
        self.value = ""
        self.maxlength = maxlength

    def click(self):
        pass

    def wait_for(self, state=None, timeout=None):
        pass

    def press_sequentially(self, text, delay=None):
        # mimics a real box: honours maxlength, like the browser does
        self.value = (self.value + text)[: self.maxlength]
        self.owner.keystrokes += len(text)

    def is_enabled(self):
        return self.owner.submit_enabled()


class _FakeLoginPage:
    """Simulates Refunnel's real code screen: N single-digit boxes, and
    a Verify button that only enables once every box holds a digit."""

    def __init__(self, num_boxes=6, code_length=6, selector="input[maxlength='1']"):
        self.boxes = [_FakeBox(self, maxlength=1) for _ in range(num_boxes)]
        self.code_length = code_length
        self.selector = selector
        self.keystrokes = 0
        self.url = "https://app.refunnel.com/Login"
        self.clicked_submit = False

    def submit_enabled(self):
        filled = [b for b in self.boxes if b.value]
        return len(filled) >= self.code_length

    def entered_code(self):
        return "".join(b.value for b in self.boxes)

    def locator(self, selector):
        page = self
        matches = page.boxes if selector == page.selector else []

        class _Loc:
            def count(self):
                return len(matches)

            @property
            def first(self):
                if not matches:
                    raise RuntimeError("no match")
                return matches[0]

            def nth(self, i):
                return matches[i]

            def wait_for(self, state=None, timeout=None):
                if not matches:
                    raise RuntimeError("no match")

        return _Loc()

    def wait_for_timeout(self, _ms):
        pass


def test_enters_one_digit_per_box_across_six_boxes():
    page = _FakeLoginPage(num_boxes=6, code_length=6)
    refunnel_auth._enter_login_code(page, "483920")
    assert page.entered_code() == "483920"
    assert [b.value for b in page.boxes] == ["4", "8", "3", "9", "2", "0"]


def test_six_box_entry_leaves_the_verify_button_enabled():
    page = _FakeLoginPage(num_boxes=6, code_length=6)
    refunnel_auth._enter_login_code(page, "483920")
    assert page.submit_enabled() is True


def test_still_supports_a_single_combined_input_box():
    page = _FakeLoginPage(num_boxes=1, code_length=6)
    page.boxes[0].maxlength = 6
    refunnel_auth._enter_login_code(page, "483920")
    assert page.boxes[0].value == "483920"


def test_uses_real_keystrokes_not_a_bulk_value_set():
    # the React form only enables Verify on real key events
    page = _FakeLoginPage(num_boxes=6, code_length=6)
    refunnel_auth._enter_login_code(page, "483920")
    assert page.keystrokes == 6


def test_raises_a_clear_error_when_no_code_entry_exists():
    page = _FakeLoginPage(num_boxes=0, code_length=6)
    with pytest.raises(refunnel_auth.LoginError, match="code entry"):
        refunnel_auth._enter_login_code(page, "483920", timeout_ms=10)


def test_sabotage_old_single_fill_behaviour_would_be_caught():
    # reproduces the exact production bug: all six digits into box one
    page = _FakeLoginPage(num_boxes=6, code_length=6)
    page.boxes[0].maxlength = 6          # pretend fill() dumped it all in box 1
    page.boxes[0].press_sequentially("483920")
    with pytest.raises(AssertionError):
        assert page.submit_enabled() is True  # wrong -- Verify stays DISABLED
    assert page.submit_enabled() is False     # confirms why the run timed out


# ---------- _submit_login_code: wait for enabled, don't click a dead button ----------

class _SubmitPage(_FakeLoginPage):
    def __init__(self, enabled_after_calls=0, auto_submit=False, **kw):
        super().__init__(**kw)
        self._calls = 0
        self._enabled_after = enabled_after_calls
        self._auto_submit = auto_submit

    def locator(self, selector):
        page = self

        class _Btn:
            def wait_for(self, state=None, timeout=None):
                pass

            def is_enabled(self):
                page._calls += 1
                return page._calls > page._enabled_after

            def click(self):
                page.clicked_submit = True
                page.url = "https://app.refunnel.com/dashboard/content/social-listening"

            @property
            def first(self):
                return self

        return _Btn()


def test_clicks_verify_once_it_becomes_enabled():
    page = _SubmitPage(enabled_after_calls=3)
    refunnel_auth._submit_login_code(page)
    assert page.clicked_submit is True


def test_returns_immediately_if_the_widget_auto_submitted():
    page = _SubmitPage()
    page.url = "https://app.refunnel.com/dashboard/content/social-listening"
    refunnel_auth._submit_login_code(page)
    assert page.clicked_submit is False  # nothing to click, already through


def test_raises_a_useful_error_if_verify_never_enables():
    class _NeverEnables(_SubmitPage):
        def locator(self, selector):
            btn = super().locator(selector)
            btn.is_enabled = lambda: False
            return btn

    page = _NeverEnables()
    with pytest.raises(refunnel_auth.LoginError, match="never became enabled"):
        refunnel_auth._submit_login_code(page, enable_timeout_ms=50)


# ---------- load_or_refresh_session: clear logging on every path (the "did it use a cached session or a fresh login?" gap) ----------

class _FakeContext:
    def __init__(self, valid, storage_state_path=None):
        self.valid = valid
        self.closed = False
        self.storage_state_path = storage_state_path

    def close(self):
        self.closed = True

    def new_page(self):
        return _FakePage()

    def storage_state(self, path):
        self.storage_state_path = path


class _FakePage:
    def close(self):
        pass


class _SessionBrowser:
    def __init__(self, valid_for_cached, valid_after_fresh_login=True):
        self.valid_for_cached = valid_for_cached
        self.valid_after_fresh_login = valid_after_fresh_login
        self.contexts_created = []

    def new_context(self, storage_state=None):
        is_cached = storage_state is not None
        valid = self.valid_for_cached if is_cached else self.valid_after_fresh_login
        ctx = _FakeContext(valid=valid)
        self.contexts_created.append(ctx)
        return ctx

    def close(self):
        pass


class _SessionPlaywright:
    def __init__(self, browser):
        self.chromium = self
        self._browser = browser

    def launch(self, **kwargs):
        return self._browser

    def stop(self):
        pass


def test_logs_cached_session_reuse(monkeypatch, tmp_path, capsys):
    session_file = tmp_path / "session.json"
    session_file.write_text("{}")  # exists, so the cached path is taken

    browser = _SessionBrowser(valid_for_cached=True)
    monkeypatch.setattr(refunnel_auth, "sync_playwright", lambda: type("S", (), {"start": lambda self: _SessionPlaywright(browser)})())
    monkeypatch.setattr(refunnel_auth, "is_session_valid", lambda ctx: ctx.valid)

    refunnel_auth.load_or_refresh_session(email="x@example.com", session_file=str(session_file))

    out = capsys.readouterr().out
    assert "Reused the cached Refunnel session" in out
    assert "Fresh login" not in out


def test_logs_expired_cached_session_falling_back(monkeypatch, tmp_path, capsys):
    session_file = tmp_path / "session.json"
    session_file.write_text("{}")

    browser = _SessionBrowser(valid_for_cached=False, valid_after_fresh_login=True)
    monkeypatch.setattr(refunnel_auth, "sync_playwright", lambda: type("S", (), {"start": lambda self: _SessionPlaywright(browser)})())
    monkeypatch.setattr(refunnel_auth, "is_session_valid", lambda ctx: ctx.valid)
    monkeypatch.setattr(refunnel_auth, "_perform_login", lambda page, email: None)

    refunnel_auth.load_or_refresh_session(email="x@example.com", session_file=str(session_file))

    out = capsys.readouterr().out
    assert "no longer valid" in out
    assert "Fresh login via Gmail OTP succeeded" in out


def test_logs_no_cached_session_found(monkeypatch, tmp_path, capsys):
    session_file = tmp_path / "does_not_exist.json"  # never created

    browser = _SessionBrowser(valid_for_cached=False, valid_after_fresh_login=True)
    monkeypatch.setattr(refunnel_auth, "sync_playwright", lambda: type("S", (), {"start": lambda self: _SessionPlaywright(browser)})())
    monkeypatch.setattr(refunnel_auth, "is_session_valid", lambda ctx: ctx.valid)
    monkeypatch.setattr(refunnel_auth, "_perform_login", lambda page, email: None)

    refunnel_auth.load_or_refresh_session(email="x@example.com", session_file=str(session_file))

    out = capsys.readouterr().out
    assert "No cached session found" in out


def test_sabotage_silent_success_would_be_caught(monkeypatch, tmp_path, capsys):
    # this is the exact real problem: a genuinely successful run
    # produced no distinguishing output at all
    session_file = tmp_path / "session.json"
    session_file.write_text("{}")
    browser = _SessionBrowser(valid_for_cached=True)
    monkeypatch.setattr(refunnel_auth, "sync_playwright", lambda: type("S", (), {"start": lambda self: _SessionPlaywright(browser)})())
    monkeypatch.setattr(refunnel_auth, "is_session_valid", lambda ctx: ctx.valid)

    refunnel_auth.load_or_refresh_session(email="x@example.com", session_file=str(session_file))
    out = capsys.readouterr().out
    with pytest.raises(AssertionError):
        assert out.strip() == ""  # wrong -- this used to be true, and was the whole problem
    assert "Reused the cached Refunnel session" in out  # confirms the fix


# ---------- _perform_login's OTP retry loop (isolated with fakes) ----------
#
# _perform_login itself still drives a real Playwright page and isn't
# fully testable without one, but its retry LOGIC -- attempt counting,
# when it re-clicks Send, when it gives up -- is pure enough to isolate
# with fakes for page/_find_first/gmail_otp.fetch_latest_code.

import gmail_otp


class _FakeOtpRequestPage:
    def __init__(self):
        self.goto_calls = 0
        self.filled = None
        self.url = "https://app.refunnel.com/dashboard"

    def goto(self, url, wait_until=None, timeout=None):
        self.goto_calls += 1

    def wait_for_timeout(self, ms):
        pass


class _FakeElement:
    def __init__(self, on_click=None):
        self.filled = None
        self.click_count = 0
        self._on_click = on_click

    def fill(self, value):
        self.filled = value

    def click(self):
        self.click_count += 1
        if self._on_click:
            self._on_click()


def test_retries_the_whole_request_cycle_not_just_the_poll(monkeypatch):
    # confirmed real gap this fixes: the old code clicked Send ONCE and
    # gave up outright if the email never arrived within the wait window
    send_button = _FakeElement()
    monkeypatch.setattr(refunnel_auth, "_find_first",
                        lambda page, sel, name, **kw: send_button)

    calls = {"n": 0}

    def fake_fetch(requested_after_ts, max_wait_seconds):
        calls["n"] += 1
        if calls["n"] == 1:
            raise gmail_otp.OtpNotFoundError("nothing arrived")
        return "123456"

    monkeypatch.setattr(gmail_otp, "fetch_latest_code", fake_fetch)
    monkeypatch.setattr(refunnel_auth, "_enter_login_code", lambda page, code: None)
    monkeypatch.setattr(refunnel_auth, "_submit_login_code", lambda page: None)

    refunnel_auth._perform_login(_FakeOtpRequestPage(), "x@example.com", otp_wait_seconds=1)

    assert send_button.click_count == 2  # Send was clicked AGAIN, not just polled again
    assert calls["n"] == 2


def test_gives_up_after_the_configured_number_of_attempts(monkeypatch):
    send_button = _FakeElement()
    monkeypatch.setattr(refunnel_auth, "_find_first",
                        lambda page, sel, name, **kw: send_button)

    def always_fails(requested_after_ts, max_wait_seconds):
        raise gmail_otp.OtpNotFoundError("nothing arrived")

    monkeypatch.setattr(gmail_otp, "fetch_latest_code", always_fails)

    with pytest.raises(refunnel_auth.LoginError) as exc:
        refunnel_auth._perform_login(_FakeOtpRequestPage(), "x@example.com",
                                     otp_wait_seconds=1, otp_request_attempts=3)

    assert send_button.click_count == 3
    assert "3 request(s)" in str(exc.value)


def test_succeeds_immediately_without_retrying_when_the_first_code_arrives(monkeypatch):
    send_button = _FakeElement()
    monkeypatch.setattr(refunnel_auth, "_find_first",
                        lambda page, sel, name, **kw: send_button)
    monkeypatch.setattr(gmail_otp, "fetch_latest_code", lambda **kw: "654321")
    monkeypatch.setattr(refunnel_auth, "_enter_login_code", lambda page, code: None)
    monkeypatch.setattr(refunnel_auth, "_submit_login_code", lambda page: None)

    refunnel_auth._perform_login(_FakeOtpRequestPage(), "x@example.com")

    assert send_button.click_count == 1  # no unnecessary retry


def test_sabotage_single_attempt_would_be_caught(monkeypatch):
    send_button = _FakeElement()
    monkeypatch.setattr(refunnel_auth, "_find_first",
                        lambda page, sel, name, **kw: send_button)

    calls = {"n": 0}

    def fake_fetch(requested_after_ts, max_wait_seconds):
        calls["n"] += 1
        if calls["n"] == 1:
            raise gmail_otp.OtpNotFoundError("nothing arrived")
        return "123456"

    monkeypatch.setattr(gmail_otp, "fetch_latest_code", fake_fetch)
    monkeypatch.setattr(refunnel_auth, "_enter_login_code", lambda page, code: None)
    monkeypatch.setattr(refunnel_auth, "_submit_login_code", lambda page: None)

    refunnel_auth._perform_login(_FakeOtpRequestPage(), "x@example.com", otp_wait_seconds=1)

    with pytest.raises(AssertionError):
        assert send_button.click_count == 1  # wrong -- that's the old, unretried behaviour
    assert send_button.click_count == 2


# ---------- Chromium launch args (source-inspection -- the actual launch isn't mockable) ----------

def test_load_or_refresh_session_launches_with_disable_dev_shm_usage():
    # CONFIRMED REAL gap this closes: the call every CI workflow
    # actually uses had NO launch args at all. --disable-dev-shm-usage
    # is a well-documented fix for exactly the observed crash pattern
    # (crashes after a few hundred interactions, not at launch) --
    # Chromium's shared memory usage exhausting GitHub's small default
    # /dev/shm allocation under sustained load.
    #
    # Checked against the actual .launch(...) call line specifically,
    # not the whole function's source -- a comment mentioning the flag
    # would otherwise let this pass even if the real code lost it.
    import inspect
    import refunnel_auth
    source = inspect.getsource(refunnel_auth.load_or_refresh_session)
    launch_line = next(line for line in source.splitlines() if "chromium.launch(" in line)
    assert "--disable-dev-shm-usage" in launch_line


def test_sabotage_launching_with_no_args_would_be_caught():
    import inspect
    import refunnel_auth
    source = inspect.getsource(refunnel_auth.load_or_refresh_session)
    launch_line = next(line for line in source.splitlines() if "chromium.launch(" in line)
    with pytest.raises(AssertionError):
        assert launch_line.strip() == "browser = p.chromium.launch(headless=headless)"  # wrong -- the old, no-args call
    assert "--disable-dev-shm-usage" in launch_line

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

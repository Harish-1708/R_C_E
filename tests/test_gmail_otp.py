"""
Tests for gmail_otp.py, using a fake IMAP connection. These test the
polling/matching/extraction logic in isolation -- they do NOT touch a
real Gmail account (no network access to Gmail's IMAP server in this
sandbox). _connect is monkeypatched out entirely.

Manual smoke test still required once a real App Password exists --
see README "Testing the Gmail OTP fallback".
"""

import email.utils
import time

import pytest

import gmail_otp
from gmail_otp import fetch_latest_code, OtpNotFoundError


def _make_raw_email(internal_ts: float, body_text: str) -> bytes:
    date_str = email.utils.formatdate(internal_ts, localtime=False)
    raw = (
        f"From: Refunnel <no-reply@refunnel.com>\r\n"
        f"To: harish@kelson.agency\r\n"
        f"Subject: Your login code\r\n"
        f"Date: {date_str}\r\n"
        f"Content-Type: text/plain; charset=utf-8\r\n"
        f"\r\n"
        f"{body_text}\r\n"
    )
    return raw.encode("utf-8")


class FakeImap:
    """In-memory stand-in for imaplib.IMAP4_SSL. messages: {msg_id_bytes: raw_email_bytes}"""

    def __init__(self, messages: dict, matching_ids: list):
        self._messages = messages
        self._matching_ids = matching_ids
        self.logged_out = False

    def search(self, charset, criteria):
        return "OK", [b" ".join(self._matching_ids)]

    def fetch(self, msg_id, spec):
        raw = self._messages.get(msg_id)
        if raw is None:
            return "NO", [None]
        return "OK", [(b"1 (RFC822 {%d}" % len(raw), raw)]

    def logout(self):
        self.logged_out = True


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    # keep tests fast -- don't actually wait poll_interval_seconds
    monkeypatch.setattr(time, "sleep", lambda _: None)


def test_extracts_code_from_matching_recent_email(monkeypatch):
    now = time.time()
    raw = _make_raw_email(now, "Your Refunnel login code is 483920. It expires in 10 minutes.")
    fake = FakeImap(messages={b"1": raw}, matching_ids=[b"1"])
    monkeypatch.setattr(gmail_otp, "_connect", lambda: fake)

    code = fetch_latest_code(requested_after_ts=now - 1, max_wait_seconds=5, poll_interval_seconds=1)
    assert code == "483920"
    assert fake.logged_out is True


def test_ignores_email_older_than_requested_after(monkeypatch):
    now = time.time()
    raw = _make_raw_email(now - 3600, "Your code is 111111")  # 1hr old -- stale
    fake = FakeImap(messages={b"1": raw}, matching_ids=[b"1"])
    monkeypatch.setattr(gmail_otp, "_connect", lambda: fake)

    with pytest.raises(OtpNotFoundError):
        fetch_latest_code(requested_after_ts=now, max_wait_seconds=2, poll_interval_seconds=1)


def test_picks_the_newest_of_multiple_candidates(monkeypatch):
    now = time.time()
    raw_a = _make_raw_email(now - 2, "Your code is 111111")
    raw_b = _make_raw_email(now, "Your code is 222222")  # newer -- should win
    fake = FakeImap(messages={b"1": raw_a, b"2": raw_b}, matching_ids=[b"1", b"2"])
    monkeypatch.setattr(gmail_otp, "_connect", lambda: fake)

    code = fetch_latest_code(requested_after_ts=now - 5, max_wait_seconds=5, poll_interval_seconds=1)
    assert code == "222222"


def test_matching_email_with_no_extractable_code_raises_distinct_error(monkeypatch):
    now = time.time()
    raw = _make_raw_email(now, "Welcome to Refunnel! No code here.")
    fake = FakeImap(messages={b"1": raw}, matching_ids=[b"1"])
    monkeypatch.setattr(gmail_otp, "_connect", lambda: fake)

    with pytest.raises(OtpNotFoundError, match="couldn't extract a code"):
        fetch_latest_code(requested_after_ts=now - 1, max_wait_seconds=5, poll_interval_seconds=1)


def test_no_messages_at_all_raises_after_timeout(monkeypatch):
    fake = FakeImap(messages={}, matching_ids=[])
    monkeypatch.setattr(gmail_otp, "_connect", lambda: fake)

    with pytest.raises(OtpNotFoundError, match="No login-code email"):
        fetch_latest_code(requested_after_ts=time.time(), max_wait_seconds=3, poll_interval_seconds=1)


# ---------- sabotage tests ----------

def test_sabotage_wrong_code_would_be_caught(monkeypatch):
    now = time.time()
    raw = _make_raw_email(now, "Your code is 999999")
    fake = FakeImap(messages={b"1": raw}, matching_ids=[b"1"])
    monkeypatch.setattr(gmail_otp, "_connect", lambda: fake)

    code = fetch_latest_code(requested_after_ts=now - 1, max_wait_seconds=5, poll_interval_seconds=1)
    with pytest.raises(AssertionError):
        assert code == "123456"  # deliberately wrong
    assert code == "999999"  # confirm actual correct behavior


def test_sabotage_stale_check_removed_would_be_caught(monkeypatch):
    # prove the "ignore older than requested_after_ts" test isn't
    # trivially passing: if we set requested_after far enough in the
    # past that the old message qualifies, it SHOULD be found
    now = time.time()
    raw = _make_raw_email(now - 3600, "Your code is 111111")
    fake = FakeImap(messages={b"1": raw}, matching_ids=[b"1"])
    monkeypatch.setattr(gmail_otp, "_connect", lambda: fake)

    code = fetch_latest_code(requested_after_ts=now - 7200, max_wait_seconds=5, poll_interval_seconds=1)
    assert code == "111111"


# ---------- extract_code: context-aware patterns (audit fix) ----------

def test_extract_code_prefers_a_number_presented_as_a_code():
    # the real risk: a bare "any 4-8 digit number" match could pick up
    # an order number or a year from an unrelated email
    body = "Order 99887766 confirmed in 2026. Your Refunnel login code is 483920."
    assert gmail_otp.extract_code(body) == "483920"


def test_extract_code_handles_code_first_phrasing():
    body = "483920 is your verification code."
    assert gmail_otp.extract_code(body) == "483920"


def test_extract_code_handles_colon_phrasing():
    body = "Login code: 774411"
    assert gmail_otp.extract_code(body) == "774411"


def test_extract_code_falls_back_to_generic_pattern():
    # an unanticipated format still works rather than failing outright
    body = "Here it is -- 556677 -- use it soon."
    assert gmail_otp.extract_code(body) == "556677"


def test_extract_code_returns_none_when_nothing_matches():
    assert gmail_otp.extract_code("no numbers here at all") is None


def test_sabotage_context_pattern_ignored_would_be_caught():
    body = "Order 99887766 confirmed. Your login code is 483920."
    result = gmail_otp.extract_code(body)
    with pytest.raises(AssertionError):
        assert result == "99887766"  # wrong -- that's the order number
    assert result == "483920"  # confirms actual correct behavior


# ---------- poll loop resilience (audit fix) ----------

def test_transient_imap_failure_does_not_abort_the_whole_wait(monkeypatch):
    now = time.time()
    raw = _make_raw_email(now, "Your Refunnel login code is 112233.")
    good = FakeImap(messages={b"1": raw}, matching_ids=[b"1"])
    calls = {"count": 0}

    def flaky_connect():
        calls["count"] += 1
        if calls["count"] == 1:
            raise ConnectionResetError("connection reset by peer")
        return good

    monkeypatch.setattr(gmail_otp, "_connect", flaky_connect)
    code = fetch_latest_code(requested_after_ts=now - 1, max_wait_seconds=30, poll_interval_seconds=1)
    assert code == "112233"  # recovered on the retry instead of dying
    assert calls["count"] == 2


def test_bad_search_status_is_retried_not_fatal(monkeypatch):
    now = time.time()
    raw = _make_raw_email(now, "Your Refunnel login code is 445566.")

    class FlakySearchImap(FakeImap):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.search_calls = 0

        def search(self, charset, criteria):
            self.search_calls += 1
            if self.search_calls == 1:
                return "NO", [b""]
            return super().search(charset, criteria)

    fake = FlakySearchImap(messages={b"1": raw}, matching_ids=[b"1"])
    monkeypatch.setattr(gmail_otp, "_connect", lambda: fake)
    code = fetch_latest_code(requested_after_ts=now - 1, max_wait_seconds=30, poll_interval_seconds=1)
    assert code == "445566"


def test_unreadable_code_is_still_a_hard_error_not_retried_forever(monkeypatch):
    # found the right email but genuinely can't read a code out of it --
    # a real config problem that retrying would only hide
    now = time.time()
    raw = _make_raw_email(now, "Your login link is ready, no numeric code here.")
    fake = FakeImap(messages={b"1": raw}, matching_ids=[b"1"])
    monkeypatch.setattr(gmail_otp, "_connect", lambda: fake)

    with pytest.raises(OtpNotFoundError, match="couldn't extract"):
        fetch_latest_code(requested_after_ts=now - 1, max_wait_seconds=30, poll_interval_seconds=1)


def test_imap_is_logged_out_even_when_a_transient_failure_happens(monkeypatch):
    now = time.time()
    raw = _make_raw_email(now, "Your Refunnel login code is 778899.")

    class FailingSearchImap(FakeImap):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.search_calls = 0

        def search(self, charset, criteria):
            self.search_calls += 1
            if self.search_calls == 1:
                raise RuntimeError("transient blip")
            return super().search(charset, criteria)

    fake = FailingSearchImap(messages={b"1": raw}, matching_ids=[b"1"])
    monkeypatch.setattr(gmail_otp, "_connect", lambda: fake)
    fetch_latest_code(requested_after_ts=now - 1, max_wait_seconds=30, poll_interval_seconds=1)
    assert fake.logged_out is True  # no leaked connection on the failing cycle

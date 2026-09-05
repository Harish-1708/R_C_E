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

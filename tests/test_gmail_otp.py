"""
Tests for gmail_otp.py, using a fake Gmail API client. These test the
polling/matching/extraction logic in isolation -- they do NOT touch a
real Gmail account (no network access to Google's APIs in this sandbox).
_get_gmail_service is monkeypatched out entirely.

Manual smoke test still required once real OAuth credentials exist --
see README "Testing the Gmail OTP fallback".
"""

import base64
import time

import pytest

import gmail_otp
from gmail_otp import fetch_latest_code, OtpNotFoundError


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode()


class FakeMessagesResource:
    def __init__(self, messages_by_id, list_result):
        self._messages_by_id = messages_by_id
        self._list_result = list_result

    def list(self, userId, q, maxResults):  # noqa: N803 (matches real Gmail API arg names)
        return _Exec(self._list_result)

    def get(self, userId, id, format):  # noqa: A002, N803
        return _Exec(self._messages_by_id[id])


class _Exec:
    def __init__(self, value):
        self._value = value

    def execute(self):
        return self._value


class FakeGmailService:
    def __init__(self, messages_by_id, list_result):
        self._messages = FakeMessagesResource(messages_by_id, list_result)

    def users(self):
        return self

    def messages(self):
        return self._messages


def _make_message(msg_id, internal_ts, body_text):
    return {
        "id": msg_id,
        "internalDate": str(int(internal_ts * 1000)),
        "payload": {
            "mimeType": "text/plain",
            "body": {"data": _b64(body_text)},
        },
    }


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    # keep tests fast -- don't actually wait poll_interval_seconds
    monkeypatch.setattr(time, "sleep", lambda _: None)


def test_extracts_code_from_matching_recent_email(monkeypatch):
    now = time.time()
    msg = _make_message("m1", now, "Your Refunnel login code is 483920. It expires in 10 minutes.")
    service = FakeGmailService(
        messages_by_id={"m1": msg},
        list_result={"messages": [{"id": "m1"}]},
    )
    monkeypatch.setattr(gmail_otp, "_get_gmail_service", lambda: service)

    code = fetch_latest_code(requested_after_ts=now - 1, max_wait_seconds=5, poll_interval_seconds=1)
    assert code == "483920"


def test_ignores_email_older_than_requested_after(monkeypatch):
    now = time.time()
    old_msg = _make_message("old", now - 3600, "Your code is 111111")  # 1hr old -- stale
    service = FakeGmailService(
        messages_by_id={"old": old_msg},
        list_result={"messages": [{"id": "old"}]},
    )
    monkeypatch.setattr(gmail_otp, "_get_gmail_service", lambda: service)

    with pytest.raises(OtpNotFoundError):
        fetch_latest_code(requested_after_ts=now, max_wait_seconds=2, poll_interval_seconds=1)


def test_picks_the_newest_of_multiple_candidates(monkeypatch):
    now = time.time()
    msg_a = _make_message("a", now - 2, "Your code is 111111")
    msg_b = _make_message("b", now, "Your code is 222222")  # newer -- should win
    service = FakeGmailService(
        messages_by_id={"a": msg_a, "b": msg_b},
        list_result={"messages": [{"id": "a"}, {"id": "b"}]},
    )
    monkeypatch.setattr(gmail_otp, "_get_gmail_service", lambda: service)

    code = fetch_latest_code(requested_after_ts=now - 5, max_wait_seconds=5, poll_interval_seconds=1)
    assert code == "222222"


def test_matching_email_with_no_extractable_code_raises_distinct_error(monkeypatch):
    now = time.time()
    msg = _make_message("m1", now, "Welcome to Refunnel! No code here.")
    service = FakeGmailService(
        messages_by_id={"m1": msg},
        list_result={"messages": [{"id": "m1"}]},
    )
    monkeypatch.setattr(gmail_otp, "_get_gmail_service", lambda: service)

    with pytest.raises(OtpNotFoundError, match="couldn't extract a code"):
        fetch_latest_code(requested_after_ts=now - 1, max_wait_seconds=5, poll_interval_seconds=1)


def test_no_messages_at_all_raises_after_timeout(monkeypatch):
    service = FakeGmailService(messages_by_id={}, list_result={"messages": []})
    monkeypatch.setattr(gmail_otp, "_get_gmail_service", lambda: service)

    with pytest.raises(OtpNotFoundError, match="No login-code email"):
        fetch_latest_code(requested_after_ts=time.time(), max_wait_seconds=3, poll_interval_seconds=1)


# ---------- sabotage tests ----------

def test_sabotage_wrong_code_would_be_caught(monkeypatch):
    now = time.time()
    msg = _make_message("m1", now, "Your code is 999999")
    service = FakeGmailService(
        messages_by_id={"m1": msg},
        list_result={"messages": [{"id": "m1"}]},
    )
    monkeypatch.setattr(gmail_otp, "_get_gmail_service", lambda: service)
    code = fetch_latest_code(requested_after_ts=now - 1, max_wait_seconds=5, poll_interval_seconds=1)
    with pytest.raises(AssertionError):
        assert code == "123456"  # deliberately wrong
    assert code == "999999"  # confirm actual correct behavior


def test_sabotage_stale_check_removed_would_be_caught(monkeypatch):
    # prove the "ignore older than requested_after_ts" test isn't
    # trivially passing: if we set requested_after far enough in the
    # past that the old message qualifies, it SHOULD be found
    now = time.time()
    old_msg = _make_message("old", now - 3600, "Your code is 111111")
    service = FakeGmailService(
        messages_by_id={"old": old_msg},
        list_result={"messages": [{"id": "old"}]},
    )
    monkeypatch.setattr(gmail_otp, "_get_gmail_service", lambda: service)

    code = fetch_latest_code(requested_after_ts=now - 7200, max_wait_seconds=5, poll_interval_seconds=1)
    assert code == "111111"

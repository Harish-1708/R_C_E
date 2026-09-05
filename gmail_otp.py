"""
gmail_otp.py

Fetches the latest Refunnel login code from Gmail, for the fallback path
only (saved-session-expired case). Uses plain IMAP with a Gmail **App
Password** -- not the full OAuth client-id/secret/refresh-token flow.

Setup (one-time, done by a human, not this script):
    1. Turn on 2-Step Verification on the Gmail account, if it isn't
       already on (App Passwords require it).
    2. Go to https://myaccount.google.com/apppasswords, create one for
       "Mail" / "Other (custom name)" -- Google gives you a 16-character
       password immediately, no consent screen, no project setup.
    3. Store the Gmail address as GMAIL_ADDRESS and that 16-character
       password as GMAIL_APP_PASSWORD (GitHub Actions secrets). This
       script reads them from environment variables of the same names.

That's the entire setup -- no Google Cloud Console project, no OAuth
client, no refresh token to manage.

This module is NOT exercised against a live Gmail account in this
sandbox (no network access to Gmail's IMAP server here). It should be
smoke-tested manually once a real App Password exists -- see README
"Testing the Gmail OTP fallback".
"""

from __future__ import annotations

import email
import email.utils
import imaplib
import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Optional


DEFAULT_SENDER_QUERY = os.environ.get("REFUNNEL_OTP_SENDER_QUERY", "refunnel.com")
# Most OTP emails use a 4-8 digit numeric code. Adjust if Refunnel's
# format differs (e.g. alphanumeric).
DEFAULT_CODE_PATTERN = os.environ.get("REFUNNEL_OTP_CODE_PATTERN", r"\b(\d{4,8})\b")

IMAP_HOST = "imap.gmail.com"


class OtpNotFoundError(RuntimeError):
    pass


def _connect() -> imaplib.IMAP4_SSL:
    """Log into Gmail over IMAP with an App Password. Separated out as
    its own function so tests can monkeypatch it with a fake connection
    instead of touching a real mailbox."""
    imap = imaplib.IMAP4_SSL(IMAP_HOST)
    imap.login(os.environ["GMAIL_ADDRESS"], os.environ["GMAIL_APP_PASSWORD"])
    imap.select("INBOX")
    return imap


def _extract_body_text(message: email.message.Message) -> str:
    """Walk a (possibly multipart) email and concatenate any text/plain
    or text/html parts we find."""
    texts = []
    if message.is_multipart():
        parts = message.walk()
    else:
        parts = [message]

    for part in parts:
        content_type = part.get_content_type()
        if content_type in ("text/plain", "text/html"):
            payload = part.get_payload(decode=True)
            if payload is None:
                continue
            charset = part.get_content_charset() or "utf-8"
            texts.append(payload.decode(charset, errors="ignore"))

    return "\n".join(texts)


def _message_timestamp(message: email.message.Message) -> float:
    date_header = message.get("Date")
    if not date_header:
        return 0.0
    parsed = email.utils.parsedate_tz(date_header)
    if parsed is None:
        return 0.0
    return email.utils.mktime_tz(parsed)


def fetch_latest_code(
    sender_query: str = DEFAULT_SENDER_QUERY,
    code_pattern: str = DEFAULT_CODE_PATTERN,
    requested_after_ts: Optional[float] = None,
    max_wait_seconds: int = 60,
    poll_interval_seconds: int = 5,
) -> str:
    """Poll Gmail (via IMAP) for the most recent message from
    `sender_query`, sent after `requested_after_ts` (defaults to "now"
    if not given), and extract a code matching `code_pattern`.

    Raises OtpNotFoundError if nothing turns up within max_wait_seconds
    -- the caller should treat that as a hard failure, not retry
    forever, since silently looping risks masking a real problem (wrong
    sender query, Refunnel changed their email format, etc).
    """
    if requested_after_ts is None:
        requested_after_ts = time.time()

    deadline = time.time() + max_wait_seconds
    # IMAP's SINCE is day-granularity only, so search broadly (since
    # yesterday) and filter precisely by the message's own Date header.
    since_date = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%d-%b-%Y")
    search_criteria = f'(FROM "{sender_query}" SINCE {since_date})'

    while time.time() < deadline:
        imap = _connect()
        try:
            status, data = imap.search(None, search_criteria)
            if status != "OK":
                raise OtpNotFoundError(f"IMAP search failed: {status}")

            msg_ids = data[0].split() if data and data[0] else []
            candidates = []
            for msg_id in msg_ids[-10:]:  # only need the most recent handful
                status, msg_data = imap.fetch(msg_id, "(RFC822)")
                if status != "OK" or not msg_data or not msg_data[0]:
                    continue
                raw_bytes = msg_data[0][1]
                message = email.message_from_bytes(raw_bytes)
                ts = _message_timestamp(message)
                if ts >= requested_after_ts - 5:  # 5s slack for clock skew
                    candidates.append((ts, message))

            if candidates:
                candidates.sort(key=lambda pair: pair[0], reverse=True)
                _, newest = candidates[0]
                body = _extract_body_text(newest)
                match = re.search(code_pattern, body)
                if match:
                    return match.group(1)
                raise OtpNotFoundError(
                    "Found a matching email from Refunnel but couldn't extract a code from it "
                    "with the current pattern. Check the real email body and adjust "
                    "REFUNNEL_OTP_CODE_PATTERN."
                )
        finally:
            try:
                imap.logout()
            except Exception:
                pass

        time.sleep(poll_interval_seconds)

    raise OtpNotFoundError(
        f"No login-code email matching '{sender_query}' arrived within {max_wait_seconds}s."
    )

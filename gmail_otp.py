"""
gmail_otp.py

Fetches the latest Refunnel login code from Gmail, for the fallback path
only (saved-session-expired case). Uses the Gmail API with OAuth
credentials -- NOT full account access via IMAP/password, and NOT the
same credential path as sending mail in outreach.py.

Setup (one-time, done by a human, not this script):
    1. In Google Cloud Console, enable the Gmail API on the project
       already used for email-outreach-automation (or a new one).
    2. Create OAuth 2.0 credentials (Desktop app type is easiest for a
       one-time consent flow).
    3. Run the standard Google OAuth "installed app" flow ONCE locally
       to grant `gmail.readonly` scope and obtain a refresh token.
    4. Store client_id, client_secret, and refresh_token as GitHub
       Actions secrets (GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET,
       GMAIL_REFRESH_TOKEN). This script reads them from environment
       variables of the same names.

This module is NOT exercised against a live Gmail account in this
sandbox (no network access to Google's APIs here). It should be
smoke-tested manually once real OAuth credentials exist -- see
README "Testing the Gmail OTP fallback".
"""

from __future__ import annotations

import base64
import os
import re
import time
from typing import Optional

# google-auth / google-api-python-client are the standard libraries for
# this; add to requirements.txt:
#   google-auth
#   google-auth-oauthlib
#   google-api-python-client
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build


# CONFIG -- adjust these once you've seen a real Refunnel login-code
# email. I have not seen one, so these are reasonable defaults, not
# confirmed values.
DEFAULT_SENDER_QUERY = os.environ.get("REFUNNEL_OTP_SENDER_QUERY", "from:refunnel.com")
# Most OTP emails use a 4-8 digit numeric code. Adjust if Refunnel's
# format differs (e.g. alphanumeric).
DEFAULT_CODE_PATTERN = os.environ.get("REFUNNEL_OTP_CODE_PATTERN", r"\b(\d{4,8})\b")


class OtpNotFoundError(RuntimeError):
    pass


def _get_gmail_service():
    creds = Credentials(
        token=None,
        refresh_token=os.environ["GMAIL_REFRESH_TOKEN"],
        client_id=os.environ["GMAIL_CLIENT_ID"],
        client_secret=os.environ["GMAIL_CLIENT_SECRET"],
        token_uri="https://oauth2.googleapis.com/token",
        scopes=["https://www.googleapis.com/auth/gmail.readonly"],
    )
    return build("gmail", "v1", credentials=creds)


def _extract_body_text(message: dict) -> str:
    """Gmail messages can be multipart with nested parts; walk them and
    concatenate any text/plain or text/html bodies we find."""
    def walk(part):
        texts = []
        body_data = part.get("body", {}).get("data")
        mime_type = part.get("mimeType", "")
        if body_data and mime_type in ("text/plain", "text/html"):
            decoded = base64.urlsafe_b64decode(body_data + "==").decode("utf-8", errors="ignore")
            texts.append(decoded)
        for sub in part.get("parts", []) or []:
            texts.extend(walk(sub))
        return texts

    payload = message.get("payload", {})
    return "\n".join(walk(payload))


def fetch_latest_code(
    sender_query: str = DEFAULT_SENDER_QUERY,
    code_pattern: str = DEFAULT_CODE_PATTERN,
    requested_after_ts: Optional[float] = None,
    max_wait_seconds: int = 60,
    poll_interval_seconds: int = 5,
) -> str:
    """Poll Gmail for the most recent message matching `sender_query`,
    sent after `requested_after_ts` (defaults to "now" if not given),
    and extract a code matching `code_pattern`.

    Raises OtpNotFoundError if nothing turns up within max_wait_seconds
    -- the caller should treat that as a hard failure, not retry
    forever, since silently looping risks masking a real problem
    (wrong sender query, Refunnel changed their email format, etc).
    """
    if requested_after_ts is None:
        requested_after_ts = time.time()

    service = _get_gmail_service()
    deadline = time.time() + max_wait_seconds

    # Gmail search's "newer_than" is coarse (units of days/hours), so we
    # search broadly (last 15 minutes) and then double-check the actual
    # internalDate against requested_after_ts ourselves for precision.
    query = f"{sender_query} newer_than:15m"

    while time.time() < deadline:
        results = service.users().messages().list(userId="me", q=query, maxResults=5).execute()
        messages = results.get("messages", [])

        candidates = []
        for msg_meta in messages:
            msg = service.users().messages().get(userId="me", id=msg_meta["id"], format="full").execute()
            internal_ts = int(msg.get("internalDate", "0")) / 1000.0
            if internal_ts >= requested_after_ts - 5:  # 5s slack for clock skew
                candidates.append((internal_ts, msg))

        if candidates:
            candidates.sort(key=lambda pair: pair[0], reverse=True)
            _, newest = candidates[0]
            body = _extract_body_text(newest)
            match = re.search(code_pattern, body)
            if match:
                return match.group(1)
            # found a matching email but couldn't extract a code -- this
            # is worth surfacing distinctly, since it likely means
            # code_pattern is wrong rather than "no email yet"
            raise OtpNotFoundError(
                "Found a matching email from Refunnel but couldn't extract a code from it "
                "with the current pattern. Check the real email body and adjust "
                "REFUNNEL_OTP_CODE_PATTERN."
            )

        time.sleep(poll_interval_seconds)

    raise OtpNotFoundError(
        f"No login-code email matching '{sender_query}' arrived within {max_wait_seconds}s."
    )

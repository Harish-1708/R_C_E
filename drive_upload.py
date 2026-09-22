"""
drive_upload.py

Thin wrapper around the Google Drive API v3, using the SAME service
account already used for Sheets access -- no new credential type.

CONFIRMED REAL, replaces an earlier design entirely: Refunnel has its
own native "Upload to Google Drive" feature (already connected from
your own account, confirmed from real screenshots), and it does the
actual file transfer server-side. This module no longer uploads
anything itself -- its only job now is to find the file Refunnel just
created and rename it to the agreed convention, since Refunnel's own
upload doesn't offer naming control (confirmed real example:
"INSTAGRAM_REEL_tayloredforthekingdom_2026-09-21-UGC_30937735.mp4").

Requires the broader "drive" scope, not the narrower "drive.file" an
earlier version used -- confirmed real, necessary reason: drive.file
only lets the service account see files IT created; finding a file
REFUNNEL created (even in a folder the service account has been added
as an editor on) needs the wider scope.
"""
from __future__ import annotations

import time
from typing import Optional

from google.oauth2 import service_account
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/drive"]


def build_drive_service(service_account_path: str):
    """One Drive API client, built once per script run and reused for
    every lookup -- building it is the "slow" part (reads the key
    file, does token setup); each individual call is cheap once this
    exists.
    """
    creds = service_account.Credentials.from_service_account_file(service_account_path, scopes=SCOPES)
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def find_and_rename_uploaded_file(
    drive_service,
    folder_id: str,
    match_fragment: str,
    new_filename: str,
    uploaded_after: Optional[str] = None,
    max_wait_seconds: float = 30.0,
    poll_interval_seconds: float = 2.0,
) -> Optional[str]:
    """Waits for Refunnel's native upload to finish (server-side, not
    instant the moment "Save to Drive" is clicked) and renames the
    resulting file to new_filename.

    match_fragment identifies the file among everything else in the
    folder -- pass parse_refunnel.drive_match_fragment(media_id),
    confirmed real against an actual upload (the last 8 digits of the
    id's numeric part, which is what Refunnel's own filenames embed).

    uploaded_after (an RFC 3339 timestamp, e.g. from
    datetime.now(timezone.utc).isoformat()) scopes the search to files
    created at or after that moment, so an older file that happens to
    share the same fragment -- extremely unlikely at just 8 digits,
    but not impossible at scale -- is never matched by accident.

    Polls up to max_wait_seconds rather than checking once, since the
    file genuinely may not exist yet the instant the button is
    clicked. Returns the Drive file id once renamed, or None if
    nothing matching showed up within the wait -- callers should treat
    that as "not confirmed yet", leaving the Master Data row unmarked
    for a retry on a future run, not as a permanent failure.
    """
    query = f"'{folder_id}' in parents and name contains '{match_fragment}' and trashed = false"
    if uploaded_after:
        query += f" and createdTime >= '{uploaded_after}'"

    deadline = time.time() + max_wait_seconds
    while True:
        results = drive_service.files().list(
            q=query, fields="files(id, name, createdTime)",
            orderBy="createdTime desc", pageSize=5,
        ).execute()
        files = results.get("files", [])
        if files:
            newest = files[0]
            drive_service.files().update(fileId=newest["id"], body={"name": new_filename}).execute()
            return newest["id"]
        if time.time() >= deadline:
            return None
        time.sleep(poll_interval_seconds)

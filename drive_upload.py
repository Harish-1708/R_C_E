"""
drive_upload.py

Thin wrapper around the Google Drive API v3, using the SAME service
account already used for Sheets access -- no new credential type.

CONFIRMED REAL, direct decision from live evidence: this project spent
a long stretch on Refunnel's own native "Save to Drive" feature
instead of uploading directly. Every piece of that click-through was
eventually proven correct -- confirmed byte-identical to a version
that once worked, confirmed against the right Drive folder, confirmed
accepted by Refunnel's own "Uploading to Google Drive -- it will
appear shortly" toast -- and the file still never landed, across many
separate runs, on a transfer that happens entirely on Refunnel's own
servers once that toast appears. Nothing past that point is something
this codebase can see or control. This module goes back to uploading
the file itself: download the real video from Refunnel
(refunnel_export.download_approved_video), then upload it here with
the correct name set immediately -- no separate rename step needed at
all, since the filename is ours from the start, not something applied
after the fact to a file Refunnel named on its own.

Requires the broader "drive" scope, not the narrower "drive.file" an
earlier version used -- confirmed real, necessary reason: checking
whether THIS media_id was already uploaded by an earlier, since-
crashed run (find_existing_upload, below) needs to see files already
in the shared folder, not only ones this exact process session
created.
"""
from __future__ import annotations

from typing import Optional

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

SCOPES = ["https://www.googleapis.com/auth/drive"]


def build_drive_service(service_account_path: str):
    """One Drive API client, built once per script run and reused for
    every upload/lookup -- building it is the "slow" part (reads the
    key file, does token setup); each individual call is cheap once
    this exists.
    """
    creds = service_account.Credentials.from_service_account_file(service_account_path, scopes=SCOPES)
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def find_existing_upload(drive_service, folder_id: str, match_fragment: str) -> Optional[dict]:
    """A single, immediate check (no polling, no time filter) for a
    file already in Drive matching this media_id. Returns {"id",
    "name"} if one exists, else None.

    CONFIRMED REAL gap this covers: since the final filename already
    embeds media_id (see parse_refunnel.build_drive_filename), passing
    media_id itself as match_fragment catches a file left behind by an
    earlier run that uploaded successfully but crashed before marking
    drive_uploaded_at in the sheet -- checked BEFORE ever downloading
    or uploading again, so that scenario re-marks the sheet instead of
    creating a genuine duplicate file.
    """
    query = f"'{folder_id}' in parents and name contains '{match_fragment}' and trashed = false"
    results = drive_service.files().list(
        q=query, fields="files(id, name, createdTime)",
        orderBy="createdTime desc", pageSize=5,
    ).execute()
    files = results.get("files", [])
    return files[0] if files else None


def upload_file(drive_service, local_path: str, filename: str, folder_id: str) -> str:
    """Uploads local_path into folder_id under the exact name
    `filename` (not the local file's own name -- media_id-based temp
    filenames from the download step are never what should show up in
    Drive). Returns the new file's Drive file ID.

    resumable=True: these are video files, not small documents --
    matters for anything above a few MB, and costs nothing for small
    ones.
    """
    file_metadata = {"name": filename, "parents": [folder_id]}
    media = MediaFileUpload(local_path, resumable=True)
    result = drive_service.files().create(body=file_metadata, media_body=media, fields="id").execute()
    return result["id"]

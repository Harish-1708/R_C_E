"""
Thin wrapper around the Google Drive API v3, using the SAME service
account already used for Sheets access.

Supports both My Drive and Shared Drives.

Requires the broader "drive" scope because the script needs to check
existing files in the destination folder before uploading.
"""

from __future__ import annotations

from typing import Optional

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload


SCOPES = [
    "https://www.googleapis.com/auth/drive"
]


def build_drive_service(service_account_path: str):
    """
    Build one Google Drive API client for the entire run.
    """
    creds = service_account.Credentials.from_service_account_file(
        service_account_path,
        scopes=SCOPES,
    )

    return build(
        "drive",
        "v3",
        credentials=creds,
        cache_discovery=False,
    )


def find_existing_upload(
    drive_service,
    folder_id: str,
    match_fragment: str,
) -> Optional[dict]:
    """
    Check whether a file matching the media_id already exists
    inside the destination folder.

    Explicitly supports Shared Drives.
    """

    query = (
        f"'{folder_id}' in parents "
        f"and name contains '{match_fragment}' "
        f"and trashed = false"
    )

    results = drive_service.files().list(
        q=query,
        fields="files(id, name, createdTime)",
        orderBy="createdTime desc",
        pageSize=5,

        # REQUIRED FOR SHARED DRIVES
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,

    ).execute()

    files = results.get("files", [])

    return files[0] if files else None


def upload_file(
    drive_service,
    local_path: str,
    filename: str,
    folder_id: str,
) -> str:
    """
    Upload a video into the specified Drive folder.

    Explicitly supports Shared Drives.
    """

    file_metadata = {
        "name": filename,
        "parents": [folder_id],
    }

    media = MediaFileUpload(
        local_path,
        resumable=True,
    )

    result = drive_service.files().create(
        body=file_metadata,
        media_body=media,
        fields="id",

        # REQUIRED FOR SHARED DRIVE UPLOADS
        supportsAllDrives=True,

    ).execute()

    return result["id"]

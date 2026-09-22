"""
drive_upload.py

Thin wrapper around the Google Drive API v3, using the SAME service
account already used for Sheets access -- no new credential type or
secret format, just the Drive API enabled on the same GCP project (you
confirmed this is done) and the target Drive folder shared with that
service account's email (the same way each brand's Google Sheet is
shared with it).

Deliberately a thin, mockable layer: build_drive_service() and
upload_file() are the only two functions here, both simple enough to
fake in tests without touching a real Drive account.
"""
from __future__ import annotations

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# drive.file (not the broader "drive" scope): the service account can
# only see/manage files IT created, not everything in the shared
# folder or the wider Drive. Narrower blast radius for a credential
# that lives in a public repo's CI, at no cost to this feature -- every
# file this script touches is one it just uploaded itself.
SCOPES = ["https://www.googleapis.com/auth/drive.file"]


def build_drive_service(service_account_path: str):
    """One Drive API client, built once per script run and reused for
    every upload -- building it is the "slow" part (reads the key file,
    does token setup); each individual upload_file() call is cheap
    once this exists.
    """
    creds = service_account.Credentials.from_service_account_file(service_account_path, scopes=SCOPES)
    return build("drive", "v3", credentials=creds, cache_discovery=False)


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

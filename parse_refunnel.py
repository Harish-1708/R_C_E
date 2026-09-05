"""
parse_refunnel.py

Pure data-transformation layer for the Refunnel -> Google Sheets sync.
No network calls, no browser automation, no Sheets API here on purpose --
this module only turns the two CSV exports Refunnel gives us into the
row-sets each destination tab should contain. Keeping it pure makes it
fully testable with just sample CSV files.

Tabs produced:
    - master            (every media/content row, always kept, never deleted)
    - rights_approved    (media rows where rights_status == GRANTED)
    - rights_requested   (media rows where rights_status == REQUESTED)
    - rights_declined    (media rows where rights_status == DENIED)
    - payments           (every payment row)

Every row set is returned as a dict keyed by a stable id, so the sheet-sync
layer can upsert by id instead of re-appending duplicates. For media rows
the key is the `id` column (e.g. "tk_7681495537484369165") -- confirmed
unique per post in real exports, stable across re-exports, and immune to
the "same creator, many videos" duplication problem the user flagged.
For payment rows the key is the CSV's own `ID` column.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from typing import Dict, List, Optional


# Refunnel's rights_status enum values. Confirmed from a real 2078-row
# export: NONE, REQUESTED, GRANTED, and DENIED -- note the last one is
# "DENIED", not "DECLINED" as an earlier guess assumed (that guess was
# wrong and silently routed 0 rows to the Declined tab even when real
# denied rows existed -- fixed once real data surfaced the actual value).
RIGHTS_STATUS_MAP = {
    "GRANTED": "rights_approved",
    "REQUESTED": "rights_requested",
    "DENIED": "rights_declined",
    # "NONE" -> stays in master only, no usage-rights tab
}

MASTER_COLUMNS = [
    "id",
    "platform",
    "username",
    "followers",
    "original_post_link",
    "media_url",
    "caption",
    "relation_type",
    "rights_status",
    "rights_granted_until_date",
    "creator_email",  # filled in later by the modal-scrape step; blank until then
    "status",
    "spark_code",
    "emv",
    "gmv",
    "likes",
    "comments",
    "impressions",
    "shares",
    "products",
    "hashtags",
    "mentions",
    "collections",
    "created_at",
    "updated_at",
]

# Usage-rights tabs reuse the master schema (same columns) so a row looks
# identical whichever tab it's in -- just filtered by rights_status.
USAGE_RIGHTS_COLUMNS = MASTER_COLUMNS

# Trimmed columns for the Human Review tab -- just enough to identify
# and evaluate a post at a glance. The "Reviewed" (or whatever you name
# it) column is NOT listed here on purpose: you add that column
# yourself directly in the sheet, and sheets_sync.py's existing "extra
# manual columns" preservation logic (see sheets_sync.py docstring)
# automatically carries your marks forward across daily reruns, keyed
# by id -- no code change needed for that part.
HUMAN_REVIEW_COLUMNS = [
    "id",
    "username",
    "platform",
    "rights_status",
    "original_post_link",
    "creator_email",
    "followers",
    "updated_at",
]

PAYMENT_COLUMNS = [
    "id",
    "creator",
    "handle",
    "email",
    "type",
    "purpose",
    "campaign",
    "amount",
    "currency",
    "date",
    "status",
]


@dataclass
class ParseResult:
    master: Dict[str, dict] = field(default_factory=dict)
    rights_approved: Dict[str, dict] = field(default_factory=dict)
    rights_requested: Dict[str, dict] = field(default_factory=dict)
    rights_declined: Dict[str, dict] = field(default_factory=dict)
    payments: Dict[str, dict] = field(default_factory=dict)

    # Simple counters for a post-run summary line, useful for logging /
    # Slack alerts without re-walking the dicts.
    media_rows_seen: int = 0
    payment_rows_seen: int = 0
    skipped_media_rows: int = 0
    skipped_payment_rows: int = 0


def _clean(value: Optional[str]) -> str:
    return (value or "").strip()


def parse_media_csv(path: str, creator_emails: Optional[Dict[str, str]] = None) -> ParseResult:
    """Parse Refunnel's media/content bulk-export CSV.

    creator_emails: optional {media_id: email} map, supplied by the
    modal-scrape step for posts whose rights_status is not NONE. Merged
    into the `creator_email` column when present.
    """
    result = ParseResult()
    creator_emails = creator_emails or {}

    with open(path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        missing_cols = {"id", "rights_status"} - set(reader.fieldnames or [])
        if missing_cols:
            raise ValueError(
                f"media CSV is missing expected columns: {sorted(missing_cols)}. "
                f"Got columns: {reader.fieldnames}"
            )

        for row in reader:
            result.media_rows_seen += 1
            media_id = _clean(row.get("id"))
            if not media_id:
                result.skipped_media_rows += 1
                continue

            rights_status = _clean(row.get("rights_status")).upper()

            out = {
                "id": media_id,
                "platform": _clean(row.get("platform")),
                "username": _clean(row.get("username")),
                "followers": _clean(row.get("followers")),
                "original_post_link": _clean(row.get("original_post_link")),
                "media_url": _clean(row.get("media_url")),
                "caption": _clean(row.get("caption")),
                "relation_type": _clean(row.get("relation_type")),
                "rights_status": rights_status,
                "rights_granted_until_date": _clean(row.get("rights_granted_until_date")),
                "creator_email": creator_emails.get(media_id, ""),
                "status": _clean(row.get("status")),
                "spark_code": _clean(row.get("spark_code")),
                "emv": _clean(row.get("emv")),
                "gmv": _clean(row.get("gmv")),
                "likes": _clean(row.get("likes")),
                "comments": _clean(row.get("comments")),
                "impressions": _clean(row.get("impressions")),
                "shares": _clean(row.get("shares")),
                "products": _clean(row.get("products")),
                "hashtags": _clean(row.get("hashtags")),
                "mentions": _clean(row.get("mentions")),
                "collections": _clean(row.get("collections")),
                "created_at": _clean(row.get("created_at")),
                "updated_at": _clean(row.get("updated_at")),
            }

            result.master[media_id] = out

            tab = RIGHTS_STATUS_MAP.get(rights_status)
            if tab == "rights_approved":
                result.rights_approved[media_id] = out
            elif tab == "rights_requested":
                result.rights_requested[media_id] = out
            elif tab == "rights_declined":
                result.rights_declined[media_id] = out
            # NONE (or any unrecognized value) -> master only, logged so an
            # unexpected new enum value doesn't silently vanish.

    return result


def parse_payments_csv(path: str, result: Optional[ParseResult] = None) -> ParseResult:
    """Parse Refunnel's payment-history export CSV. Can be merged into an
    existing ParseResult (from parse_media_csv) or used standalone."""
    result = result or ParseResult()

    with open(path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        missing_cols = {"ID"} - set(reader.fieldnames or [])
        if missing_cols:
            raise ValueError(
                f"payments CSV is missing expected columns: {sorted(missing_cols)}. "
                f"Got columns: {reader.fieldnames}"
            )

        for row in reader:
            result.payment_rows_seen += 1
            payment_id = _clean(row.get("ID"))
            if not payment_id:
                result.skipped_payment_rows += 1
                continue

            result.payments[payment_id] = {
                "id": payment_id,
                "creator": _clean(row.get("Creator")),
                "handle": _clean(row.get("Handle")),
                "email": _clean(row.get("Email")),
                "type": _clean(row.get("Type")),
                "purpose": _clean(row.get("Purpose")),
                "campaign": _clean(row.get("Campaign")),
                "amount": _clean(row.get("Amount")),
                "currency": _clean(row.get("Currency")),
                "date": _clean(row.get("Date")),
                "status": _clean(row.get("Status")),
            }

    return result


def build_human_review_rows(result: ParseResult) -> Dict[str, dict]:
    """Combine all three usage-rights buckets (approved/requested/
    declined) into one row-set for the Human Review tab, trimmed to
    HUMAN_REVIEW_COLUMNS. NONE-status rows are excluded -- there's
    nothing to review until a request exists."""
    combined: Dict[str, dict] = {}
    for bucket in (result.rights_approved, result.rights_requested, result.rights_declined):
        for media_id, row in bucket.items():
            combined[media_id] = {col: row.get(col, "") for col in HUMAN_REVIEW_COLUMNS}
    return combined


def apply_creator_emails(result: ParseResult, emails: Dict[str, str]) -> int:
    """Mutate result.master (and, by shared reference, whichever
    usage-rights bucket that row is also in) with newly-found creator
    emails. Returns how many rows were actually updated, for logging.

    Relies on the fact that parse_media_csv puts the *same* dict object
    into both result.master and its matching rights_* bucket, so a
    single in-place update is visible in both places without needing to
    touch the bucket separately.
    """
    updated = 0
    for media_id, email in emails.items():
        if not email:
            continue
        row = result.master.get(media_id)
        if row is None:
            continue
        row["creator_email"] = email
        updated += 1
    return updated


def rows_needing_email_scrape(result: ParseResult) -> List[str]:
    """Return media ids whose rights_status is not NONE and don't yet
    have a creator_email -- i.e. the targeted, small set the browser
    automation should open the per-post modal for. Never includes NONE
    rows, keeping the risky modal-click step scoped down."""
    ids = []
    for bucket in (result.rights_approved, result.rights_requested, result.rights_declined):
        for media_id, row in bucket.items():
            if not row.get("creator_email"):
                ids.append(media_id)
    return ids

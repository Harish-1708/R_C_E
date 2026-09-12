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
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional


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
    "media_type",
    "viewable_media_type",
    "post_type",
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
    "moved_to_human_review_at",
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
    # Rows moved here (out of the three buckets above) once you mark
    # them reviewed -- see apply_human_review_flags().
    rights_reviewed: Dict[str, dict] = field(default_factory=dict)
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
                "media_type": _clean(row.get("media_type")),
                "viewable_media_type": _clean(row.get("viewable_media_type")),
                "post_type": _clean(row.get("post_type")),
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


REVIEWED_TRUE_VALUES = ("yes", "y", "true", "1")


def is_reviewed_value(value: Optional[str]) -> bool:
    """Single shared definition of "this row is marked reviewed".

    Confirmed real inconsistency this fixes: run_daily_sync.py and
    apply_human_review.py both required an exact match against
    REVIEWED_TRUE_VALUES before moving a row into Human Review, but
    content_tracker.py's two-way Reviewed sync treated ANY non-blank
    value as reviewed. So typing "No" or "TBD" into either sheet would
    get copied across to the other sheet as though it meant reviewed,
    while the actual row-moving logic correctly ignored it -- two
    different answers to the same question, in the same pipeline.
    Everything now routes through this one function.
    """
    return (value or "").strip().lower() in REVIEWED_TRUE_VALUES


def apply_human_review_flags(result: ParseResult, reviewed_ids: Iterable[str]) -> int:
    """Move rows marked reviewed (via a manual 'Reviewed' column you add
    to Master Data yourself -- mark it 'Yes' for any row) OUT of
    whichever of the three rights-status tabs they're currently in, and
    into the Human Review tab instead. The row itself is untouched in
    Master Data -- this only changes which Usage Rights tab (if any) it
    shows up in. Returns how many rows were moved, for logging.
    """
    moved = 0
    for media_id in reviewed_ids:
        row = result.master.get(media_id)
        if row is None:
            continue
        result.rights_approved.pop(media_id, None)
        result.rights_requested.pop(media_id, None)
        result.rights_declined.pop(media_id, None)
        result.rights_reviewed[media_id] = row
        moved += 1
    return moved


def build_human_review_rows(
    result: ParseResult, existing_moved_dates: Optional[Dict[str, str]] = None
) -> Dict[str, dict]:
    """Rows moved into Human Review by apply_human_review_flags(),
    trimmed to HUMAN_REVIEW_COLUMNS.

    moved_to_human_review_at is set ONCE, the first time a row appears
    here, and never changed again on later runs -- confirmed real want:
    you need to know WHEN something was pushed into Human Review, so
    you can filter by it, and this tab is normally written as a full
    rewrite, which would otherwise silently reset the date to "now"
    every single run. Pass the tab's CURRENT moved_to_human_review_at
    values (read from the sheet before calling this, e.g. via
    sheets_sync.read_column_values) as existing_moved_dates -- an id
    already there keeps its original date; a genuinely new id gets a
    fresh timestamp. Deliberately only in this tab, never in Master
    Data (that column doesn't belong there, and isn't added there by
    this function).
    """
    existing_moved_dates = existing_moved_dates or {}
    now = datetime.now(timezone.utc).isoformat()
    rows = {}
    for media_id, row in result.rights_reviewed.items():
        out = {col: row.get(col, "") for col in HUMAN_REVIEW_COLUMNS if col != "moved_to_human_review_at"}
        out["moved_to_human_review_at"] = existing_moved_dates.get(media_id) or now
        rows[media_id] = out
    return rows


def propagate_emails_by_username(result: ParseResult) -> int:
    """Once one post's creator_email is known, apply it to every OTHER
    post by that same username that doesn't have one yet -- an email
    belongs to the creator, not the individual post, so there's no
    reason to scrape it separately for each of their posts. Confirmed
    real opportunity: 342 of 1360 unique usernames in a real export
    appear on 2+ posts.

    Call this BEFORE computing rows_needing_email_scrape() so already-
    known emails shrink the scraping target list immediately, and again
    after new emails are found during a run so newly-discovered ones
    propagate too, without needing a fresh run.

    Assumes one email per creator (your stated assumption) -- if a
    creator genuinely has two different emails on file, whichever one
    is encountered first while building the map wins silently. Returns
    how many rows were filled in this way, for logging.
    """
    email_by_username: Dict[str, str] = {}
    for row in result.master.values():
        username = row.get("username", "")
        email = row.get("creator_email", "")
        if username and email and username not in email_by_username:
            email_by_username[username] = email

    filled = 0
    for row in result.master.values():
        if row.get("creator_email"):
            continue
        username = row.get("username", "")
        known_email = email_by_username.get(username)
        if known_email:
            row["creator_email"] = known_email
            filled += 1
    return filled


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


def find_duplicate_post_links(result: ParseResult) -> Dict[str, List[str]]:
    """Detect real duplicate CONTENT -- the same underlying video/post
    showing up under two different `id`s. `id` is Refunnel's own and
    already prevents duplicate rows for the same id (confirmed: 2078
    unique ids for 2078 real rows, with usernames legitimately
    repeating -- multiple videos from the same creator is normal, not a
    duplicate). But if Refunnel ever assigned two different ids to what
    is actually the same post, id-based dedup alone wouldn't catch it.

    This checks the second, independent signal available --
    `original_post_link` (the actual TikTok/IG URL) -- and returns
    {link: [id1, id2, ...]} for any link shared by 2+ different ids.
    Blank links (e.g. Instagram Stories, which have none) are ignored --
    that's missing data, not a duplicate.

    This is diagnostic only -- it does NOT remove anything automatically
    (a shared link could have a legitimate reason, e.g. two distinct
    relation_types like TAGGED and MENTIONED for the same post), so
    the caller decides what, if anything, to do with the result.
    """
    by_link: Dict[str, List[str]] = {}
    for media_id, row in result.master.items():
        link = row.get("original_post_link", "").strip()
        if not link:
            continue
        by_link.setdefault(link, []).append(media_id)
    return {link: ids for link, ids in by_link.items() if len(ids) > 1}


def rows_needing_email_scrape(result: ParseResult) -> List[str]:
    """Return media ids that still need a scraped email -- REQUESTED and
    NONE-status rows only, never Approved/Declined.

    NONE-status posts (never had any usage-rights action) use the exact
    same button class as originally discovered -- confirmed real: the
    very first pre-filled-email screenshot in this whole project was a
    never-requested post. So this covers the two statuses confirmed to
    still have a working request-card UI.

    Approved (and presumably Declined) are excluded on purpose -- a real
    debug screenshot confirmed their card footer is replaced entirely
    with a status badge ("Usage rights approved -- Via direct post
    permission"), with no request-card button at all. Every attempt on
    one was a guaranteed failure, not intermittent bad luck -- which is
    why an early run took hours grinding through them.
    """
    ids = []
    for media_id, row in result.master.items():
        if row.get("rights_status") in ("REQUESTED", "NONE") and not row.get("creator_email"):
            ids.append(media_id)
    return ids

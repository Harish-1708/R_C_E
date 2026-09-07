"""Core logic for the Content Tracker -- a separate, cross-brand sheet
derived from each brand's own Master Data, NOT part of the Refunnel
export pipeline itself. See build_content_tracker.py for the
orchestration (Google Sheets I/O) that uses these pure functions.

Column policy, as agreed:
  - FREEZE_ONCE_SET_COLUMNS: computed fresh only when a row is first
    created; never recomputed on later runs, even if the source data in
    Master Data has since changed. Protects against silently altering
    something you may have manually corrected (e.g. Product).
  - REFRESH_COLUMNS: re-read from Master Data on every run, since these
    genuinely change over time as other automation progresses. BUT a
    blank value from Master Data never erases a non-blank value already
    in the tracker -- confirmed real need: you want to be able to type
    an email in directly here before Master Data has it, without a
    later refresh wiping it out.
  - MANUAL_COLUMNS: never touched by any automated write, ever. Blank
    on a brand-new row; whatever you type stays exactly as you left it.
"""
from __future__ import annotations

from typing import Dict, Optional

TRACKER_COLUMNS = [
    "id",  # internal key for matching rows across runs -- not one of
           # your named columns, but required for the freeze/refresh
           # logic to reliably tell "this is the same post as last
           # time" apart from "this is a new post"
    "Brand",
    "Platform",
    "Creator",
    "Creator Email",
    "Product",
    "Sub Category",
    "Usage Rights",
    "Refunnel Link",
    "Video File",
    "Created At",
    "Summary",
    "Product Score",
    "Rights Duration",
    "Ad Ready",
    "Notes",
    "Contact Status",
    "Last Contacted Date",
]

FREEZE_ONCE_SET_COLUMNS = [
    "Brand", "Platform", "Creator", "Product", "Sub Category",
    "Refunnel Link", "Video File", "Created At",
]
REFRESH_COLUMNS = ["Usage Rights", "Creator Email"]
MANUAL_COLUMNS = [
    "Summary", "Product Score", "Rights Duration", "Ad Ready", "Notes",
    "Contact Status", "Last Contacted Date",
]

# Confirmed real: you asked for "Declined", not Master Data's internal
# "DENIED" -- and Title Case display generally, not the all-caps form
# Master Data stores internally.
RIGHTS_STATUS_DISPLAY = {
    "NONE": "None",
    "REQUESTED": "Requested",
    "GRANTED": "Granted",
    "DENIED": "Declined",
}


def derive_product_and_subcategory(brand: str, products_field: str) -> tuple[str, str]:
    """Confirmed real rule, from actual Master Data values for Duderobe:
    if `products` mentions "SheRobe" -> Product = SheRobe; otherwise ->
    DudeRobe. Sub Category = "UFC" if `products` mentions it, else same
    as Product. If `products` is blank, BOTH stay blank -- confirmed:
    you want to fill those in yourself rather than have a default
    guessed for you.

    Only implemented for Duderobe so far -- its product-naming
    convention ("The DudeRobe...", "The SheRobe...", "The UFC
    DudeRobe...") doesn't extend to other brands, which have never had
    real Master Data to build a rule from yet. Other brands get both
    fields blank regardless of `products_field`, for manual fill, until
    a real rule is defined for them too.
    """
    text = (products_field or "").strip()
    if not text:
        return "", ""
    if brand != "Duderobe":
        return "", ""
    lower = text.lower()
    product = "SheRobe" if "sherobe" in lower else "DudeRobe"
    sub_category = "UFC" if "ufc" in lower else product
    return product, sub_category


def _as_creator_handle(username: str) -> str:
    """Adds a leading @ if not already present. Purely additive, as
    agreed -- never edits the username itself, and this ONLY affects
    this tracker's own Creator column, never Master Data's username
    field."""
    username = (username or "").strip()
    if not username or username.startswith("@"):
        return username
    return f"@{username}"


def build_fresh_tracker_row(master_row: Dict[str, str], brand: str) -> Dict[str, str]:
    """Build what a tracker row would look like if computed fresh from
    today's Master Data, with no history considered. Used for a
    brand-new id, and as the "candidate fresh values" input to
    merge_tracker_row() for an id that already exists in the tracker.
    """
    product, sub_category = derive_product_and_subcategory(brand, master_row.get("products", ""))
    return {
        "id": master_row.get("id", ""),
        "Brand": brand,
        "Platform": master_row.get("platform", ""),
        "Creator": _as_creator_handle(master_row.get("username", "")),
        "Creator Email": master_row.get("creator_email", ""),
        "Product": product,
        "Sub Category": sub_category,
        "Usage Rights": RIGHTS_STATUS_DISPLAY.get(master_row.get("rights_status", ""), ""),
        "Refunnel Link": master_row.get("media_url", ""),
        "Video File": master_row.get("original_post_link", ""),
        "Created At": master_row.get("created_at", ""),
        "Summary": "",
        "Product Score": "",
        "Rights Duration": "",
        "Ad Ready": "",
        "Notes": "",
        "Contact Status": "",
        "Last Contacted Date": "",
    }


def merge_tracker_row(existing_row: Optional[Dict[str, str]], fresh_row: Dict[str, str]) -> Dict[str, str]:
    """The core freeze/refresh/manual policy for one row.

    existing_row: what's currently in the tracker for this id, or None
    if this id has never appeared in the tracker before.
    fresh_row: today's freshly computed values (build_fresh_tracker_row).

    Brand new row (existing_row is None): use the fresh values as-is --
    there's nothing to freeze yet, and manual columns start blank.

    Existing row: start from exactly what's already there (freezing
    FREEZE_ONCE_SET_COLUMNS and MANUAL_COLUMNS by construction, since
    they're simply never touched below), then update ONLY
    REFRESH_COLUMNS -- and even then, only when the fresh value is
    non-blank, so a blank Master Data value (or a run where scraping
    hasn't caught up yet) never erases something already sitting in the
    tracker, manually typed or previously scraped.
    """
    if existing_row is None:
        return dict(fresh_row)

    merged = dict(existing_row)
    for col in REFRESH_COLUMNS:
        fresh_value = fresh_row.get(col, "")
        if fresh_value:
            merged[col] = fresh_value
    return merged


def build_tracker_target_rows(
    master_rows: Dict[str, Dict[str, str]],
    brand: str,
    existing_tracker_rows: Dict[str, Dict[str, str]],
) -> Dict[str, Dict[str, str]]:
    """The full per-run build: for every id currently in Master Data,
    compute the row this brand's tracker tab should show, applying the
    freeze/refresh/manual policy against whatever's already there.

    Deliberately does NOT drop an id that's in existing_tracker_rows but
    missing from master_rows here -- that's handled by sync_tab's own
    never_delete=True at write time, so this function only needs to
    handle what's IN today's Master Data.
    """
    target: Dict[str, Dict[str, str]] = {}
    for media_id, master_row in master_rows.items():
        fresh = build_fresh_tracker_row(master_row, brand)
        existing = existing_tracker_rows.get(media_id)
        target[media_id] = merge_tracker_row(existing, fresh)
    return target

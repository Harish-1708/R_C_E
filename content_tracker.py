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

import re
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
    "Content Type",
    "Post Type",
    "Theme",
    "Usage Rights",
    "Refunnel Link",
    "Video File",
    "Created At",
    "Reviewed",
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
    "Content Type", "Post Type", "Theme", "Refunnel Link", "Video File", "Created At",
]
REFRESH_COLUMNS = ["Usage Rights", "Creator Email"]
MANUAL_COLUMNS = [
    "Reviewed", "Summary", "Product Score", "Rights Duration", "Ad Ready", "Notes",
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

# Sourced from viewable_media_type, NOT media_type -- confirmed real:
# Refunnel's own dashboard has two separate filter dimensions, "Post
# type" (Reels/Feed/Story/Carousel for Instagram, Video/Image/Carousel
# for TikTok) and "Content type" (just Video/Image). viewable_media_type
# is the exact match for their "Content type" concept (2 values only:
# VIDEO 2077, IMAGE 1 in a real export) -- media_type has a 3rd value
# (STORY) that doesn't belong here at all, since Refunnel's own UI
# treats Story as a Post Type, not a Content Type. "Post Type" is a
# separate, richer dimension (post_type in the raw CSV) that could be
# its own future tracker column if ever wanted, but that's not what
# this one is.
#
# Refunnel only tracks creator-generated content, never the separate
# official content folder or older archive, so there's no genuine
# "product photo" or studio "lifestyle" category available from this
# data at all. This is deliberately the full extent of Content Type --
# checked captions for more specific style descriptors (unboxing,
# review, haul) and they're too rare (4-16 out of 2078) to be a
# reliable column on their own, so those live in Theme instead, only
# when a caption actually says so.
CONTENT_TYPE_DISPLAY = {
    "VIDEO": "UGC Video",
    "IMAGE": "UGC Photo",
}

# Checked IN ORDER -- first match wins. This is what makes the priority
# rule work: explicit occasion names (Father's Day, etc.) are listed
# BEFORE the generic "Gift-Giving" catch-all, so "father's day" matches
# Father's Day even though the caption also very likely contains
# "gift" -- while "gift for dad" with no explicit occasion falls
# through to Gift-Giving, exactly as you described.
#
# Valentine's Day / Mother's Day / Wedding-Honeymoon / Graduation:
# confirmed 0 matches in the real backlog (English or Spanish) even with
# a broad keyword net, kept here anyway for future content and for your
# year-round campaign planning -- they just won't tag anything in the
# existing backlog today.
#
# Spanish equivalents added for real, evidence-based reasons -- checked
# actual Master Data and found a genuine Spanish-speaking creator
# segment (words like "hombre", "bata", "casa" appear hundreds of
# times) that the original English-only list completely missed.
# Confirmed real counts before adding anything: "regalo/regalos" (36
# rows, -> Gift-Giving), "dia del padre" (2, -> Father's Day), "navidad"
# (1, -> Christmas/Holiday). The rest (cumpleaños, día de la madre, san
# valentín, aniversario, boda) are at 0 today, included for the same
# future-proofing reason as their English 0-count counterparts above --
# not guessed, just not yet observed.
#
# Explicitly checked and REJECTED as a false positive: "summerwins" /
# "summervibes" / "summermusthaves" appear 267 times, but ALWAYS
# bundled together with confirmed platform-promo tags
# (#tiktokshopsummersale, #backtoschoolshopping, #weeklydeals) -- this
# is the same coordinated TikTok Shop campaign-hashtag noise as
# #tiktokshopbacktoschool below, not real content about summer, so it's
# deliberately NOT a theme here despite the high raw count.
#
# "tiktokshop"-prefixed hashtags are stripped before matching (see
# _clean_theme_text) -- confirmed real: #tiktokshopbacktoschool and
# #tiktokshopsummersale appear on totally unrelated robe videos (a
# TikTok Shop platform promotional tag, not real content about summer
# or school), which would otherwise falsely tag hundreds of rows.
THEME_KEYWORDS = [
    ("Valentine's Day", ["valentine", "vday", "san valentin", "san valentín", "sanvalentin"]),
    ("Mother's Day", [
        "mothersday", "mother's day", "giftformom", "for mom",
        "dia de la madre", "día de la madre",
    ]),
    ("Father's Day", [
        "fathersday", "father's day", "dia del padre", "día del padre", "diadelpadre",
    ]),
    ("Wedding/Honeymoon", ["honeymoon", "wedding", "bridal", "groomsmen", "boda"]),
    ("Birthday", ["birthday", "bday", "cumpleaños", "cumpleanos"]),
    ("Graduation", ["graduation", "grad gift"]),
    ("Christmas/Holiday", ["christmas", "xmas", "holiday", "stocking", "navidad"]),
    ("Gift-Giving", ["gift", "present", "regalo", "regalos", "regalar"]),
    ("Self-Care/Cozy", ["selfcare", "self care", "cozy", "relax"]),
    ("Winter/Cold Weather", ["winter", "cold"]),
    ("Athletic/Workout", ["workout", "gym", "ufc", "athletic"]),
    ("Travel/Vacation", ["travel", "vacation", "cruise"]),
    ("Try-On/Haul", ["tryon", "try on", "haul"]),
    ("Unboxing", ["unboxing", "unbox"]),
]


def derive_content_type(viewable_media_type: str) -> str:
    """Maps Master Data's viewable_media_type (NOT media_type -- see
    CONTENT_TYPE_DISPLAY comment above) to a display label. Blank/unknown
    values stay blank rather than guessing."""
    return CONTENT_TYPE_DISPLAY.get((viewable_media_type or "").strip().upper(), "")


# Sourced from Refunnel's own post_type field -- confirmed real,
# cross-referenced against platform in an actual export: TikTok+VIDEO
# (2034), Instagram+REELS (35), Instagram+STORY (5), TikTok+STORY (3),
# PRIVATE+PRIVATE (1, a since-restricted post). This is the separate,
# richer dimension from Refunnel's own "Post Type" filter (distinct
# from "Content Type" -- their own UI treats Story as a Post Type, not
# a Content Type, which is exactly why Content Type is sourced from
# viewable_media_type instead of this field).
POST_TYPE_DISPLAY = {
    "VIDEO": "Video",
    "REELS": "Reels",
    "STORY": "Story",
    "PRIVATE": "Private",
}


def derive_post_type(post_type: str) -> str:
    """Maps Master Data's post_type to a display label. Blank/unknown
    values stay blank rather than guessing."""
    return POST_TYPE_DISPLAY.get((post_type or "").strip().upper(), "")


def _clean_theme_text(caption: str, hashtags: str) -> str:
    text = f"{caption or ''} {hashtags or ''}".lower()
    return re.sub(r"#?tiktokshop\w*", "", text)


def derive_theme(caption: str, hashtags: str) -> str:
    """Checks THEME_KEYWORDS in order, first match wins. Blank if
    nothing matches -- confirmed real: roughly 60% of the real backlog
    won't match any current theme, and that's fine, per your
    instruction to leave it blank rather than force a guess."""
    text = _clean_theme_text(caption, hashtags)
    for theme, keywords in THEME_KEYWORDS:
        if any(kw in text for kw in keywords):
            return theme
    return ""


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
    content_type = derive_content_type(master_row.get("viewable_media_type", ""))
    post_type = derive_post_type(master_row.get("post_type", ""))
    theme = derive_theme(master_row.get("caption", ""), master_row.get("hashtags", ""))
    return {
        "id": master_row.get("id", ""),
        "Brand": brand,
        "Platform": master_row.get("platform", ""),
        "Creator": _as_creator_handle(master_row.get("username", "")),
        "Creator Email": master_row.get("creator_email", ""),
        "Product": product,
        "Sub Category": sub_category,
        "Content Type": content_type,
        "Post Type": post_type,
        "Theme": theme,
        "Usage Rights": RIGHTS_STATUS_DISPLAY.get(master_row.get("rights_status", ""), ""),
        "Refunnel Link": master_row.get("media_url", ""),
        "Video File": master_row.get("original_post_link", ""),
        "Created At": master_row.get("created_at", ""),
        "Reviewed": "",
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

    Existing row: start from exactly what's already there, then update
    ONLY REFRESH_COLUMNS -- and even then, only when the fresh value is
    non-blank, so a blank Master Data value (or a run where scraping
    hasn't caught up yet) never erases something already sitting in the
    tracker, manually typed or previously scraped.

    FREEZE_ONCE_SET_COLUMNS are frozen once they actually HAVE a value
    -- but if one is still blank (either genuinely no source info yet,
    or -- confirmed real bug -- because the column was added to the
    schema AFTER this row already existed in the tracker, so it never
    got a first chance to compute anything), it's allowed to take a
    fresh value now. A real run showed this exactly: adding Content
    Type/Theme to an already-populated tracker left every existing row
    frozen at blank forever, since "existing but blank" and "genuinely
    already computed" were being treated the same way. Once a column
    has any real value, freezing kicks in as normal -- this only
    affects columns that have never actually been set for that row.
    MANUAL_COLUMNS are unaffected either way -- they're not written by
    build_fresh_tracker_row at all, so this loop naturally never touches
    them.
    """
    if existing_row is None:
        return dict(fresh_row)

    merged = dict(existing_row)
    for col in REFRESH_COLUMNS:
        fresh_value = fresh_row.get(col, "")
        if fresh_value:
            merged[col] = fresh_value
    for col in FREEZE_ONCE_SET_COLUMNS:
        if not merged.get(col, ""):
            merged[col] = fresh_row.get(col, "")
    return merged


def merge_reviewed_from_master(target_rows: Dict[str, Dict[str, str]], master_rows: Dict[str, Dict[str, str]]) -> int:
    """The other direction of the Reviewed sync -- confirmed real: a
    real check showed 20 rows marked Reviewed directly in Master Data
    (the original, established mechanism from before this tracker
    existed) versus 0 in the Content Tracker, meaning the ONLY sync
    direction that existed (tracker -> Master Data) had nothing to
    propagate and looked completely broken from your side, even though
    it was working correctly for its one direction.

    Pulls a genuine "Yes" from Master Data into the tracker whenever
    the tracker doesn't already have one, so both sheets end up
    agreeing on what's been reviewed regardless of which one you
    happened to mark it in. Never erases an existing tracker value
    (checked first, skipped if already set), and never pulls in a
    blank from Master Data either -- this only ever ADDS a Reviewed
    marking, in either direction, never removes one.

    Call this BEFORE propagate_reviewed_to_master() in
    build_content_tracker.py, so a value that came FROM Master Data
    doesn't get redundantly written back to it as if it were new.

    Returns how many rows picked up a Reviewed value this way, for
    logging.
    """
    merged = 0
    for media_id, row in target_rows.items():
        if row.get("Reviewed", ""):
            continue
        master_value = master_rows.get(media_id, {}).get("Reviewed", "").strip()
        if master_value:
            row["Reviewed"] = master_value
            merged += 1
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

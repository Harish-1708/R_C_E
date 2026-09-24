"""
refunnel_export.py

The actual "get data out of Refunnel" steps, run against an already
logged-in Playwright page (see refunnel_auth.py for how that page is
obtained).

Three pieces:

1. scroll_to_load_all() -- you told me the bulk-export only captures
   whatever's currently rendered, and the page lazy-loads more as you
   scroll (you saw "80 of 2078 media" until scrolling further). This
   scrolls the page repeatedly, tracking that "X of Y" counter, until
   X == Y (or growth stalls, as a safety net) -- so the export afterward
   actually captures everything.

2. export_payments_csv() -- I'm confident about this one: your
   screenshot shows an explicit "Export" button on the Payments page.

3. export_media_csv() -- I have NOT seen the actual export/bulk-import
   button on the Content page in your screenshots (only "Create
   collection", filter chips, and a "..." menu were visible). This
   function has a placeholder selector and WILL need you to tell me
   (or fix directly) where that control actually is.

4. scrape_creator_email() -- gated behind SCRAPE_EMAILS_ENABLED (default
   False). This opens the same modal that can send a real request email
   to a creator, so it only ever clicks the "Email" channel tab to reveal
   the pre-filled address and then closes via the X -- it must never
   click anything resembling "Send request". Leave this off until you've
   manually verified the selectors match reality; see README "Enabling
   email scraping safely".

Nothing in this file has been run against the live site (no network
access to app.refunnel.com from this sandbox).
"""

from __future__ import annotations

import random
import re
import time
from pathlib import Path
from typing import Callable, Iterable, Optional

from playwright.sync_api import Page

import refunnel_auth


# Turn this on only after you've manually verified scrape_creator_email()
# against the real site -- see module docstring and README.
SCRAPE_EMAILS_ENABLED = True

# Confirmed real, exact text from the live page's own markup
# (.urq-title inside a .usage-rights-requested-card). A card showing
# this is awaiting the brand's own approve/decline decision, and its
# menu has NO usage-rights option at all -- see scrape_creator_emails'
# skip logic for the full confirmed evidence and why the class name
# alone can't be used to detect it.
PENDING_REVIEW_TITLE_TEXT = "Pending review"

# Per-attempt click timeout inside the detached-card retry loop.
#
# CONFIRMED REAL overcorrection this replaces: the previous value (8000)
# was set based on evidence gathered while a SEPARATE bug (the general
# exception handler over-resetting scroll) was still present, which
# meant retries could never succeed regardless of timeout length --
# any value would have looked equally "doomed" at the time. Once that
# bug was fixed (retries do get a genuinely fresh element now), a live
# run showed 3-for-3 clicks still failing, every single one at exactly
# 8s -- consistent with the element genuinely needing MORE time to
# stabilize (virtualized-list re-render settling, image lazy-loading),
# not with it being permanently un-clickable. 8s never gave it the
# chance to find out.
#
# 15s is a middle ground, not a confirmed-correct number -- if clicks
# still fail consistently at this value too, that would be real
# evidence the problem isn't about time at all, and needs a fresh debug
# snapshot from the moment of failure to diagnose properly.
CLICK_ATTEMPT_TIMEOUT_MS = 15000

# Refunnel's "Request usage rights" flow has a "Send request" button
# (confirmed from your screenshot). We refuse to click anything whose
# accessible name matches this, as a hard safety net independent of
# whatever selector logic runs above it.
_DANGEROUS_BUTTON_PATTERN = re.compile(r"send\s*request", re.I)

# Pacing between actions in the email-scraping loop -- purely to avoid
# a bursty, machine-speed click pattern (a reasonable-load courtesy),
# NOT an attempt to evade any bot-detection. Each value is a (min, max)
# range in milliseconds; _pace() picks a random point in that range so
# the interval isn't perfectly uniform. Tune these directly if you want
# it faster/slower -- see README "Pacing and its time cost" for what
# changing them does to total run time.
EMAIL_SCRAPE_ACTION_PACE_MS = (400, 900)      # between clicks within one post's flow
# Between finishing one post and starting the next. CONFIRMED REAL, new
# pattern this addresses: a live 25-post run showed post 1 succeeding
# completely (menu, modal, email field all read), then EVERY post after
# it failing at the very first step -- centering the card. Not random
# flakiness; "works once, then stops." The one thing that genuinely
# changes after a successful post is that a modal (with its own overlay)
# was open and then closed via Escape -- the grid behind it plausibly
# needs to re-settle its layout once that overlay goes away, and
# 600-1200ms may not have been enough room for that. Widened as a
# targeted, evidenced adjustment -- not a guess at an arbitrary bigger
# number -- specifically for the transition this pattern points at.
EMAIL_SCRAPE_ITEM_PACE_MS = (1500, 2500)
SCROLL_SEARCH_PACE_MS = (200, 400)            # between scroll-search steps in _scroll_until_card_found


def _pace(page: Page, ms_range: tuple = EMAIL_SCRAPE_ACTION_PACE_MS) -> None:
    """Wait a randomized amount of time within ms_range. Not a security
    measure -- just avoids machine-speed clicking."""
    page.wait_for_timeout(random.randint(ms_range[0], ms_range[1]))


class ExportError(RuntimeError):
    pass


def select_workspace(
    page: Page,
    workspace_name: str,
    all_known_workspace_names: Iterable[str],
    timeout_ms: int = 15000,
) -> None:
    """Switch Refunnel's active workspace, using the top-left
    workspace switcher (logo + name + chevron -> dropdown with a
    'Search Workspaces' box and a list of workspace names -- confirmed
    from your screenshot of the OPEN dropdown).

    Confirmed from a real failure's HTML dump: the left sidebar can load
    in a COLLAPSED state (`class="left-side-navbar collapsed"`), and in
    that state the workspace name isn't in the DOM at all -- not just
    hidden by CSS. There's a real expand control for it though: an
    `<img alt="Toggle menu">`. So this checks briefly for a known
    workspace name, and if the sidebar looks collapsed, clicks that
    toggle and waits again with the full time budget.

    If workspace_name is already the active one, this is a no-op (skips
    opening the switcher at all).
    """
    all_known = list(all_known_workspace_names)
    combined_css = ", ".join(f":text-is('{name}')" for name in all_known)
    trigger = page.locator(combined_css).first

    try:
        trigger.wait_for(state="visible", timeout=3000)
    except Exception:
        try:
            page.get_by_alt_text("Toggle menu").click()
        except Exception as e:
            raise ExportError(
                "Sidebar looked collapsed (no workspace name found in the page at all) and "
                f"clicking the 'Toggle menu' expand control also failed. Original error: {e}"
            ) from e
        try:
            trigger.wait_for(state="visible", timeout=timeout_ms)
        except Exception as e:
            raise ExportError(
                f"Clicked the sidebar expand toggle but still no known workspace name "
                f"({', '.join(all_known)}) became visible within {timeout_ms}ms. The sidebar "
                f"may use a different expand control than expected, or something else is "
                f"blocking rendering. Original error: {e}"
            ) from e

    active_text = trigger.inner_text().strip()

    if active_text == workspace_name:
        # Might already be on the target workspace -- confirm the dropdown
        # isn't already open before treating this as a no-op.
        search_box_visible = page.get_by_placeholder(re.compile("search workspaces", re.I)).count() > 0
        if not search_box_visible:
            return

    trigger.click()

    try:
        page.get_by_placeholder(re.compile("search workspaces", re.I)).wait_for(
            state="visible", timeout=5000
        )
    except Exception as e:
        raise ExportError(
            f"Clicked what looked like the workspace switcher (was showing '{active_text}') "
            f"but the 'Search Workspaces' dropdown never appeared. Original error: {e}"
        ) from e

    target = page.get_by_text(workspace_name, exact=True).last  # .last to skip the trigger itself
    try:
        target.wait_for(state="visible", timeout=5000)
        target.click()
    except Exception as e:
        raise ExportError(
            f"Workspace switcher opened but couldn't find/click '{workspace_name}' in the list. "
            f"Original error: {e}"
        ) from e

    page.wait_for_timeout(2000)  # let the workspace switch (page reload/content refresh) settle


def goto_social_listening_for_workspace(page: Page, workspace_name: str, known_workspace_names: Iterable[str],
                                       usage_rights: Optional[str] = None) -> None:
    """Navigates to Social Listening for a SPECIFIC workspace, with the
    intended 12-months time range GENUINELY applied afterward -- not
    just requested in the URL before the switch.

    CONFIRMED REAL, SERIOUS BUG this fixes: switching workspaces
    silently RESETS the active time-range filter back to THAT
    workspace's own default/last-used setting, overriding whatever was
    in the URL beforehand. A live run navigated with
    insights_timeline=last12months, then switched to Swoveralls -- and
    a screenshot of the resulting page showed "Last 3 months" in the
    dropdown, not 12. Every script that ever did "goto, then
    select_workspace" was exposed to this for any workspace whose own
    default differs from 12 months (which just Swoveralls being an
    additional brand, not Duderobe specifically, was enough to prove).

    Re-navigates to the SAME URL again, AFTER the workspace switch has
    settled -- this is the one, single place that sequence lives now,
    so every caller (the main sync, campaign sync, the Drive backfill)
    gets the fix by using this instead of the two calls directly.
    """
    page.goto(refunnel_auth.refunnel_social_listening_url(usage_rights))
    select_workspace(page, workspace_name, known_workspace_names)
    # Re-navigate: the workspace switch above can silently reset the
    # time-range filter to THIS workspace's own default -- see the
    # docstring above for the confirmed real evidence.
    page.goto(refunnel_auth.refunnel_social_listening_url(usage_rights))


def scroll_to_top(page: Page, scroll_container_selector: str = "#scrollableDiv") -> None:
    """Resets the scrollable grid back to scrollTop=0.

    CONFIRMED REAL, serious bug this fixes: run_daily_sync.py calls
    scroll_to_load_all() (which scrolls the container all the way to
    its real bottom, to load everything for the CSV export) BEFORE
    scrape_creator_emails() ever runs, on the SAME page instance, with
    nothing in between resetting the scroll position. But
    _scroll_until_card_found() -- what scrape_creator_emails() uses to
    locate each card -- only ever scrolls FORWARD, matching
    _order_ids_for_scraping()'s own documented assumption that ids are
    processed in feed order (newest first) starting from the top.

    Starting from the bottom instead means the newest posts (searched
    for first) can never be reached going forward -- confirmed real
    from an actual failed run against Swoveralls' much larger backlog:
    every single "couldn't locate" failure reported the EXACT SAME
    scrollTop, matching the container's real bottom exactly, for
    nearly 2000 consecutive different ids in a row. The underlying CSV
    export itself was complete (confirmed: 5873 rows, a full year) --
    only the scraper's own separate, forward-only search was affected.
    Duderobe's much smaller backlog never surfaced this, since its
    virtualized list likely still kept early cards close enough to
    stay reachable even scrolled to the bottom; Swoveralls' far larger
    one does not.

    Called once, right before scraping starts on the SAME page
    instance the export just used -- a restart's fresh page.goto()
    naturally resets scroll position on its own and doesn't need this.
    """
    page.evaluate(
        f"""() => {{
            const el = document.querySelector({scroll_container_selector!r});
            if (el) el.scrollTop = 0;
        }}"""
    )
    page.wait_for_timeout(500)  # let the virtualized list settle back to the top


def scroll_to_load_all(
    page: Page,
    count_text_pattern: str = r"(\d+)\s+of\s+(\d+)\s+media",
    max_rounds: int = 400,
    scroll_pause_ms: int = 3000,
    idle_rounds_before_giving_up: int = 20,
    scroll_container_selector: str = "#scrollableDiv",
) -> None:
    """Scroll until the page's own '<loaded> of <total> media' counter
    (visible in your screenshot) shows loaded == total, or growth stalls
    for idle_rounds_before_giving_up consecutive scrolls in a row.

    Defaults widened twice now, both times for the same reason:
      - 1200ms/6 rounds -> 2000ms/12 rounds when the "Last 12 months"
        filter increased the total from ~2065 to 2771 and a run
        stalled at 120/2771. The failure screenshot showed a
        perfectly healthy, normally-loading page.
      - 2000ms/12 rounds -> 3000ms/20 rounds when the total grew
        further to 5890 and a run stalled at 2620/5890 -- CONFIRMED
        REAL, same signature exactly: a healthy-looking screenshot,
        real varied content, the counter genuinely stuck. The dataset
        just keeps growing over time; this isn't proven to be the
        whole story, so if a run still stalls early even with these
        wider defaults, that's real evidence pointing at something
        else (e.g. a genuine selector/DOM change) rather than "needs
        more patience" again.

    Scrolls `scroll_container_selector` directly via JS (setting its
    scrollTop), confirmed from a real HTML dump to be `#scrollableDiv`
    -- the actual react-infinite-scroll-component container, separate
    from the virtuoso grid used just for rendering. An earlier version
    used `page.mouse.wheel()` at the mouse's default position, which
    wasn't necessarily over this container at all -- confirmed by a
    real run that only loaded 20 of 2078 items before giving up.

    Raises ExportError if it can't find the counter text at all --
    likely means the page structure changed and count_text_pattern needs
    updating, not that scrolling itself failed.
    """
    pattern = re.compile(count_text_pattern, re.I)

    def read_counts() -> Optional[tuple]:
        text = page.inner_text("body")
        match = pattern.search(text)
        if not match:
            return None
        return int(match.group(1)), int(match.group(2))

    # Retries with real waits before concluding the counter is genuinely
    # missing -- confirmed real: a scheduled run failed here, but you
    # confirmed the exact same URL loads fine when you check it
    # yourself, meaning this was very likely just the page not having
    # finished rendering yet on that particular run (the 12-months pull
    # is a much bigger initial load than before), not an actual
    # structural change. A single, immediate, zero-retry check couldn't
    # tell those two situations apart -- this can.
    first_read = None
    for _ in range(8):
        first_read = read_counts()
        if first_read is not None:
            break
        page.wait_for_timeout(2000)
    if first_read is None:
        raise ExportError(
            f"Couldn't find a '<n> of <total> media' counter on the page using pattern "
            f"{count_text_pattern!r}, even after retrying for ~16s. The page structure may "
            f"have changed -- update count_text_pattern in scroll_to_load_all() -- or this "
            f"specific run hit a genuinely slow/failed page load beyond just needing more "
            f"time. Check the saved failure screenshot/HTML to tell which."
        )

    idle_rounds = 0
    last_loaded = first_read[0]

    for _ in range(max_rounds):
        loaded, total = read_counts() or (last_loaded, first_read[1])
        if loaded >= total:
            return

        # CONFIRMED REAL gap this closes: setting scrollTop to the SAME
        # value it already holds (which happens whenever scrollHeight
        # hasn't grown since the last round) produces no actual
        # position change -- no guarantee the underlying infinite-
        # scroll listener treats that as a new "reached the bottom"
        # event rather than a no-op. Nudging up first guarantees a
        # real, measurable scroll delta every single round, whether or
        # not scrollHeight has changed, before scrolling back to the
        # (possibly still the same) bottom.
        page.evaluate(
            "(sel) => { const el = document.querySelector(sel); "
            "if (el) { el.scrollTop = Math.max(0, el.scrollTop - 300); } }",
            scroll_container_selector,
        )
        page.wait_for_timeout(150)
        page.evaluate(
            "(sel) => { const el = document.querySelector(sel); "
            "if (el) { el.scrollTop = el.scrollHeight; } }",
            scroll_container_selector,
        )
        page.wait_for_timeout(scroll_pause_ms)

        new_loaded, _ = read_counts() or (loaded, total)
        if new_loaded <= last_loaded:
            idle_rounds += 1
            if idle_rounds >= idle_rounds_before_giving_up:
                # Raising here on purpose, not warning-and-continuing --
                # a real run silently proceeded with 820/2078 rows once,
                # and since every downstream tab is a FULL REWRITE, that
                # would have overwritten and destroyed ~1250 real rows
                # already correctly in the sheet. An incomplete pull
                # must abort the whole run, not produce a short CSV that
                # looks successful. If this fires legitimately (e.g. a
                # real content count dropped), the fix is to adjust
                # idle_rounds_before_giving_up or investigate why growth
                # actually stalled -- not to catch and ignore this.
                raise ExportError(
                    f"scroll_to_load_all stopped early at {new_loaded}/{total} after "
                    f"{idle_rounds} rounds with no growth. Aborting rather than "
                    f"continuing with a partial pull -- every sheet tab is a full "
                    f"rewrite, so exporting/syncing this data would delete real rows "
                    f"that a complete pull would have kept."
                )
        else:
            idle_rounds = 0
        last_loaded = new_loaded


def export_payments_csv(page: Page, download_dir: str) -> str:
    """Click the Payments page's Export button and save the resulting
    CSV. Confirmed from a real screenshot: an 'Export' button with a
    dropdown chevron opens two choices, 'Export as CSV' and 'Export as
    Excel' -- no confirmation modal like the media export has, so this
    should download directly once the CSV option is clicked."""
    Path(download_dir).mkdir(parents=True, exist_ok=True)

    export_button = page.get_by_role("button", name=re.compile(r"^Export", re.I)).first

    with page.expect_download(timeout=30000) as download_info:
        export_button.click()
        # If it's a dropdown (chevron suggests format choice), a CSV
        # option likely appears -- click it if present. If the first
        # click already triggered the download directly, this just
        # times out quietly and the download above still resolves.
        try:
            csv_option = page.get_by_text(re.compile(r"\bCSV\b", re.I)).first
            csv_option.wait_for(state="visible", timeout=3000)
            csv_option.click()
        except Exception:
            pass
    download = download_info.value

    out_path = str(Path(download_dir) / f"payment-history-{int(time.time())}.csv")
    download.save_as(out_path)
    return out_path


def export_media_csv(page: Page, download_dir: str, scroll_container_selector: str = "#scrollableDiv") -> str:
    """Export the media/content CSV via Social Listening -> All Brand
    Mentions -> the '...' menu next to 'Create collection' -> 'Export
    Content CSV' -> a confirmation modal -> 'Download to device'
    (confirmed from real screenshots; the menu also has 'Save from a
    UGC link', 'Save a local file', and 'Bulk upload media', which are
    unrelated upload actions, not this export; the modal's other option,
    'Export to email', is async/emails a link later -- we want the
    immediate one instead, and its own text confirms it only includes
    "content currently loaded on the page", which is exactly why
    scroll_to_load_all() runs first).

    The '...' button is `<div class="upload-content-activator"><img
    src=".../dottedMenuIconBlack....svg"></div>` -- confirmed unique
    (exactly one match) from a real HTML dump. (An earlier version
    located it by screen position relative to 'Create collection',
    which turned out to be wrong -- it matched the unrelated 'Sort by'
    button instead, since layout-based matching doesn't require being
    on the same row.)

    Confirmed from two real HTML dumps taken before/after scrolling: the
    header containing this button is a STICKY header that hides itself
    (`content-header-section content-header-visible` -> `...-hidden`)
    once you've scrolled down -- e.g. right after scroll_to_load_all()
    loads everything. So this scrolls the container back to the top
    first, to bring the header (and this button) back before looking
    for it.
    """
    Path(download_dir).mkdir(parents=True, exist_ok=True)

    page.evaluate(
        "(sel) => { const el = document.querySelector(sel); if (el) { el.scrollTop = 0; } }",
        scroll_container_selector,
    )
    page.wait_for_timeout(1000)  # let the sticky-header show/hide transition settle

    try:
        more_menu_button = page.locator(".upload-content-activator").first
        more_menu_button.wait_for(state="visible", timeout=8000)
        more_menu_button.click()
    except Exception as e:
        raise ExportError(
            "Couldn't find/click the '...' menu button (.upload-content-activator) next to "
            "'Create collection', even after scrolling back to the top. If this keeps "
            "happening, the sticky-header transition may need more than 1000ms to settle -- "
            f"try increasing that wait. Original error: {e}"
        ) from e

    export_item = page.get_by_text(re.compile(r"Export Content CSV", re.I))
    try:
        export_item.first.wait_for(state="visible", timeout=5000)
    except Exception as e:
        raise ExportError(
            "The '...' menu opened but 'Export Content CSV' wasn't found in it -- "
            f"the menu's wording or structure may have changed. Original error: {e}"
        ) from e
    export_item.first.click()

    # Clicking "Export Content CSV" opens a confirmation modal ("Export
    # data in CSV format") with two options -- "Download to device"
    # (immediate, only what's currently loaded -- what we want, since
    # scroll_to_load_all() already loaded everything) vs "Export to
    # email" (async, emails a link later). Confirmed from a real
    # screenshot of that modal.
    download_option = page.get_by_text(re.compile(r"Download to device", re.I))
    try:
        download_option.first.wait_for(state="visible", timeout=8000)
    except Exception as e:
        raise ExportError(
            "Clicked 'Export Content CSV' but the 'Download to device' confirmation modal "
            f"never appeared. Original error: {e}"
        ) from e

    with page.expect_download(timeout=30000) as download_info:
        download_option.first.click()
    download = download_info.value

    out_path = str(Path(download_dir) / f"media_content-{int(time.time())}.csv")
    download.save_as(out_path)
    return out_path


# Best guess based on a single screenshot of the filter bar (see below
# for the one selector that was WRONG and has since been confirmed
# fixed against real markup) -- the rest are still unverified.
# Centralized here so a real run's failure message points straight at
# what to fix.
#
# CAMPAIGN_FILTER_BUTTON_SELECTOR: CONFIRMED WRONG in a real run, now
# fixed. The initial guess assumed a native <button>; the real element
# is a styled <div class="campaign-filter-label clickable"> with a
# <span class="campaign-text">Campaign</span> inside -- a real
# TimeoutError and the actual failing markup confirmed this. Matches
# the real class first; the old button-based guess stays as a
# comma-separated fallback in case a future redesign goes back to a
# real button, rather than silently losing that possibility.
CAMPAIGN_FILTER_BUTTON_SELECTOR = ".campaign-filter-label.clickable, button:has-text('Campaign')"
# Confirmed real from an actual HTML dump: the visible "Campaign" label
# above has a SIBLING element carrying the ARIA disclosure state --
# `<div tabindex="-1" aria-controls="_r_k_" aria-owns="_r_k_"
# aria-expanded="false"><div></div></div>` -- immediately after
# .campaign-filter-label inside .campaign-filter-container. Its
# aria-controls value is the dropdown panel's real id, regenerated on
# every page load, so it's read at runtime rather than ever hardcoded.
CAMPAIGN_DISCLOSURE_TOGGLE_SELECTOR = ".campaign-filter-container [aria-controls]"
CAMPAIGN_SEARCH_INPUT_SELECTOR = "input[placeholder*='search campaign' i]"
# Confirmed real from the SAME dump, once actually captured with the
# panel genuinely open: each campaign is
# `<div class="campaign-option "><input class="campaign-checkbox"
# type="checkbox"><span title="Exact Campaign Name">Exact Campaign
# Name</span></div>`. The earlier [role='checkbox']/input[type=checkbox]
# guess was wrong not because it was a bad guess about checkboxes in
# general (these genuinely ARE <input type="checkbox">), but because
# scoping it as "#panel_id " + that whole comma-separated string builds
# invalid CSS: a leading #id scope only applies to the FIRST
# comma-branch, leaving the second (input[type='checkbox']) searching
# the entire page again -- the exact page-wide-leak risk this scoping
# was meant to prevent in the first place.
CAMPAIGN_CHECKBOX_ROW_SELECTOR = ".campaign-option"
CAMPAIGN_CHECKBOX_INPUT_SELECTOR = "input.campaign-checkbox"
CAMPAIGN_APPLY_BUTTON_SELECTOR = "button:has-text('Apply Changes')"
CLEAR_ALL_FILTERS_SELECTOR = "text=Clear"


def list_available_campaigns(page: Page, debug_dir: Optional[str] = None, timeout_ms: int = 15000,
                             max_scroll_rounds: int = 40) -> list:
    """Reads the full, current list of campaign names straight from the
    Campaign filter's own checklist -- confirmed real requirement: with
    22 campaigns today and more added over time, a hardcoded list would
    silently miss new ones. This is the single source of truth, read
    fresh every run, never stored in our own code.

    Each campaign name is read from its checkbox row's own text, not
    assumed from a fixed list -- so a campaign being renamed or removed
    on Refunnel's side is reflected automatically too.

    Scoped to the ACTUAL open panel, not a page-wide search -- confirmed
    real from an actual HTML dump: the panel is a React disclosure with
    a DYNAMICALLY GENERATED id (e.g. "_r_k_", different on every page
    load), referenced by aria-controls on a sibling of the visible
    "Campaign" label, and the panel isn't even mounted in the DOM while
    closed. Reading that id at runtime and scoping the search to it
    means this can't accidentally match some OTHER filter's checkboxes
    elsewhere on the page -- confirmed real risk with the previous,
    page-wide CAMPAIGN_CHECKBOX_ROW_SELECTOR search.

    If this finds ZERO campaigns and debug_dir is given, it saves a
    screenshot + page HTML BEFORE closing the dropdown -- confirmed
    real bug in an earlier version of this function: the diagnostic was
    captured AFTER the Escape key had already closed the panel, so the
    very evidence meant to show what's actually there showed nothing
    useful at all (aria-expanded="false", panel not even in the DOM).
    Capturing before closing is what makes this diagnostic worth having.
    """
    filter_container = page.locator(CAMPAIGN_FILTER_BUTTON_SELECTOR).first
    filter_container.click()

    # The panel's real id is read at RUNTIME from the toggle sibling's
    # aria-controls -- never hardcoded, since it's regenerated on every
    # page load and a stale id from a previous run would silently match
    # nothing.
    toggle = page.locator(CAMPAIGN_DISCLOSURE_TOGGLE_SELECTOR).first
    panel_id = None
    try:
        toggle.wait_for(state="attached", timeout=timeout_ms)
        panel_id = toggle.get_attribute("aria-controls")
    except Exception:
        pass

    rows = page.locator(f"#{panel_id} {CAMPAIGN_CHECKBOX_ROW_SELECTOR}") if panel_id else page.locator(CAMPAIGN_CHECKBOX_ROW_SELECTOR)
    try:
        rows.first.wait_for(state="visible", timeout=timeout_ms)
    except Exception:
        pass  # fall through to the empty-result handling below, which
        # captures a diagnostic either way (timed out waiting, or found
        # something that just yielded no usable text) -- both mean the
        # selector doesn't match what's really on the page.
    # Scroll WITHIN the dropdown until no new campaigns appear --
    # confirmed real: Swoveralls has 22+ campaigns but a single read
    # found only the first 10, because the list lazy-loads more as it
    # is scrolled. Scrolling the LAST currently-rendered row into view
    # forces whichever ancestor actually scrolls to load the next batch,
    # without having to guess that container's selector.
    #
    # Names are collected in first-seen order and de-duplicated, since
    # the same rows stay rendered across rounds. Stops after two rounds
    # in a row with nothing new -- one quiet round can just be the list
    # still rendering.
    names: list = []
    seen: set = set()
    quiet_rounds = 0
    for _ in range(max_scroll_rounds):
        count = rows.count()
        before = len(names)
        for i in range(count):
            try:
                text = rows.nth(i).inner_text().strip()
            except Exception:
                continue  # a row recycled mid-read; the next round catches it
            if text and text not in seen:
                seen.add(text)
                names.append(text)

        if len(names) == before:
            quiet_rounds += 1
            if quiet_rounds >= 2:
                break
        else:
            quiet_rounds = 0

        if count == 0:
            break
        try:
            rows.nth(count - 1).scroll_into_view_if_needed(timeout=2000)
        except Exception:
            break  # nothing further to scroll to
        page.wait_for_timeout(400)  # let the next batch render

    if not names and debug_dir:
        # Captured BEFORE the Escape below, while the panel (if it
        # opened at all) is still actually in the DOM -- see the
        # docstring above for why the previous ordering made this
        # diagnostic useless.
        try:
            out_dir = Path(debug_dir)
            out_dir.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(out_dir / "campaign_discovery_empty.png"), full_page=True)
            (out_dir / "campaign_discovery_empty.html").write_text(page.content(), encoding="utf-8")
            print(f"Found 0 campaigns -- saved a debug snapshot to {out_dir} for diagnosis "
                  f"(panel_id read as {panel_id!r}; CAMPAIGN_CHECKBOX_ROW_SELECTOR in "
                  f"refunnel_export.py likely still needs updating).")
        except Exception as e:
            print(f"Found 0 campaigns, and couldn't save a debug snapshot either: {e}")

    # Closes the dropdown without applying anything -- this call is
    # read-only by design, it must never change the active filter.
    # Deliberately LAST, after any diagnostic capture above.
    page.keyboard.press("Escape")

    return names


def filter_is_genuinely_active(page: Page, campaign_name: str) -> bool:
    """True if the applied campaign filter is ACTUALLY showing on the
    page for this exact campaign -- confirmed real, serious bug this
    guards against: a live run reported 2795 posts for "SheRobe
    Content Campaign" (the FULL, unfiltered library size) while a
    manual check of the same campaign showed genuinely zero results.
    The filter silently failed to take effect that one time, and
    nothing caught it -- scroll_to_load_all() and export_media_csv()
    happily proceeded against the UNFILTERED view, which would have
    mistagged all 2795 posts as belonging to a campaign they were
    never in.

    Checks that campaign_name appears in the active-filter chip text
    (confirmed real from a screenshot: "Campaign | is | <name> | X"),
    by checking the page's own visible text rather than guessing at
    the chip's exact CSS structure -- a real caption mentioning a
    marketing campaign's exact name by coincidence is a low enough
    risk to accept, especially against the alternative of trusting an
    unverified filter and silently exporting the wrong data.
    """
    try:
        return campaign_name in page.inner_text("body")
    except Exception:
        return False


def filter_by_campaign(page: Page, campaign_name: str, debug_dir: Optional[str] = None, timeout_ms: int = 15000) -> None:
    """Clears any currently active filters, then applies ONLY
    campaign_name. Clearing first (rather than just unchecking the
    previous campaign) also resets any OTHER stray filter that might
    be active, so each campaign's export genuinely reflects "only this
    campaign", not "this campaign plus whatever was left over".

    Selects the EXACT campaign, not a substring match -- confirmed
    real, genuine risk from actual campaign data: "Partner with
    DudeRobe!" and "Partner with DudeRobe" both exist as real,
    distinct campaigns, differing only by trailing punctuation.
    Searching for one and taking the first checkbox that merely
    CONTAINS matching text could silently check the wrong one. Uses
    Playwright's own exact-text matching (get_by_text(..., exact=True))
    rather than string-interpolating campaign_name into a raw CSS
    selector, which also sidesteps any issue if a campaign name ever
    contains a quote or other CSS-special character.

    If debug_dir is given, captures a screenshot + HTML right after
    clicking Apply -- confirmed real need: a live run had every single
    campaign stall scroll_to_load_all at exactly "20 of 2795", the same
    number as the FULL unfiltered library. That's genuinely ambiguous
    from the log alone -- it could mean the filter applied and 20 is a
    real (tiny) match count, or it could mean the filter never actually
    took effect and this is just Refunnel's normal unfiltered
    first-batch view. Deliberately NOT guessed at or "fixed" by making
    the scroll lenient here -- doing that without knowing which
    explanation is true risks silently tagging random unrelated posts
    as belonging to a campaign they're not actually in, which is worse
    than just being missing. This capture is what settles it.
    """
    clear_all_filters(page)

    # CONFIRMED REAL root cause of erratic campaign failures: clicking
    # the Campaign filter is a TOGGLE, not "open". A live run across 10
    # Swoveralls campaigns failed in three different ways -- "Apply
    # Changes" never enabling (30s), the search input never becoming
    # visible (15s), and "<div> intercepts pointer events" -- with
    # successes and failures interleaved. All three are the same
    # underlying problem: the dropdown was ALREADY open, left over from
    # the previous campaign, so the click CLOSED it instead of opening
    # it. Everything after that then operates on a panel that isn't
    # there (no search box), or on a stale one (nothing gets checked,
    # so Apply stays disabled), or against a lingering overlay.
    #
    # Fixed by making "open" deterministic instead of assuming a
    # starting state: force it closed with Escape first, then open it,
    # then VERIFY the search box actually appeared -- retrying the
    # whole open sequence rather than charging ahead into a panel that
    # never opened.
    search_box = None
    for open_attempt in range(3):
        try:
            page.keyboard.press("Escape")  # guarantee closed, whatever state it was in
            page.wait_for_timeout(400)
            page.locator(CAMPAIGN_FILTER_BUTTON_SELECTOR).first.click()
            candidate = page.locator(CAMPAIGN_SEARCH_INPUT_SELECTOR).first
            candidate.wait_for(state="visible", timeout=5000)
            search_box = candidate
            break
        except Exception:
            continue

    if search_box is None:
        raise ExportError(
            f"couldn't get the Campaign filter dropdown open for {campaign_name!r} after 3 "
            f"attempts -- its search box never became visible. Refusing to continue rather "
            f"than acting on a panel that isn't there."
        )

    search_box.fill(campaign_name)
    # Let the campaign list actually filter down to the typed text
    # before matching a row against it -- without this the row we match
    # can be one the list is about to replace.
    page.wait_for_timeout(600)

    row = page.locator(CAMPAIGN_CHECKBOX_ROW_SELECTOR).filter(
        has=page.get_by_text(campaign_name, exact=True)
    ).first
    row.wait_for(state="visible", timeout=timeout_ms)

    # .check() rather than .click() -- confirmed real symptom this
    # addresses: "DudeRobe Content Campaign" left Apply Changes
    # permanently disabled (30s of retries, "element is not enabled"),
    # the same shape of failure as the OTP multi-box issue -- a plain
    # .click() can register visually without firing the change event a
    # React form needs to consider a selection made. .check() is
    # Playwright's own checkbox-specific method, built to verify the
    # box ends up genuinely checked, not just clicked at.
    row.locator(CAMPAIGN_CHECKBOX_INPUT_SELECTOR).check()

    # Wait for Apply to actually become enabled before clicking it.
    # Confirmed real: when the checkbox didn't register, the old code
    # clicked a permanently-disabled Apply and burned the full 30s
    # timeout before failing. Checking the state first turns that into
    # a fast, clearly-worded failure instead of a long silent stall.
    apply_button = page.locator(CAMPAIGN_APPLY_BUTTON_SELECTOR).first
    try:
        apply_button.wait_for(state="visible", timeout=5000)
        page.wait_for_selector(
            f"{CAMPAIGN_APPLY_BUTTON_SELECTOR}:not([disabled])", timeout=5000
        )
    except Exception as e:
        raise ExportError(
            f"the checkbox for {campaign_name!r} doesn't appear to have registered -- "
            f"'Apply Changes' never became enabled. Skipping rather than clicking a "
            f"disabled button for 30s. Original error: {e}"
        ) from e

    apply_button.click(timeout=10000)
    page.wait_for_timeout(800)  # let the filtered view settle before anything reads it

    if debug_dir:
        try:
            out_dir = Path(debug_dir)
            out_dir.mkdir(parents=True, exist_ok=True)
            safe_name = re.sub(r"[^A-Za-z0-9]+", "_", campaign_name)[:60]
            page.wait_for_timeout(1000)  # let the filtered view settle before capturing
            page.screenshot(path=str(out_dir / f"after_apply_{safe_name}.png"), full_page=True)
            (out_dir / f"after_apply_{safe_name}.html").write_text(page.content(), encoding="utf-8")
        except Exception as e:
            print(f"Couldn't save post-Apply debug snapshot for {campaign_name!r}: {e}")


NO_RESULTS_TEXT_PATTERN = "No results for these filter"


def has_no_results_for_filter(page: Page) -> bool:
    """True if Refunnel is showing its own "no matching content" empty
    state -- confirmed real, exact text from a live run's debug
    screenshots: "No results for these filters(s)" (Refunnel's own
    typo -- matched on the stable leading substring so either spelling
    of the trailing "(s)" still counts). Checked BEFORE attempting to
    scroll/export a campaign-filtered view, since a genuinely empty
    result has no "<n> of <total> media" counter at all -- confirmed
    real: without this check, scroll_to_load_all() raised ExportError
    trying to find a counter that will never exist, treating a
    perfectly legitimate "this campaign has 0 posts" answer as a
    failure to retry rather than a real, valid result to record.
    """
    try:
        return NO_RESULTS_TEXT_PATTERN in page.inner_text("body")
    except Exception:
        return False


def clear_all_filters(page: Page) -> None:
    """Resets every active filter on the Content view -- used between
    campaigns so one campaign's export can never accidentally include
    a stray filter left over from the previous one."""
    try:
        page.locator(CLEAR_ALL_FILTERS_SELECTOR).first.click(timeout=3000)
    except Exception:
        pass  # nothing was active to clear -- not an error


def _safe_click(locator, timeout_ms: Optional[int] = None) -> None:
    """Click, but refuse if the element's own text matches
    _DANGEROUS_BUTTON_PATTERN (looks like 'Send request') -- a hard
    safety net independent of whatever locator logic got us here."""
    try:
        text = locator.inner_text(timeout=2000)
    except Exception:
        text = ""
    if _DANGEROUS_BUTTON_PATTERN.search(text or ""):
        raise ExportError(
            f"Refusing to click an element whose text matches a dangerous "
            f"send/submit pattern: {text!r}"
        )
    # timeout_ms=None keeps Playwright's default for existing callers.
    if timeout_ms is None:
        locator.click()
    else:
        locator.click(timeout=timeout_ms)


def _order_ids_for_scraping(media_rows: dict, media_ids: Iterable[str]) -> list:
    """Process ids in the same order they appear in media_rows (CSV/feed
    order), not whatever order media_ids happens to be in. Since we
    scroll forward monotonically to bring virtualized cards into view
    (see scrape_creator_emails), matching the feed's own order means one
    forward pass covers everything instead of scrolling back and forth."""
    wanted = set(media_ids)
    return [mid for mid in media_rows if mid in wanted]


def _scroll_until_card_found(
    page: Page,
    media_id: str,
    scroll_container_selector: str,
    max_rounds: int = 3000,
    scroll_step: int = 800,
    pace_ms_range: tuple = SCROLL_SEARCH_PACE_MS,
    bottom_rounds_before_giving_up: int = 4,
    card_selector: Optional[str] = None,
):
    """react-virtuoso (the grid library this page uses) only keeps
    nearby cards mounted in the DOM, unmounting far-off ones as you
    scroll -- confirmed from a real HTML dump (a real failure snapshot
    had just 10 cards in the entire DOM). So a card matching media_id
    may simply not exist in the DOM yet/anymore. This scrolls
    `scroll_container_selector` forward in small steps until a card
    containing that media_id's thumbnail (matched by image src, which
    embeds the id) appears, or until the container is genuinely at the
    bottom with nothing left to load.

    CONFIRMED REAL BUG this fixes: the old version stopped after a
    FIXED 200 rounds x 800px = 160,000px of scrolling, whatever the
    actual list length was. Real failure diagnostics showed scrollTop
    stalling at 159,951 / 159,933 -- exactly that ceiling -- while the
    container's real scrollHeight was 327,922px. So roughly the bottom
    HALF of the grid was permanently unreachable, and any post living
    there could never be scraped: it failed identically on every run,
    forever, with a misleading "couldn't locate ... after scrolling
    through everything" message. It had never actually scrolled through
    everything.

    Now the stopping condition is the real one -- "we reached the
    bottom and the card still isn't here" -- instead of an arbitrary
    round count that silently became wrong as the backlog grew. The
    bottom must be observed for several consecutive rounds
    (bottom_rounds_before_giving_up) so a lazily-loading grid gets a
    chance to extend scrollHeight before we conclude there's no more.
    max_rounds stays only as a last-resort infinite-loop guard, now set
    far above any realistic list height rather than acting as the
    routine limit.

    Returns (locator_or_none, diagnostics_dict). diagnostics_dict has
    scrollTop/scrollHeight/clientHeight read from the container right
    when this gives up (or None if that read itself failed) -- this
    tells us whether scrolling is actually moving the container at all
    (scrollTop stuck near 0 would mean the scrollTop assignment isn't
    taking effect on this element), separate from "moved fine but the
    card still never rendered."
    """
    # card_selector overrides the default id-in-image match -- used by
    # the username+date fallback for posts whose id never appears in
    # their thumbnail URL (see card_selector_for_username_date).
    selector = card_selector or f"div.rf-virtuoso-item:has(img[src*='{media_id}'])"
    state = None
    rounds_at_bottom = 0

    for _ in range(max_rounds):
        card = page.locator(selector)
        if card.count() > 0:
            return card.first, None

        # Scroll and read the container's geometry in ONE evaluate call
        # rather than two, so checking "are we at the bottom yet?" every
        # round costs no extra round-trip over the old blind scroll.
        state = page.evaluate(
            "(args) => { const el = document.querySelector(args.sel); "
            "if (!el) { return null; } "
            "el.scrollTop += args.step; "
            "return {scrollTop: el.scrollTop, scrollHeight: el.scrollHeight, "
            "clientHeight: el.clientHeight}; }",
            {"sel": scroll_container_selector, "step": scroll_step},
        )
        _pace(page, pace_ms_range)

        if not state:
            break  # container isn't on the page at all -- scrolling can't help

        # 2px of slack: browsers report fractional/rounded scroll values.
        at_bottom = state["scrollTop"] + state["clientHeight"] >= state["scrollHeight"] - 2
        rounds_at_bottom = rounds_at_bottom + 1 if at_bottom else 0
        if rounds_at_bottom >= bottom_rounds_before_giving_up:
            break

    if state is None:
        try:
            state = page.evaluate(
                "(sel) => { const el = document.querySelector(sel); "
                "return el ? {scrollTop: el.scrollTop, scrollHeight: el.scrollHeight, "
                "clientHeight: el.clientHeight} : null; }",
                scroll_container_selector,
            )
        except Exception as e:
            state = {"diagnostic_read_failed": str(e)}

    return None, state


def _is_logged_out(page: Page) -> bool:
    """True if the page has been bounced to Refunnel's login screen.

    Confirmed real: a session can expire MID-RUN. When that happened,
    nothing noticed -- the browser was perfectly alive, so the crash
    detector said "fine", and the scrape loop then failed all 625
    remaining posts one at a time against the login screen ("Container
    state: None", because #scrollableDiv doesn't exist there) before
    the run finally died on the Payments export ~40 minutes later.
    A logged-out page is recoverable in exactly the same way a crashed
    one is -- get a fresh session -- but only if something detects it.
    """
    try:
        return refunnel_auth.is_login_url(page.url)
    except Exception:
        return False


def _save_scrape_failure_snapshot(page: Page, debug_dir: str, media_id: str, category: str = "unknown") -> None:
    """Diagnostic capture for scrape_creator_emails -- a screenshot and
    the raw page HTML.

    category names WHICH kind of failure this is (e.g. "couldnt_locate",
    "empty_field", "exception:TimeoutError") and is folded into the
    filename, so each distinct failure type gets its own capture once
    per run -- not one shared snapshot for the whole run. CONFIRMED
    REAL gap this fixes: a live artifact only ever showed the "no email
    on file" case (correct, not a bug), because it happened to occur
    first and used up the single shared slot -- a genuinely different,
    ongoing issue (detached clicks) never got its own evidence at all.
    """
    try:
        out_dir = Path(debug_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        safe_category = re.sub(r"[^A-Za-z0-9_]+", "_", category)
        page.screenshot(path=str(out_dir / f"scrape_failure_{safe_category}_{media_id}.png"), full_page=True)
        (out_dir / f"scrape_failure_{safe_category}_{media_id}.html").write_text(page.content(), encoding="utf-8")
        print(f"Saved scrape-failure debug snapshot ({category}) for media_id={media_id!r} to {out_dir}")
    except Exception as e:
        print(f"Couldn't save scrape-failure debug snapshot: {e}")


def _should_print_progress(attempted: int, total: int, checkpoint: int) -> bool:
    """True every `checkpoint` items, and once more on the final item --
    pulled out as its own function specifically so this cadence logic is
    unit-testable without needing a real Playwright page."""
    return attempted % checkpoint == 0 or attempted == total


def _is_target_crashed(exception: Exception) -> bool:
    """True if this specific exception means the whole browser target
    has crashed, not just this one post. Confirmed real: a run's log
    showed the loop grinding through ~1070 individually-failing posts,
    each taking real time, after the browser had already crashed --
    because the old circuit breaker was removed, but nothing replaced
    it with a way to notice "the whole page is dead" immediately.
    "Target crashed" is Playwright's own, unambiguous message for
    exactly this -- unlike a generic timeout, which really might just
    be one post's own issue, this is a direct fact, not something that
    needs N repeats to become believable.
    """
    return "crashed" in str(exception).lower()


def _format_progress_line(attempted: int, total: int, found: int, empty_fields: int = 0,
                          pending_review: int = 0) -> str:
    # Broken down by reason -- confirmed real need: a plain "failed"
    # count doesn't tell you WHY, and "empty email field on file" (a
    # real, expected outcome for some posts) looks identical to a
    # genuine error otherwise.
    #
    # pending_review is reported here too, not just in a separate
    # trailing message -- confirmed real: a post awaiting the brand's
    # own approve/decline decision isn't something the scraper CAN
    # read an email from at all, so it isn't a scrape error and
    # shouldn't look like one buried in "other error(s)".
    #
    # CONFIRMED REAL bug this fixed previously: attempted correctly
    # counts pending-review items (each loop iteration increments it
    # exactly once, pending or not), so this subtraction must exclude
    # pending_review too, or every pending-review post gets silently
    # double-counted: once correctly as "Pending review", and again
    # folded into "Errors", even with zero actual exceptions in the
    # run. A user's own complete log proved this directly: 25
    # attempted, 0 found, 23 empty, 2 pending review -- 23 + 2 already
    # accounts for all 25, so the real error count was always zero.
    #
    # Wording matches what the user asked for directly ("N searched -
    # F Found, M Not found, P Pending review, E Errors") over the
    # original phrasing -- same numbers, shorter and less clinical.
    other_failed = attempted - found - empty_fields - pending_review
    pending_note = f", {pending_review} Pending review" if pending_review else ""
    return (f"scrape_creator_emails: {attempted}/{total} searched "
            f"-- {found} Found, {empty_fields} Not found{pending_note}, "
            f"{other_failed} Errors")


# CONFIRMED REAL from a saved page with the Approved filter applied: an
# Approved card's footer holds ONLY the usage-rights toggle -- the ARIA
# disclosure wrapping .usage-rights-approved-card. Upload to Google
# Drive is in that toggle's dropdown. An earlier selector targeted a
# dotted "..." icon instead; on the live page that opened the card's
# other menu (Show content / Mute creator / Delete from library).
#
# The toggle is clicked on the CARD ITSELF (.usage-rights-approved-card,
# see _open_drive_upload_menu), not this wrapper -- the same reversal
# already confirmed for the email-scraping flow: a live run showed
# hundreds of failures clicking the wrapper once the grid was deep into
# a large batch, while clicking the card directly (matched by a
# proven-working prior version of the email-scraping code) held up.
DRIVE_UPLOAD_MENU_ITEM_SELECTOR = "text=Upload to Google Drive"
DRIVE_MODAL_ALL_FOLDERS_TAB_SELECTOR = "button:has-text('All folders')"
DRIVE_MODAL_SAVE_BUTTON_SELECTOR = "button:has-text('Save to Drive')"


_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def card_date_label(created_at: str) -> Optional[str]:
    """Formats an ISO created_at ("2026-09-11T14:02:00Z") the way a card
    displays its date ("Sep 11") -- confirmed real from saved pages:
    "Sep 11", "Jul 19", "Mar 10" (abbreviated month, unpadded day, no
    year). Returns None if created_at can't be parsed."""
    try:
        y, m, d = (created_at or "")[:10].split("-")
        return f"{_MONTHS[int(m) - 1]} {int(d)}"
    except Exception:
        return None


def card_selector_for_username_date(username: str, created_at: str) -> Optional[str]:
    """Selector locating a card by creator handle + displayed post date --
    the fallback for posts whose media id never appears in their
    thumbnail URL.

    CONFIRMED REAL need: TikTok ids and numeric Instagram ids DO appear
    in card image filenames (e.g. "ig_18352380517172428.jpg"), but the
    32-character hex Instagram ids (e.g. ig_0431480af70141eab24c76d9f2b5b40c)
    do not -- which is exactly why those kept failing with "couldn't
    locate". Suggested directly: match on creator + date instead.

    Uses confirmed-real classes (span.post-header-uname for the handle,
    .post_time__cpgc for the date) and :text-is() for EXACT matching,
    so "@indycub9" can never match "@indycub99".
    """
    label = card_date_label(created_at)
    if not username or not label:
        return None
    handle = username if username.startswith("@") else f"@{username}"
    handle = handle.replace("'", "\\'")
    return (f"div.rf-virtuoso-item"
            f":has(span.post-header-uname:text-is('{handle}'))"
            f":has(.post_time__cpgc:text-is('{label}'))")


def trigger_native_drive_upload(
    page: Page,
    media_id: str,
    drive_folder_name: str,
    scroll_container_selector: str = "#scrollableDiv",
    debug_dir: Optional[str] = None,
    timeout_ms: int = 15000,
    username: Optional[str] = None,
    created_at: Optional[str] = None,
    reset_scroll: bool = True,
) -> Optional[bool]:
    """Saves one Approved post's video to Google Drive using Refunnel's
    OWN native upload -- Refunnel does the file transfer server-side;
    this only drives the UI and picks the folder.

    CONFIRMED REAL flow, from saved pages and screenshots of the live UI:
      1. On an APPROVED card, "Upload to Google Drive" lives in the
         "Usage rights approved" status bar's chevron dropdown -- the
         card footer holds ONLY that toggle (wrapping
         .usage-rights-approved-card). An earlier version clicked a
         dotted "..." icon instead; a live run's screenshot showed that
         opened the card's OTHER menu ("Show content / Mute creator /
         Delete from library"), so "Upload to Google Drive" never
         appeared and every attempt timed out.
      2. The save modal: "All folders" tab -> the brand's folder (a
         span with its exact name) -> "Save to Drive", which is
         DISABLED until a folder is picked, so it is waited on rather
         than clicked blind.

    Card lookup: by media id in the thumbnail URL first (works for
    TikTok and numeric Instagram ids -- confirmed), then, if given,
    by creator handle + displayed date for the 32-char hex Instagram
    ids that never appear in thumbnail URLs. The fallback refuses to
    act when more than one card matches (same creator, same day) --
    skipping is always safer than uploading the wrong video.

    Returns True once Save to Drive is clicked, False if the card
    couldn't be located at all (or was ambiguous), or None if the card
    WAS found but its actual current status on Refunnel's live page no
    longer matches what triggered this call -- CONFIRMED REAL, found
    via a saved debug snapshot: a media_id marked Approved in Master
    Data can genuinely show as Pending review on the live page if its
    status changed after the last export. None is a distinct, correct
    outcome, not a failure -- callers should treat it the same way
    Pending review is already treated for email scraping (skip
    cleanly, don't count it as an error, a fresh export picks up the
    real status). Raises on any other failure in the flow, after
    saving a debug snapshot if debug_dir is given.
    """
    # Reset BEFORE searching -- confirmed real: the search only scrolls
    # FORWARD, so anything above the current position was unreachable.
    #
    # reset_scroll=False lets a caller processing a BATCH skip this for
    # every individual item, matching the same proven pattern already
    # used for email scraping: reset ONCE before the whole batch, then
    # let each item's forward-only search continue from wherever the
    # last one left off (targets are in the same newest-first feed
    # order the search already moves through). CONFIRMED REAL gap this
    # closes: this call used to run unconditionally, so a 50-item batch
    # meant 50 full resets to the top and 50 full re-scrolls back down
    # -- far more scrolling/DOM churn per item than email scraping ever
    # does for a similarly-sized batch, on top of the same virtualized-
    # list instability that's already been the root cause every other
    # time it's shown up in this project.
    if reset_scroll:
        scroll_to_top(page, scroll_container_selector)
    grid_item, _ = _scroll_until_card_found(page, media_id, scroll_container_selector)

    if grid_item is None and username and created_at:
        fallback = card_selector_for_username_date(username, created_at)
        if fallback:
            scroll_to_top(page, scroll_container_selector)
            grid_item, _ = _scroll_until_card_found(
                page, media_id, scroll_container_selector, card_selector=fallback
            )
            if grid_item is not None:
                try:
                    matches = page.locator(fallback).count()
                except Exception:
                    matches = 1
                if matches > 1:
                    print(f"drive upload: {matches} cards match @{username.lstrip('@')} on "
                          f"{card_date_label(created_at)} -- skipping media_id={media_id!r} "
                          f"rather than risk uploading the wrong video.")
                    return False

    if grid_item is None:
        return False

    # CONFIRMED REAL root cause, found via a saved debug snapshot: two
    # media_ids failed identically across every version of this click
    # mechanism tried so far (the original ARIA-wrapper approach, this
    # rewrite's direct-card-click) -- because neither version was ever
    # the problem. Their card genuinely has no .usage-rights-approved-
    # card at all; it has .usage-rights-requested-card with a
    # .urq-title of "Pending review" instead. Their status changed on
    # Refunnel's live page sometime after Master Data was last
    # exported -- our sheet still says GRANTED, but the real page
    # disagrees. No click mechanism can open a menu that doesn't
    # exist. Checked BEFORE attempting to open anything, same proven
    # pattern already used for email scraping -- no wasted retries,
    # no stray menu left open, and a genuinely different, correct
    # outcome instead of a misleading "menu didn't open" error.
    try:
        is_now_pending_review = grid_item.locator(
            f".urq-title:has-text('{PENDING_REVIEW_TITLE_TEXT}')"
        ).count() > 0
    except Exception:
        is_now_pending_review = False
    if is_now_pending_review:
        print(f"drive upload: media_id={media_id!r} is marked Approved in Master Data, but "
              f"Refunnel's live page now shows it as {PENDING_REVIEW_TITLE_TEXT!r} -- its status "
              f"changed since the last export. Skipping rather than retrying against a menu that "
              f"genuinely doesn't exist for this card; a fresh export will pick up its real "
              f"current status.")
        return None

    try:
        _open_drive_upload_menu(page, media_id, grid_item, scroll_container_selector)

        all_folders_tab = page.locator(DRIVE_MODAL_ALL_FOLDERS_TAB_SELECTOR).first
        all_folders_tab.wait_for(state="visible", timeout=timeout_ms)
        all_folders_tab.click()

        folder_row = page.get_by_text(drive_folder_name, exact=True).first
        folder_row.wait_for(state="visible", timeout=timeout_ms)
        folder_row.click()

        save_button = page.locator(DRIVE_MODAL_SAVE_BUTTON_SELECTOR).first
        page.wait_for_selector(f"{DRIVE_MODAL_SAVE_BUTTON_SELECTOR}:not([disabled])", timeout=timeout_ms)
        save_button.click()
        return True
    except Exception:
        if debug_dir:
            try:
                out_dir = Path(debug_dir)
                out_dir.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(out_dir / f"drive_upload_failure_{media_id}.png"), full_page=True)
                (out_dir / f"drive_upload_failure_{media_id}.html").write_text(page.content(), encoding="utf-8")
            except Exception:
                pass
        raise


def _open_drive_upload_menu(page: Page, media_id: str, grid_item, scroll_container_selector: str,
                            attempts: int = 3) -> None:
    """Opens an Approved card's status-toggle dropdown and clicks
    "Upload to Google Drive". Modeled directly on _open_usage_rights_menu
    (email scraping) -- reusing the exact same proven mechanisms, not a
    new, separately-guessed approach.

    CONFIRMED REAL bug this fixes: this Drive flow was never updated
    with ANY of the fixes built for the email-scraping flow across many
    rounds. It still clicked the ARIA wrapper (.pop-up-menu >
    [aria-controls]:has(.usage-rights-approved-card)) instead of the
    card itself, used a bare scroll_into_view_if_needed(timeout=4000)
    that doesn't even scroll (it only WAITS for the element to already
    be in view), a plain, unforced click, and no retry loop at all --
    one attempt, then straight to failure. A live 527-video run showed
    exactly the errors this predicts: hundreds of consecutive
    "Locator.scroll_into_view_if_needed: Timeout 4000ms exceeded" and
    "aria-controls]:has(.usage-rights-approved-card)" failures once the
    run reached deeper into the grid, the same virtualized-list
    instability the email flow already had this fixed for.
    """
    card_selector = ".usage-rights-approved-card"
    last_error: Optional[Exception] = None
    early_geometry_note = ""
    for attempt in range(attempts):
        try:
            card = grid_item.locator(card_selector).first
            if card.count() == 0:
                scroll_to_top(page, scroll_container_selector)
                grid_item, _ = _scroll_until_card_found(page, media_id, scroll_container_selector)
                if grid_item is None:
                    raise ExportError(f"card for media_id={media_id!r} left the page and couldn't be re-found")
                card = grid_item.locator(card_selector).first

            if attempt == 0:
                # Diagnostic-only, read ONCE, right here -- the same
                # approach that turned out to be decisive for the
                # email-scraping flow after several rounds of guessing
                # at the cause. CONFIRMED REAL need: the SAME two
                # media_ids failed identically, with the exact same
                # 4000ms timing, across multiple independent runs with
                # different batch sizes -- not random timing variance,
                # something specific and repeatable about these two
                # posts. This is the only way to see it directly
                # instead of continuing to guess.
                try:
                    geometry = card.evaluate(_CARD_GEOMETRY_JS, timeout=EVALUATE_TIMEOUT_MS)
                    early_geometry_note = f" Toggle geometry when first found: {geometry}."
                except Exception as geometry_error:
                    early_geometry_note = (
                        f" Couldn't read toggle geometry even on the first attempt "
                        f"(gone before we could even measure it): {geometry_error}."
                    )

            card.evaluate(_VERIFY_CARD_JS, _DANGEROUS_BUTTON_PATTERN.pattern, timeout=EVALUATE_TIMEOUT_MS)

            try:
                card.click(force=True, timeout=MENU_CLICK_TIMEOUT_MS)
            except Exception:
                pass

            page.wait_for_timeout(150)
            try:
                expanded = card.evaluate(_MENU_EXPANDED_JS, timeout=EVALUATE_TIMEOUT_MS)
            except Exception:
                expanded = None

            upload_item = page.locator(DRIVE_UPLOAD_MENU_ITEM_SELECTOR).first
            try:
                already_open = upload_item.is_visible()
            except Exception:
                already_open = False

            if expanded != "true" and not already_open:
                card.evaluate(_POINTER_SEQUENCE_JS, timeout=EVALUATE_TIMEOUT_MS)

            upload_item.wait_for(state="visible", timeout=MENU_OPEN_TIMEOUT_MS)
            _safe_click(upload_item, timeout_ms=MENU_CLICK_TIMEOUT_MS)
            return
        except Exception as e:
            last_error = e
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass
            if attempt < attempts - 1:
                page.wait_for_timeout(150)
    raise ExportError(
        f"Card for media_id={media_id!r} was found, but its Drive-upload menu didn't open "
        f"after {attempts} attempts (real forced click, then full pointer sequence, each "
        f"attempt). Original error: {last_error}.{early_geometry_note}"
    ) from last_error

_MEDIA_ID_IN_SRC = re.compile(r"(tk_\d+|ig_[0-9a-f]{32}|ig_\d+)")


def modal_post_check(image_srcs: Iterable[str], media_id: str) -> str:
    """Does the open "Request usage rights" modal belong to media_id?

    CONFIRMED REAL: the modal shows the post's own preview image, whose
    filename embeds the media id (a live snapshot's modal contained
    "tk_7688237002000551181_0.jpg" for exactly that post). So the modal
    can be checked against the post we MEANT to open.

    Why it matters: on a virtualized grid that keeps re-rendering, a
    click can land on a DIFFERENT card than the one resolved moments
    earlier -- opening another creator's modal. Without this check that
    creator's email would be silently recorded against the wrong post.

    Returns "match", "mismatch" (another post's id is present -- refuse),
    or "unknown" (no identifiable post image, e.g. some Instagram posts
    -- nothing to contradict, so don't block on it).
    """
    found = set()
    for src in image_srcs:
        for mid in _MEDIA_ID_IN_SRC.findall(src or ""):
            found.add(mid)
    if media_id in found:
        return "match"
    if found:
        return "mismatch"
    return "unknown"


USAGE_RIGHTS_CARD_SELECTOR = ".usage-rights-request-card, .usage-rights-requested-card"
USAGE_RIGHTS_MENU_TEXT = "Request creator approval to use this content in your marketing"

# Per-attempt timings for opening the usage-rights menu. CONFIRMED REAL
# cost this cuts: every locator.evaluate() call here used to have NO
# explicit timeout, silently falling back to Playwright's own 30s
# default -- a live run's Call log showed exactly this, waiting to
# resolve a locator for a card that was confirmed completely absent
# from the page's own HTML at that moment. With every operation below
# now explicitly bounded, a genuinely stuck post costs roughly 35s for
# all 3 attempts combined (not 30s x however many unbounded calls it
# happened to hit), and a working one returns in well under a second.
MENU_CLICK_TIMEOUT_MS = 1500
MENU_OPEN_TIMEOUT_MS = 2500
# CONFIRMED REAL, root-cause bug this fixes -- and it explains why the
# problem persisted across BOTH the wrapper-click AND the card-click
# versions of this code: locator.evaluate() resolves its locator with
# PLAYWRIGHT'S OWN 30-SECOND DEFAULT if no timeout is given, completely
# separate from any timeout passed to .click(). A live run's Call log
# showed it waiting on exactly that locator resolution, and the same
# card was confirmed completely absent from the page's own HTML at the
# moment it finally gave up -- the card genuinely was recycled away,
# and every .evaluate() call was silently allowed to wait the full 30s
# hoping it would reappear, instead of failing fast so the retry loop
# could actually retry within a sane total budget.
# 2000 was tuned to escape the OLD problem (an unbounded 30s wait) --
# not tested against whether it's long enough for a genuinely-present
# element that's just momentarily settling. Loosened to 4000: still
# nowhere near the 30s that caused the original "never ends" complaint
# (3 attempts x 4s is a worst case of ~12s just for this piece, not
# 90s), but gives real room for the grid to catch up.
EVALUATE_TIMEOUT_MS = 4000

# Runs INSIDE the page, in one synchronous turn, on the resolved card.
# Every step happens before react-virtuoso can process a scroll event
# and recycle the node -- so there is no gap for the card to vanish in.
# Confirms the element is genuinely connected, runs the send-button
# safety net, and scrolls it into view -- the MINIMUM amount needed,
# not centered. It deliberately does NOT click: a live run proved a
# synthetic click reaches the correct, connected card and still
# doesn't open the menu.
#
# CONFIRMED REAL, and a two-part correction of an earlier assumption.
# This used to scroll with block:"center", reasoning it would keep the
# sticky header off the click point -- but a user's own screenshot
# showed a near-top card's failure snapshot landing many rows further
# down the list than it started, consistent with centering forcing it
# to the screen's MIDDLE and over-scrolling past where it actually was.
#
# That led to removing the scroll ENTIRELY, on the theory that
# force=True and the pointer sequence below bypass hit-testing and so
# don't need the element positioned anywhere in particular. That part
# was wrong: force=True skips actionability checks like the sticky
# header's hit-test, but does NOT make an off-screen element clickable
# -- Playwright still needs real on-screen coordinates to click at, and
# an element beyond the viewport has none. Confirmed directly: a user's
# own run captured the geometry of 5 separate failing cards the moment
# each was first found, before any click was attempted. All 5 had a
# normal, non-zero size (185x46) and a horizontal position nowhere near
# either edge (left 379, right 564 of a 1280px-wide viewport) -- but
# EVERY one had top > 720, the viewport's own height. Not a column
# problem, not a sizing problem: the card just wasn't scrolled into
# view yet.
#
# block:"nearest" is the middle ground -- it scrolls only enough to
# bring the element into view (nothing at all if it's already there),
# never forcing it to the center, so it can't reproduce the earlier
# over-scroll while still fixing this.
_VERIFY_CARD_JS = """(el, dangerous) => {
    if (!el.isConnected) { throw new Error("card detached before click"); }
    if (new RegExp(dangerous, "i").test(el.innerText || "")) {
        throw new Error("refusing to click a send/submit-like element");
    }
    el.scrollIntoView({block: "nearest", inline: "nearest"});
    if (!el.isConnected) { throw new Error("card detached after scrolling into view"); }
}"""

# Second mechanism: the FULL pointer/mouse sequence dispatched straight to
# the card element (no coordinates, so nothing can intercept it). Used only
# if the real forced click didn't open the menu.
_POINTER_SEQUENCE_JS = """(el) => {
    if (!el.isConnected) { throw new Error("card detached before pointer sequence"); }
    const r = el.getBoundingClientRect();
    const opts = {bubbles: true, cancelable: true, view: window, button: 0,
                  clientX: r.left + r.width / 2, clientY: r.top + r.height / 2};
    el.dispatchEvent(new PointerEvent("pointerdown", {...opts, pointerId: 1, isPrimary: true}));
    el.dispatchEvent(new MouseEvent("mousedown", opts));
    el.dispatchEvent(new PointerEvent("pointerup", {...opts, pointerId: 1, isPrimary: true}));
    el.dispatchEvent(new MouseEvent("mouseup", opts));
    el.dispatchEvent(new MouseEvent("click", opts));
}"""

# Reads (never clicks) the disclosure wrapper's open state from the card's
# CURRENT node, so a recycled node can't leave us polling a stale one.
_MENU_EXPANDED_JS = """(el) => {
    const w = el.closest('[aria-controls]');
    return w ? w.getAttribute('aria-expanded') : null;
}"""

# Diagnostic only, never used to decide anything -- read once, on the
# FIRST attempt only, immediately after the card is found and before
# any click is attempted. CONFIRMED REAL pattern this targets: a
# user's own manual audit of 6 failures across a 25-post run found
# EVERY SINGLE ONE was the 4th (rightmost) card of a 4-column grid
# row, no exceptions -- not scroll depth, not dataset size, a
# specific, consistent column.
#
# CONFIRMED REAL correction to an earlier version of this same idea:
# reading geometry at the END, after giving up, only ever showed the
# card was ALREADY gone -- unsurprising, since 3 full attempts' worth
# of re-searching and re-scrolling had already happened by then.
# Reading it at the earliest possible moment instead is the only way
# this can tell us anything about what the element looked like when
# it was actually still there.
_CARD_GEOMETRY_JS = """(el) => {
    const r = el.getBoundingClientRect();
    return {
        connected: el.isConnected,
        width: Math.round(r.width),
        height: Math.round(r.height),
        top: Math.round(r.top),
        left: Math.round(r.left),
        right: Math.round(r.right),
        viewportWidth: window.innerWidth,
        viewportHeight: window.innerHeight,
    };
}"""


def _open_usage_rights_menu(page: Page, media_id: str, grid_item, scroll_container_selector: str,
                            media_row: Optional[dict] = None, attempts: int = 3):
    """Opens the usage-rights menu for media_id's card and clicks its
    "Request creator approval..." item. Returns the (possibly re-found)
    grid_item. Raises ExportError if it can't.

    Built from a live run's own Playwright call log plus debug HTML:

    1. PHYSICAL CLICKS KEPT MISSING. The log showed "element is not
       stable", then "<div class=new-tabs-switch> from <div class=
       set-sticky> intercepts pointer events", then "element was
       detached". Playwright's actionability wait gave the virtualized
       list time to recycle the card, and the sticky header sat over it.

    1b. BUT A CLICK-ONLY DOM EVENT DOESN'T OPEN THE MENU. A later live
       run clicked the correct, connected card with el.click() four times
       per post; the menu never opened (the final error was the menu wait,
       not "card detached", so the click genuinely landed). el.click() --
       like dispatchEvent(new MouseEvent("click")) -- fires ONLY "click".
       The proven-working original code used a real click, which fires
       pointerdown, mousedown, pointerup, mouseup AND click; popover
       triggers commonly listen on pointerdown/mousedown. So each attempt
       now uses (a) Playwright's real click with force=True -- the full
       trusted sequence, minus the actionability wait that caused the
       recycling race -- and only if that didn't open it, (b) the full
       pointer sequence dispatched directly to the element.

    2. SCROLL THE MINIMUM AMOUNT, NEVER CENTER. Centering (block:
       "center") over-scrolled near-top cards well past where they
       actually were -- a user's own screenshot proved it directly.
       Removing the scroll entirely fixed that, but broke something
       else: force=True skips hit-testing, not the requirement that an
       element have real on-screen coordinates to click at all. Direct
       geometry captured from 5 separate live failures, each read the
       moment its card was first found, confirmed exactly that -- a
       normal, correctly-sized element (185x46, nowhere near a
       horizontal edge) whose top sat below the viewport's own height
       every single time. block:"nearest" (see _VERIFY_CARD_JS) is the
       middle ground: scrolls only enough to bring the element into
       view, nothing if it's already there, never forcing it to center.

    3. CLICK THE CARD ITSELF, NOT ITS WRAPPER. The real ancestor chain
       is card -> div[cursor:pointer] -> div[aria-controls]. The
       cursor:pointer div, sitting between them, most likely owns the
       handler. A DOM click only bubbles UPWARD, so clicking the outer
       aria-controls wrapper would never pass through it. Clicking the
       innermost card bubbles through both -- matching what the
       proven-working original code clicked.

    4. RETRY IN PLACE. Retries re-query the card where it is (Playwright
       locators are lazy). Only if it has genuinely left the page do we
       reset to the top and search again -- resetting on every retry
       meant re-scrolling thousands of cards per attempt, and that
       scrolling is itself what churns the list.
    """
    last_error: Optional[Exception] = None
    early_geometry_note = ""
    for attempt in range(attempts):
        try:
            card = grid_item.locator(USAGE_RIGHTS_CARD_SELECTOR).first
            if card.count() == 0:
                scroll_to_top(page, scroll_container_selector)
                grid_item = _find_card_for_media(page, media_id, scroll_container_selector, media_row)
                if grid_item is None:
                    raise ExportError(f"card for media_id={media_id!r} left the page and couldn't be re-found")
                card = grid_item.locator(USAGE_RIGHTS_CARD_SELECTOR).first

            if attempt == 0:
                # Diagnostic-only, read ONCE, right here -- the earliest
                # possible moment, before any click has a chance to
                # disturb anything. CONFIRMED REAL correction to an
                # earlier version of this same idea: reading geometry
                # only at the very END (after all attempts already
                # failed) just showed the card was ALREADY gone by
                # then -- of course it was, 3 full attempts' worth of
                # re-searching and re-scrolling had already happened.
                # This can only tell us anything if it runs before that
                # churn, not after it.
                try:
                    geometry = card.evaluate(_CARD_GEOMETRY_JS, timeout=EVALUATE_TIMEOUT_MS)
                    early_geometry_note = f" Toggle geometry when first found: {geometry}."
                except Exception as geometry_error:
                    early_geometry_note = (
                        f" Couldn't read toggle geometry even on the first attempt "
                        f"(gone before we could even measure it): {geometry_error}."
                    )

            card.evaluate(_VERIFY_CARD_JS, _DANGEROUS_BUTTON_PATTERN.pattern, timeout=EVALUATE_TIMEOUT_MS)

            # Mechanism 1: Playwright's REAL click -- trusted pointerdown,
            # mousedown, pointerup, mouseup, click, exactly what the
            # proven-working original code sent -- but force=True skips the
            # actionability wait that gave virtuoso time to recycle the card.
            # The card is centered, so the sticky header isn't over it.
            try:
                card.click(force=True, timeout=MENU_CLICK_TIMEOUT_MS)
            except Exception:
                pass  # mechanism 2 below still gets its chance

            page.wait_for_timeout(150)  # let React commit the open state
            try:
                expanded = card.evaluate(_MENU_EXPANDED_JS, timeout=EVALUATE_TIMEOUT_MS)
            except Exception:
                expanded = None

            menu_item = page.get_by_role("menuitem").filter(has_text=USAGE_RIGHTS_MENU_TEXT).first
            try:
                already_open = menu_item.is_visible()
            except Exception:
                already_open = False

            # Only fire mechanism 2 when the menu is confirmed NOT open. The
            # menu-item check guards the case where the state read came back
            # unknown (e.g. the node just re-rendered) -- firing a second
            # click into an ALREADY-open menu would toggle it shut again.
            if expanded != "true" and not already_open:
                # Mechanism 2: the full pointer sequence dispatched straight
                # to the element -- no coordinates, nothing can intercept.
                card.evaluate(_POINTER_SEQUENCE_JS, timeout=EVALUATE_TIMEOUT_MS)

            menu_item.wait_for(state="visible", timeout=MENU_OPEN_TIMEOUT_MS)
            _safe_click(menu_item, timeout_ms=MENU_CLICK_TIMEOUT_MS)
            return grid_item
        except Exception as e:
            last_error = e
            try:
                page.keyboard.press("Escape")  # never stack a new open on a half-open menu
            except Exception:
                pass
            if attempt < attempts - 1:
                page.wait_for_timeout(150)

    raise ExportError(
        f"Card for media_id={media_id!r} was found, but its usage-rights menu didn't open "
        f"after {attempts} attempts (real forced click, then full pointer sequence, each "
        f"attempt). Not marked as 'no email' -- it will be retried on a future run. "
        f"Original error: {last_error}.{early_geometry_note}"
    ) from last_error


def _find_card_for_media(page: Page, media_id: str, scroll_container_selector: str,
                         media_row: Optional[dict] = None, skip_id_search: bool = False):
    """Finds media_id's card: by id in the thumbnail URL first, then by
    creator handle + displayed date for posts whose id never appears in
    their thumbnail (some 32-char hex Instagram ids). The Drive upload
    path already had this fallback; email scraping never did."""
    grid_item = None
    if not skip_id_search:
        # skip_id_search: the caller already ran (and failed) this exact
        # search -- repeating it would waste a full scroll pass per post.
        grid_item, _ = _scroll_until_card_found(page, media_id, scroll_container_selector)
    if grid_item is not None or not media_row:
        return grid_item
    fallback = card_selector_for_username_date(media_row.get("username", ""), media_row.get("created_at", ""))
    if not fallback:
        return None
    scroll_to_top(page, scroll_container_selector)
    grid_item, _ = _scroll_until_card_found(page, media_id, scroll_container_selector, card_selector=fallback)
    if grid_item is not None:
        try:
            if page.locator(fallback).count() > 1:
                return None  # same creator, same day -- ambiguous; never guess
        except Exception:
            pass
    return grid_item


def scrape_creator_emails(
    page: Page,
    media_rows: dict,
    media_ids: Iterable[str],
    scroll_container_selector: str = "#scrollableDiv",
    debug_dir: Optional[str] = None,
    max_consecutive_failures: Optional[int] = None,
    max_consecutive_empty_fields: Optional[int] = None,
    on_email_found: Optional[Callable[[str, str], None]] = None,
) -> dict:
    """For each media id needing an email, open its 'Request usage
    rights' flow, read the pre-filled email, and close WITHOUT sending
    anything -- closes via the Escape key rather than a close button,
    since Escape can't submit a form.

    Returns (results, empty_ids): `results` is {media_id: email} for
    whichever ones had an email available. `empty_ids` is the set of
    ids CONFIRMED to have no email on file (the modal opened fine, the
    field was genuinely empty) -- distinct from ids that hit an
    exception (crash, timeout), which are NOT in this set since their
    true status is still unknown and they deserve a retry. Confirmed
    real, serious bug this fixes: a crash-triggered restart was
    re-scanning the ENTIRE target list from scratch every time,
    including hundreds of ids already confirmed empty earlier in the
    SAME run -- pure wasted work. The caller (run_daily_sync.py) is
    expected to accumulate this set across restarts and exclude it from
    the next attempt's target list, so a restart only ever spends time
    on ids that are genuinely still unknown.

    If debug_dir is given, the FIRST time a card can't be located, this
    saves a screenshot + the scroll container's scrollTop/scrollHeight/
    clientHeight to debug_dir -- one snapshot, not one per failure, so
    it doesn't flood the artifact upload. Every failure still logs
    those same numbers to stdout regardless.

    on_email_found(media_id, email): if given, called immediately after
    each email is successfully found -- before moving to the next post.
    Use this to save progress to the sheet AS IT HAPPENS (e.g. via
    GspreadSheetsClient.update_single_cell) rather than only at the very
    end, so a later interruption doesn't lose emails already found. A
    failure inside this callback is caught and logged, not allowed to
    abort the whole scraping loop.

    Prints a "starting -- N post(s) to attempt" line immediately, then a
    "progress X/N attempted -- Y found, Z failed" line every 25 items
    (and once more at the very end) -- confirmed real need: a run gave
    no visible sign of life in the GitHub Actions log for 25+ minutes,
    since nothing printed until either a failure or the very end,
    making it impossible to tell "still working, just slow" apart from
    "stuck" or "broken" from the log alone.

    Every individual post failing is ALREADY handled by design -- one
    bad post (page didn't load, element timing, whatever) just gets
    skipped and the loop moves to the next id. That's the normal,
    expected behavior and needs no special handling.

    BOTH threshold-based circuit breakers are DISABLED by default now
    (max_consecutive_failures and max_consecutive_empty_fields) --
    confirmed real, explicit, repeated instruction: this must run to
    completion of the full target list every time, regardless of how
    many consecutive failures (of any kind, including genuine
    exceptions/crashes) occur, and regardless of how many posts turn
    out to have no email on file.

    A THIRD, DIFFERENT mechanism is always on, and isn't a threshold at
    all: if an exception's own message says the browser target itself
    crashed (see _is_target_crashed), this stops the current attempt
    IMMEDIATELY -- not after N repeats, since one such message is
    already a direct fact that the whole page is dead, not a pattern
    that needs confirming. Confirmed real need: without this, a run's
    log showed the loop spending a huge amount of wall-clock time
    individually failing on ~1070 posts, one at a time, after the
    browser had already crashed -- each one doomed from the start, but
    with nothing to notice that until the whole list was exhausted.
    This costs NOTHING in coverage -- every id still in the list when
    this fires is simply retried on the next attempt, exactly like any
    other unresolved id already is (confirmed by the numbers matching
    exactly in a real run: 1944 attempted - 868 confirmed-empty = 1076
    correctly re-attempted next time, not a restart from zero).

    Honest tradeoff for the two DISABLED breakers, stated plainly: a
    slow, non-crash failure (a timeout, a missing element) still isn't
    caught early anymore -- only a confirmed crash is. That's a
    deliberate, narrower net than before, on purpose.

    Pass an int for either max_consecutive_* parameter explicitly if
    you ever want either threshold-based safety net back for a specific
    run (e.g. to bound worst-case runtime deliberately). Individual
    empty-field occurrences are still not printed one at a time
    (confirmed real complaint about log
    clutter) -- they're tallied and reported in the periodic progress
    line instead (see _format_progress_line), broken down by reason.

    Flow -- confirmed from a real, complete HTML trace of the whole
    interaction (card -> popover -> modal), for BOTH never-requested and
    already-Requested posts:
      1. Each post card (a react-virtuoso grid item, matched here by its
         thumbnail image src containing the media id) has a status
         toggle in its footer. The class name differs by status --
         `.usage-rights-request-card` for never-requested posts,
         `.usage-rights-requested-card` for already-Requested ones (one
         letter different: "request" vs "requested") -- so both are
         matched together.
      2. Clicking it reveals a popover whose top menu item's TITLE also
         differs by status ("Request usage-rights" vs "Usage-rights
         requested"), but its SUBTITLE is identical either way
         ("Request creator approval to use this content in your
         marketing") -- matched on that instead, since it doesn't vary.
      3. That opens a modal (`.usageRightsModal`) with three channel
         tabs (`.ur-tab-card`): Email / TikTok DM / TT Shop DM -- Email
         is active by default, confirmed for both post statuses.
      4. The email field has a real, properly-linked
         `<label>Creator email address</label>`, so `get_by_label()`
         finds it directly.
      5. The confirmed "Send request" button (`.ur-bottom-btn`) is never
         clicked -- `_safe_click`'s pattern check refuses it regardless.

    Since react-virtuoso virtualizes the grid, a target card may not be
    mounted in the DOM at all if scroll_to_load_all() already scrolled
    past it -- _scroll_until_card_found() scrolls forward to bring it
    back before interacting with it.
    """
    if not SCRAPE_EMAILS_ENABLED:
        print("scrape_creator_emails: SCRAPE_EMAILS_ENABLED is False, skipping. "
              "See refunnel_export.py module docstring.")
        return {}

    # Consolidated onto the shared scroll_to_top() helper -- this used
    # to be its own separate inline reset here, duplicated by a SEPARATE
    # call the caller (run_daily_sync.py) also added, once the real
    # cascading-failure bug below was found and fixed. Owning the whole
    # reset responsibility (start of run AND after each failure) here,
    # in the one function that actually needs the invariant, removes
    # that duplication -- see scroll_to_top()'s own docstring for the
    # full confirmed evidence.
    scroll_to_top(page, scroll_container_selector)
    page.wait_for_timeout(500)

    results: dict = {}
    empty_ids: set = set()
    # CONFIRMED REAL gap this replaces: a single shared boolean meant
    # whichever failure type happened FIRST in a run "used up" the
    # only debug snapshot for the entire run -- a live artifact showed
    # only the "no email on file" case (correct, not a bug), while the
    # separately ongoing "detached click" issue never got its own
    # snapshot at all, because the empty-field case happened first and
    # consumed the one slot. Tracked per DISTINCT failure category now
    # (not per media_id -- still capped, just not collapsed to one
    # total), so each kind of failure gets its own piece of evidence
    # once per run.
    debug_snapshots_saved: set = set()
    consecutive_failures = 0
    consecutive_empty_fields = 0
    # Materialized into a list (not left as a lazy iterable) specifically
    # so its length is known upfront, for the "X/Y attempted" progress
    # line below -- confirmed real need: a real run gave no visible sign
    # of life for 25+ minutes, and the only log output visible in GitHub
    # Actions was the step's environment-variable header, not any of
    # this function's actual print() calls, since nothing printed
    # until either a failure or the very end.
    target_ids = list(_order_ids_for_scraping(media_rows, media_ids))
    total_targets = len(target_ids)
    print(f"scrape_creator_emails: starting -- {total_targets} post(s) to attempt.")
    attempted = 0
    found_count = 0
    empty_field_count = 0
    pending_review_skipped = 0
    PROGRESS_CHECKPOINT = 25
    for media_id in target_ids:
        try:
            grid_item, diagnostics = _scroll_until_card_found(page, media_id, scroll_container_selector)
            if grid_item is None and media_rows.get(media_id):
                # Creator + date fallback for posts whose id never appears
                # in their thumbnail URL. The Drive upload path had this;
                # email scraping never did, so those posts could only ever
                # fail with "couldn't locate" even while on the page.
                # (_find_card_for_media resets scroll itself before searching.)
                fallback_item = _find_card_for_media(
                    page, media_id, scroll_container_selector, media_rows.get(media_id),
                    skip_id_search=True,
                )
                if fallback_item is not None:
                    grid_item = fallback_item
            if grid_item is None:
                if _is_logged_out(page):
                    print(
                        "scrape_creator_emails: the session has been logged out mid-run (the "
                        "page is now Refunnel's login screen, which is why no cards can be "
                        "found). Stopping this attempt immediately rather than failing every "
                        "remaining post one at a time against a login page. Nothing is lost: "
                        "every remaining id stays in the target list and is retried once a "
                        "fresh session is ready."
                    )
                    break
                print(f"scrape_creator_emails: couldn't locate media_id={media_id!r} on the page "
                      f"after scrolling through everything. Container state: {diagnostics}")
                if debug_dir and "couldnt_locate" not in debug_snapshots_saved:
                    _save_scrape_failure_snapshot(page, debug_dir, media_id, category="couldnt_locate")
                    debug_snapshots_saved.add("couldnt_locate")

                # CONFIRMED REAL, root cause of a run where every id
                # after roughly the second one failed identically for
                # the entire rest of a 5245-post run:
                # _scroll_until_card_found() NEVER resets scroll
                # position -- it always continues forward from
                # wherever the container currently is (el.scrollTop +=
                # step, every single call). One failed search that
                # exhausts all the way to the real bottom therefore
                # permanently strands every SUBSEQUENT search at that
                # same bottom position too, since nothing else ever
                # resets it -- a single failure cascades into
                # destroying the rest of the entire run. Reset here,
                # immediately after a failed search, so only THIS one
                # id is affected and the next one gets a genuinely
                # fresh, working search -- not on every successful
                # search, which would make a 5000+ item run far slower
                # for no reason.
                scroll_to_top(page, scroll_container_selector)

                consecutive_failures += 1
                consecutive_empty_fields = 0
                if max_consecutive_failures is not None and consecutive_failures >= max_consecutive_failures:
                    print(
                        f"scrape_creator_emails: {consecutive_failures} failures in a row -- "
                        f"stopping early rather than grinding through the remaining ids. "
                        f"Returning the {len(results)} email(s) found before this happened."
                    )
                    break
                continue

            # CONFIRMED REAL, from live screenshots of BOTH menu types
            # plus the saved HTML: a post awaiting the brand's own
            # approve/decline decision shows "Pending review", and its
            # menu contains ONLY "Upload to Google Drive" and "Attach
            # to a campaign" -- there is NO "Request usage-rights" item
            # in it at all. A normal card's menu does have it (along
            # with Set usage-rights labels / Upload to Meta).
            #
            # So a Pending-review post can NEVER yield a creator email:
            # the scraper opens the menu, waits the full timeout for an
            # item that structurally cannot appear, and fails. Confirmed
            # against real data: ig_18018159830937735 -- the id that
            # failed FIRST on every single run, with exactly that menu
            # timeout -- is a Pending review card in the saved HTML.
            #
            # Detected by the card's TITLE text, not its class: a
            # Pending-review card uses .usage-rights-requested-card,
            # the SAME class as a genuine "Usage rights requested"
            # card, so the class alone genuinely cannot tell them
            # apart. The title text is the only real distinguisher.
            #
            # Skipped BEFORE opening the menu -- no click, no wasted
            # timeout, and (importantly) no risk of leaving a stray
            # menu open to interfere with the next post.
            try:
                is_pending_review = grid_item.locator(
                    f".urq-title:has-text('{PENDING_REVIEW_TITLE_TEXT}')"
                ).count() > 0
            except Exception:
                is_pending_review = False
            if is_pending_review:
                # CONFIRMED REAL noise this removes: the per-item message
                # used to print here every single time -- and pending
                # posts stay in the target list run after run (they're
                # never added to results or empty_ids, since there's
                # genuinely nothing to read), so a live run with 7
                # browser-crash restarts printed the SAME already-known
                # pending post's message again on every restart. The
                # progress line's own "N pending review" count, plus the
                # one-time summary at the end of this call, already say
                # everything worth saying -- this is why is skipped once,
                # not why on every repeat.
                pending_review_skipped += 1
                # CONFIRMED REAL bug this fixes: attempted was ALSO
                # incremented here, and this branch's own `continue`
                # still runs the loop's `finally:` block afterward --
                # finally ALWAYS runs, even on continue -- which
                # increments attempted AGAIN. Confirmed directly with a
                # minimal reproduction of this exact try/finally shape:
                # 25 target_ids with 2 pending-review among them left
                # attempted at 27, not 25, at the end of the loop.
                # Removed the double count here; the finally block's
                # own increment (which every other exit path already
                # relies on) is the only one needed.
                consecutive_failures = 0
                consecutive_empty_fields = 0
                continue

            # Re-finds the card fresh on each attempt (re-scrolling if
            # needed) rather than retrying the SAME stale locator chain
            # -- confirmed real, new failure pattern: a live run against
            # Swoveralls' much larger backlog showed the card resolving
            # successfully every single time, then getting "detached
            # from the DOM" mid-click, for the full 30s, on nearly every
            # item. react-virtuoso (the virtualized list library) reuses
            # DOM nodes for different items as the list scrolls/settles
            # -- Playwright's own built-in retry keeps re-querying the
            # SAME locator chain, but if the underlying node keeps
            # getting recycled faster than a click can land, that retry
            # alone never wins. Re-running the full find-and-click
            # sequence gives it a genuinely fresh DOM reference each
            # time instead of hammering the same doomed one.
            # Opens the menu via a centered DOM click on the card itself,
            # retrying in place -- see _open_usage_rights_menu for the full
            # evidence (sticky-header interception, 800px-vs-720px overshoot,
            # and why the innermost card, not its wrapper, is clicked).
            grid_item = _open_usage_rights_menu(
                page, media_id, grid_item, scroll_container_selector,
                media_row=media_rows.get(media_id),
            )
            _pace(page)


            # Email tab is active by default, but click it explicitly in
            # case that ever changes.
            email_tab = page.locator(".ur-tab-card", has_text="Email").first
            email_tab.wait_for(state="visible", timeout=5000)
            _safe_click(email_tab)
            _pace(page)

            # Confirm the open modal is THIS post's before reading anything
            # -- a click that landed on a shifted card would otherwise
            # record another creator's email against this post. See
            # modal_post_check for the confirmed-real evidence.
            try:
                modal_srcs = page.locator("[role=dialog] img").evaluate_all(
                    "els => els.map(e => e.getAttribute('src') || '')"
                )
            except Exception:
                modal_srcs = []
            if modal_post_check(modal_srcs, media_id) == "mismatch":
                raise ExportError(
                    f"The open usage-rights modal belongs to a DIFFERENT post than "
                    f"media_id={media_id!r} -- refusing to record its email against "
                    f"the wrong creator."
                )

            email_input = page.get_by_label(re.compile(r"Creator email address", re.I))
            email_input.wait_for(state="visible", timeout=5000)
            email_value = (email_input.input_value() or "").strip()

            if email_value:
                results[media_id] = email_value
                found_count += 1
                consecutive_failures = 0
                consecutive_empty_fields = 0
                if on_email_found:
                    try:
                        on_email_found(media_id, email_value)
                    except Exception as e:
                        print(f"scrape_creator_emails: on_email_found callback failed for "
                              f"media_id={media_id!r} (email was still found, just not "
                              f"saved incrementally): {e}")
            else:
                # No per-item print here anymore -- confirmed real
                # complaint about log clutter (25 identical lines in a
                # row). Tallied instead and reported in the periodic
                # progress line, broken down by reason. Still worth a
                # one-time debug snapshot (first occurrence only) for
                # genuine diagnosis if this pattern ever turns out to
                # be wrong.
                if debug_dir and "empty_field" not in debug_snapshots_saved:
                    _save_scrape_failure_snapshot(page, debug_dir, media_id, category="empty_field")
                    debug_snapshots_saved.add("empty_field")
                empty_field_count += 1
                empty_ids.add(media_id)
                consecutive_empty_fields += 1
                consecutive_failures = 0  # a clean "field was empty" isn't a crash/error
                if max_consecutive_empty_fields is not None and consecutive_empty_fields >= max_consecutive_empty_fields:
                    print(
                        f"scrape_creator_emails: {consecutive_empty_fields} posts in a row had "
                        f"no email on file -- stopping early rather than grinding through the "
                        f"remaining ids. Returning the {len(results)} email(s) found before "
                        f"this happened."
                    )
                    break

        except Exception as e:
            print(f"scrape_creator_emails: couldn't get email for media_id={media_id!r}: {e}")
            exception_category = f"exception:{type(e).__name__}"
            if debug_dir and exception_category not in debug_snapshots_saved:
                _save_scrape_failure_snapshot(page, debug_dir, media_id, category=exception_category)
                debug_snapshots_saved.add(exception_category)

            # REMOVED a scroll_to_top() reset that used to be here --
            # confirmed real, harmful over-reach: a live run showed
            # EVERY single item failing identically at the click step
            # ("element detached") right after this branch started
            # resetting unconditionally for ANY exception, including
            # ones with nothing to do with scroll position at all (a
            # menu never appearing, for instance). The reset itself
            # was very likely destabilizing the virtualized list right
            # before the NEXT item's click attempt, turning one
            # unrelated failure into a self-perpetuating cascade of
            # detached-click failures -- the opposite of what this was
            # meant to prevent. Only the "couldn't locate" branch above
            # has DIRECT, confirmed evidence (the diagnostics dict
            # showing scrollTop genuinely at the real bottom) that a
            # reset is actually needed; this general branch never did,
            # it was defensive insurance that turned out to cause the
            # exact class of problem it was guarding against.

            consecutive_failures += 1
            consecutive_empty_fields = 0
            if _is_logged_out(page):
                print(
                    "scrape_creator_emails: the session has been logged out mid-run -- "
                    "stopping this attempt immediately rather than failing every remaining "
                    "post against a login page. Nothing is lost: every remaining id stays in "
                    "the target list and is retried once a fresh session is ready."
                )
                break
            if _is_target_crashed(e):
                print(
                    "scrape_creator_emails: the browser target itself has crashed (not just "
                    "this one post) -- stopping this attempt immediately rather than "
                    "continuing to individually fail on every remaining post against a "
                    "browser that's confirmed dead. Nothing is lost: every remaining id is "
                    "still in the target list and will be correctly retried once a fresh "
                    "session is ready. This is a direct detection, not the old N-in-a-row "
                    "guess -- confirmed real need: without it, a real run spent a huge amount "
                    "of wasted time individually failing ~1070 posts one at a time after the "
                    "browser had already died."
                )
                break
            if max_consecutive_failures is not None and consecutive_failures >= max_consecutive_failures:
                print(
                    f"scrape_creator_emails: {consecutive_failures} failures in a row -- "
                    f"stopping early rather than grinding through the remaining ids. This "
                    f"pattern usually means every remaining post shares the same problem "
                    f"(e.g. a status whose card doesn't have this button at all), not bad "
                    f"luck on individual posts. Returning the {len(results)} email(s) "
                    f"found before this happened."
                )
                break

        finally:
            # Always try to back out via Escape, regardless of success/
            # failure above -- never clicks anything to close, so this
            # can't accidentally submit anything either.
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass
            # Pace between posts, not just within one -- this is the
            # bigger contributor to total added time since it runs once
            # per item rather than once per click. See README "Pacing
            # and its time cost". Wrapped in its own try/except too --
            # confirmed from a real crash log that this specific call,
            # unguarded, is exactly where a dead/crashed page's error
            # escaped the per-item error boundary entirely, aborting
            # the whole function instead of being caught and counted
            # toward the circuit breaker like every other failure here.
            try:
                _pace(page, EMAIL_SCRAPE_ITEM_PACE_MS)
            except Exception:
                pass

            # Periodic live progress -- exactly this granularity (every
            # 25, not every 1) so a run of hundreds/thousands of items
            # doesn't flood the log, while still giving a genuine,
            # frequent "is this actually doing anything" signal well
            # before the run finishes.
            attempted += 1
            if _should_print_progress(attempted, total_targets, PROGRESS_CHECKPOINT):
                print(_format_progress_line(attempted, total_targets, found_count, empty_field_count,
                                            pending_review_skipped))

    if pending_review_skipped:
        print(f"scrape_creator_emails: skipped {pending_review_skipped} post(s) awaiting your own "
              f"approve/decline decision ({PENDING_REVIEW_TITLE_TEXT!r}) -- those have no "
              f"usage-rights option to read an email from. Approving or declining them in "
              f"Refunnel makes them scrapeable on a future run.")
    return results, empty_ids

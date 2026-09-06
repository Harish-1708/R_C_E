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


# Turn this on only after you've manually verified scrape_creator_email()
# against the real site -- see module docstring and README.
SCRAPE_EMAILS_ENABLED = True

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
EMAIL_SCRAPE_ITEM_PACE_MS = (600, 1200)       # between finishing one post and starting the next
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


def scroll_to_load_all(
    page: Page,
    count_text_pattern: str = r"(\d+)\s+of\s+(\d+)\s+media",
    max_rounds: int = 400,
    scroll_pause_ms: int = 1200,
    idle_rounds_before_giving_up: int = 6,
    scroll_container_selector: str = "#scrollableDiv",
) -> None:
    """Scroll until the page's own '<loaded> of <total> media' counter
    (visible in your screenshot) shows loaded == total, or growth stalls
    for idle_rounds_before_giving_up consecutive scrolls in a row.

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

    first_read = read_counts()
    if first_read is None:
        raise ExportError(
            f"Couldn't find a '<n> of <total> media' counter on the page using pattern "
            f"{count_text_pattern!r}. The page structure may have changed -- update "
            f"count_text_pattern in scroll_to_load_all()."
        )

    idle_rounds = 0
    last_loaded = first_read[0]

    for _ in range(max_rounds):
        loaded, total = read_counts() or (last_loaded, first_read[1])
        if loaded >= total:
            return

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


def _safe_click(locator) -> None:
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
    locator.click()


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
    max_rounds: int = 200,
    scroll_step: int = 800,
    pace_ms_range: tuple = SCROLL_SEARCH_PACE_MS,
):
    """react-virtuoso (the grid library this page uses) only keeps
    nearby cards mounted in the DOM, unmounting far-off ones as you
    scroll -- confirmed from a real HTML dump. So a card matching
    media_id may simply not exist in the DOM yet/anymore. This scrolls
    `scroll_container_selector` forward in small steps until a card
    containing that media_id's thumbnail (matched by image src, which
    embeds the id) appears, or gives up.

    Returns (locator_or_none, diagnostics_dict). diagnostics_dict has
    scrollTop/scrollHeight/clientHeight read from the container right
    when this gives up (or None if that read itself failed) -- this
    tells us whether scrolling is actually moving the container at all
    (scrollTop stuck near 0 would mean the scrollTop assignment isn't
    taking effect on this element), separate from "moved fine but the
    card still never rendered."
    """
    selector = f"div.rf-virtuoso-item:has(img[src*='{media_id}'])"
    for _ in range(max_rounds):
        card = page.locator(selector)
        if card.count() > 0:
            return card.first, None
        page.evaluate(
            "(args) => { const el = document.querySelector(args.sel); "
            "if (el) { el.scrollTop += args.step; } }",
            {"sel": scroll_container_selector, "step": scroll_step},
        )
        _pace(page, pace_ms_range)

    try:
        diagnostics = page.evaluate(
            "(sel) => { const el = document.querySelector(sel); "
            "return el ? {scrollTop: el.scrollTop, scrollHeight: el.scrollHeight, "
            "clientHeight: el.clientHeight} : null; }",
            scroll_container_selector,
        )
    except Exception as e:
        diagnostics = {"diagnostic_read_failed": str(e)}

    return None, diagnostics


def _save_scrape_failure_snapshot(page: Page, debug_dir: str, media_id: str) -> None:
    """One-time diagnostic capture for scrape_creator_emails -- a
    screenshot and the raw page HTML, saved once (not per-failure) so
    we can actually see what's on screen when the scroll-search gives
    up, instead of guessing from log lines alone."""
    try:
        out_dir = Path(debug_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(out_dir / f"scrape_failure_{media_id}.png"), full_page=True)
        (out_dir / f"scrape_failure_{media_id}.html").write_text(page.content(), encoding="utf-8")
        print(f"Saved scrape-failure debug snapshot for media_id={media_id!r} to {out_dir}")
    except Exception as e:
        print(f"Couldn't save scrape-failure debug snapshot: {e}")


def scrape_creator_emails(
    page: Page,
    media_rows: dict,
    media_ids: Iterable[str],
    scroll_container_selector: str = "#scrollableDiv",
    debug_dir: Optional[str] = None,
    max_consecutive_failures: int = 25,
    on_email_found: Optional[Callable[[str, str], None]] = None,
) -> dict:
    """For each media id needing an email, open its 'Request usage
    rights' flow, read the pre-filled email, and close WITHOUT sending
    anything -- closes via the Escape key rather than a close button,
    since Escape can't submit a form.

    Returns {media_id: email} for whichever ones had an email available
    (some won't -- that's expected, not an error).

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

    Every individual post failing is ALREADY handled by design -- one
    bad post (page didn't load, element timing, whatever) just gets
    skipped and the loop moves to the next id. That's the normal,
    expected behavior and needs no special handling.

    max_consecutive_failures is a separate, LAST-RESORT circuit breaker
    for a genuinely systemic pattern, not for occasional one-off
    failures -- default 25 is deliberately generous so isolated
    failures never trigger it. It exists because a real run confirmed
    that once a post's usage rights are Approved (or presumably
    Declined), its card footer is replaced entirely with a status badge
    -- there's no request button to click at all, so every such attempt
    is a GUARANTEED failure, not intermittent bad luck.
    rows_needing_email_scrape() already excludes approved/declined for
    exactly this reason, but if some other status or edge case turns
    out to behave the same way, this stops a multi-hour run before it
    happens rather than after.

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

    page.evaluate(
        "(sel) => { const el = document.querySelector(sel); if (el) { el.scrollTop = 0; } }",
        scroll_container_selector,
    )
    page.wait_for_timeout(500)

    results: dict = {}
    debug_snapshot_saved = False
    consecutive_failures = 0
    for media_id in _order_ids_for_scraping(media_rows, media_ids):
        try:
            grid_item, diagnostics = _scroll_until_card_found(page, media_id, scroll_container_selector)
            if grid_item is None:
                print(f"scrape_creator_emails: couldn't locate media_id={media_id!r} on the page "
                      f"after scrolling through everything. Container state: {diagnostics}")
                if debug_dir and not debug_snapshot_saved:
                    _save_scrape_failure_snapshot(page, debug_dir, media_id)
                    debug_snapshot_saved = True
                consecutive_failures += 1
                if consecutive_failures >= max_consecutive_failures:
                    print(
                        f"scrape_creator_emails: {consecutive_failures} failures in a row -- "
                        f"stopping early rather than grinding through the remaining ids. "
                        f"Returning the {len(results)} email(s) found before this happened."
                    )
                    break
                continue

            request_toggle = grid_item.locator(
                ".usage-rights-request-card, .usage-rights-requested-card"
            ).first
            try:
                request_toggle.scroll_into_view_if_needed(timeout=4000)
                request_toggle.wait_for(state="visible", timeout=4000)
            except Exception as e:
                raise ExportError(
                    f"Card for media_id={media_id!r} was found in the DOM, but its "
                    f"status toggle never became visible/clickable within 4s even "
                    f"after scroll_into_view_if_needed() -- may be obscured by an "
                    f"overlay, or this post's rights status uses yet another class "
                    f"name not yet accounted for (Approved cards use a different, "
                    f"non-clickable badge entirely -- confirmed separately). "
                    f"Original error: {e}"
                ) from e
            _safe_click(request_toggle)
            _pace(page)

            # The popup's top item's TITLE differs by status ("Request
            # usage-rights" vs "Usage-rights requested"), but its
            # subtitle is identical either way -- matched on that
            # instead, since it doesn't vary.
            top_menu_item = page.get_by_role("menuitem").filter(
                has_text="Request creator approval to use this content in your marketing"
            ).first
            top_menu_item.wait_for(state="visible", timeout=5000)
            _safe_click(top_menu_item)
            _pace(page)

            # Email tab is active by default, but click it explicitly in
            # case that ever changes.
            email_tab = page.locator(".ur-tab-card", has_text="Email").first
            email_tab.wait_for(state="visible", timeout=5000)
            _safe_click(email_tab)
            _pace(page)

            email_input = page.get_by_label(re.compile(r"Creator email address", re.I))
            email_input.wait_for(state="visible", timeout=5000)
            email_value = (email_input.input_value() or "").strip()

            if email_value:
                results[media_id] = email_value
                consecutive_failures = 0
                if on_email_found:
                    try:
                        on_email_found(media_id, email_value)
                    except Exception as e:
                        print(f"scrape_creator_emails: on_email_found callback failed for "
                              f"media_id={media_id!r} (email was still found, just not "
                              f"saved incrementally): {e}")

        except Exception as e:
            print(f"scrape_creator_emails: couldn't get email for media_id={media_id!r}: {e}")
            if debug_dir and not debug_snapshot_saved:
                _save_scrape_failure_snapshot(page, debug_dir, media_id)
                debug_snapshot_saved = True
            consecutive_failures += 1
            if consecutive_failures >= max_consecutive_failures:
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

    return results

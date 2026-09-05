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

import re
import time
from pathlib import Path
from typing import Iterable, Optional

from playwright.sync_api import Page


# Turn this on only after you've manually verified scrape_creator_email()
# against the real site -- see module docstring and README.
SCRAPE_EMAILS_ENABLED = False

# Refunnel's "Request usage rights" flow has a "Send request" button
# (confirmed from your screenshot). We refuse to click anything whose
# accessible name matches this, as a hard safety net independent of
# whatever selector logic runs above it.
_DANGEROUS_BUTTON_PATTERN = re.compile(r"send\s*request", re.I)


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
                # Not raising here -- partial data is still useful, and the
                # caller's own row-count logging will make a short export
                # obvious rather than silent. But it IS worth knowing this
                # happened, so we print a loud warning.
                print(
                    f"WARNING: scroll_to_load_all stopped early at {new_loaded}/{total} "
                    f"after {idle_rounds} rounds with no growth."
                )
                return
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
    pause_ms: int = 150,
):
    """react-virtuoso (the grid library this page uses) only keeps
    nearby cards mounted in the DOM, unmounting far-off ones as you
    scroll -- confirmed from a real HTML dump. So a card matching
    media_id may simply not exist in the DOM yet/anymore. This scrolls
    `scroll_container_selector` forward in small steps until a card
    containing that media_id's thumbnail (matched by image src, which
    embeds the id) appears, or gives up. Returns the matching Locator,
    or None.
    """
    selector = f"div.rf-virtuoso-item:has(img[src*='{media_id}'])"
    for _ in range(max_rounds):
        card = page.locator(selector)
        if card.count() > 0:
            return card.first
        page.evaluate(
            "(args) => { const el = document.querySelector(args.sel); "
            "if (el) { el.scrollTop += args.step; } }",
            {"sel": scroll_container_selector, "step": scroll_step},
        )
        page.wait_for_timeout(pause_ms)
    return None


def scrape_creator_emails(
    page: Page,
    media_rows: dict,
    media_ids: Iterable[str],
    scroll_container_selector: str = "#scrollableDiv",
) -> dict:
    """For each media id needing an email, open its 'Request usage
    rights' flow, read the pre-filled email, and close WITHOUT sending
    anything -- closes via the Escape key rather than a close button,
    since Escape can't submit a form.

    Returns {media_id: email} for whichever ones had an email available
    (some won't -- that's expected, not an error).

    Flow -- confirmed from a real, complete HTML trace of the whole
    interaction (card -> popover -> modal):
      1. Each post card (a react-virtuoso grid item, matched here by its
         thumbnail image src containing the media id) has a
         `.usage-rights-request-card` toggle. Clicking it reveals a
         popover whose menu items include a top one with the exact text
         "Request usage-rights" (`role="menuitem"`), distinct from
         "Request whitelisting-rights" below it.
      2. That opens a modal (`.usageRightsModal`) with three channel
         tabs (`.ur-tab-card`): Email / TikTok DM / TT Shop DM -- Email
         is active by default.
      3. The email field has a real, properly-linked
         `<label>Creator email address</label>`, so `get_by_label()`
         finds it directly.
      4. The confirmed "Send request" button (`.ur-bottom-btn`) is never
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
    for media_id in _order_ids_for_scraping(media_rows, media_ids):
        try:
            grid_item = _scroll_until_card_found(page, media_id, scroll_container_selector)
            if grid_item is None:
                print(f"scrape_creator_emails: couldn't locate media_id={media_id!r} on the page "
                      f"after scrolling through everything.")
                continue

            request_toggle = grid_item.locator(".usage-rights-request-card").first
            _safe_click(request_toggle)

            top_menu_item = page.get_by_role("menuitem", name=re.compile(r"^Request usage-rights$", re.I)).first
            top_menu_item.wait_for(state="visible", timeout=5000)
            _safe_click(top_menu_item)

            # Email tab is active by default, but click it explicitly in
            # case that ever changes.
            email_tab = page.locator(".ur-tab-card", has_text="Email").first
            email_tab.wait_for(state="visible", timeout=5000)
            _safe_click(email_tab)

            email_input = page.get_by_label(re.compile(r"Creator email address", re.I))
            email_input.wait_for(state="visible", timeout=5000)
            email_value = (email_input.input_value() or "").strip()

            if email_value:
                results[media_id] = email_value

        except Exception as e:
            print(f"scrape_creator_emails: couldn't get email for media_id={media_id!r}: {e}")

        finally:
            # Always try to back out via Escape, regardless of success/
            # failure above -- never clicks anything to close, so this
            # can't accidentally submit anything either.
            try:
                page.keyboard.press("Escape")
                page.wait_for_timeout(300)
            except Exception:
                pass

    return results

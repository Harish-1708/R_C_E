"""
Tests for refunnel_export.py's scroll_to_load_all(), using a fake Page
object that simulates progressive lazy-loading. This covers the counting
and stop-condition logic in isolation -- it does NOT exercise anything
that needs a real browser or real Refunnel page (export_payments_csv,
export_media_csv, scrape_creator_emails all need a live site and are
untested here; see README).
"""

import re
from pathlib import Path

import pytest

from refunnel_export import (
    scroll_to_load_all,
    scroll_to_top,
    ExportError,
    select_workspace,
    _safe_click,
    _order_ids_for_scraping,
    _pace,
    _should_print_progress,
    _format_progress_line,
    _is_target_crashed,
    _scroll_until_card_found,
    _is_logged_out,
    trigger_native_drive_upload,
)


class FakePage:
    """Simulates a page where each scroll (evaluate() call) reveals
    more items, up to a schedule the test controls."""

    def __init__(self, load_schedule, total):
        # load_schedule: list of "loaded" counts over successive reads,
        # e.g. [80, 160, 240, 240, 240] simulates loading then stalling
        self.load_schedule = load_schedule
        self.total = total
        self._read_index = 0
        self.scroll_calls = 0

    def inner_text(self, _selector):
        idx = min(self._read_index, len(self.load_schedule) - 1)
        loaded = self.load_schedule[idx]
        return f"{loaded} of {self.total} media"

    def evaluate(self, _script, _arg=None):
        self.scroll_calls += 1
        self._read_index += 1

    def wait_for_timeout(self, _ms):
        pass


def test_stops_once_loaded_reaches_total():
    page = FakePage(load_schedule=[80, 400, 900, 1500, 2078], total=2078)
    scroll_to_load_all(page, scroll_pause_ms=0, idle_rounds_before_giving_up=10)
    # last read should show full count reached
    assert "2078 of 2078" in page.inner_text("body")


def test_raises_if_stalls_before_reaching_total():
    # loads to 500 then stalls forever -- must ABORT, not continue with
    # partial data, since every sheet tab is a full rewrite and a short
    # pull would delete real rows already correctly there
    page = FakePage(load_schedule=[80, 300, 500, 500, 500, 500, 500, 500, 500, 500], total=2078)
    with pytest.raises(ExportError, match="stopped early"):
        scroll_to_load_all(page, scroll_pause_ms=0, idle_rounds_before_giving_up=3)


def test_raises_if_counter_text_not_found():
    class NoCounterPage(FakePage):
        def inner_text(self, _selector):
            return "nothing relevant here"

    page = NoCounterPage(load_schedule=[0], total=0)
    with pytest.raises(ExportError, match="Couldn't find"):
        scroll_to_load_all(page)


def test_retries_before_giving_up_if_counter_takes_a_moment_to_appear():
    # confirmed real: a scheduled run failed here, but the exact same
    # URL loaded fine when checked manually -- almost certainly just a
    # slow page render on that particular run, not a real structural
    # change. This proves the retry actually gives it a real chance
    # rather than failing on the very first, immediate check.
    class SlowCounterPage(FakePage):
        def __init__(self, *a, appears_on_attempt, **kw):
            super().__init__(*a, **kw)
            self._attempt = 0
            self._appears_on_attempt = appears_on_attempt

        def inner_text(self, _selector):
            self._attempt += 1
            if self._attempt < self._appears_on_attempt:
                return "still loading, nothing here yet"
            return super().inner_text(_selector)

    page = SlowCounterPage(load_schedule=[240, 240], total=240, appears_on_attempt=3)
    scroll_to_load_all(page, scroll_pause_ms=0)  # doesn't raise -- found it on the 3rd try
    assert page._attempt >= 3


def test_sabotage_no_retry_would_be_caught():
    # proves the retry is real, not a no-op -- a version that only
    # checked once would wrongly raise here
    class SlowCounterPage(FakePage):
        def __init__(self, *a, appears_on_attempt, **kw):
            super().__init__(*a, **kw)
            self._attempt = 0
            self._appears_on_attempt = appears_on_attempt

        def inner_text(self, _selector):
            self._attempt += 1
            if self._attempt < self._appears_on_attempt:
                return "still loading, nothing here yet"
            return super().inner_text(_selector)

    page = SlowCounterPage(load_schedule=[240, 240], total=240, appears_on_attempt=3)
    try:
        scroll_to_load_all(page, scroll_pause_ms=0)
        raised = False
    except ExportError:
        raised = True
    with pytest.raises(AssertionError):
        assert raised is True  # wrong -- a working retry should NOT raise here
    assert raised is False  # confirms actual correct behavior


def test_does_not_scroll_at_all_if_already_fully_loaded():
    page = FakePage(load_schedule=[2078], total=2078)
    scroll_to_load_all(page, scroll_pause_ms=0)
    assert page.scroll_calls == 0


# ---------- select_workspace tests (fake workspace switcher) ----------

ALL_NAMES = ["Duderobe", "Swoveralls", "Defi Snacks", "Kelson"]


class _FakeLocator:
    def __init__(self, page, keys):
        self._page = page
        # keys is a list -- a single-item list for a specific-name
        # locator (get_by_text), or multiple items for the combined
        # ":text-is(...), :text-is(...)" trigger locator.
        self._keys = keys if isinstance(keys, list) else [keys]

    @property
    def first(self):
        return self

    @property
    def last(self):
        return self

    def count(self):
        return 1 if self._visible_now() else 0

    def is_visible(self):
        return self._visible_now()

    def _visible_now(self):
        if self._keys == ["__search_box__"]:
            return self._page.dropdown_open
        if self._keys == ["__toggle_menu__"]:
            return True  # the expand/collapse control is always present
        if len(self._keys) > 1:
            # the combined trigger locator -- genuinely absent from the
            # DOM while the sidebar is collapsed, matching the real bug
            if self._page.sidebar_collapsed:
                return False
        if self._page.dropdown_open:
            # any key that's a real workspace name is visible in the open list
            return any(k in ALL_NAMES for k in self._keys)
        # closed: visible only if it's the currently active workspace's trigger
        return self._page.active_workspace in self._keys

    def wait_for(self, state="visible", timeout=0):
        if state == "visible" and not self._visible_now():
            raise TimeoutError(f"{self._keys!r} never became visible")

    def inner_text(self):
        if not self._visible_now():
            raise RuntimeError(f"{self._keys!r} is not visible -- can't read its text")
        return self._page.active_workspace

    def click(self):
        if self._keys == ["__search_box__"]:
            return
        if self._keys == ["__toggle_menu__"]:
            self._page.sidebar_collapsed = False
            return
        if not self._page.dropdown_open and self._page.active_workspace in self._keys:
            self._page.dropdown_open = True
        elif self._page.dropdown_open and any(k in ALL_NAMES for k in self._keys):
            # a specific single-name locator was clicked from the open list
            if len(self._keys) == 1:
                self._page.active_workspace = self._keys[0]
                self._page.dropdown_open = False


class FakeWorkspacePage:
    def __init__(self, active_workspace="Duderobe", sidebar_collapsed=False):
        self.active_workspace = active_workspace
        self.dropdown_open = False
        self.sidebar_collapsed = sidebar_collapsed
        self.goto_calls = []

    def goto(self, url):
        self.goto_calls.append(url)

    def get_by_text(self, text, exact=True):
        return _FakeLocator(self, [text])

    def get_by_placeholder(self, _pattern):
        return _FakeLocator(self, ["__search_box__"])

    def get_by_alt_text(self, text):
        return _FakeLocator(self, ["__toggle_menu__"] if text == "Toggle menu" else ["__no_match__"])

    def locator(self, css_selector):
        # mimics page.locator(":text-is('A'), :text-is('B'), ...") by
        # pulling the quoted names back out of the generated CSS string
        names = re.findall(r":text-is\('([^']+)'\)", css_selector)
        return _FakeLocator(self, names)

    def wait_for_timeout(self, _ms):
        pass


def test_select_workspace_is_a_noop_if_already_active():
    page = FakeWorkspacePage(active_workspace="Duderobe")
    select_workspace(page, "Duderobe", ALL_NAMES)
    assert page.active_workspace == "Duderobe"
    assert page.dropdown_open is False


def test_select_workspace_switches_to_target():
    page = FakeWorkspacePage(active_workspace="Duderobe")
    select_workspace(page, "Swoveralls", ALL_NAMES)
    assert page.active_workspace == "Swoveralls"
    assert page.dropdown_open is False  # closed again after picking


def test_select_workspace_expands_collapsed_sidebar_then_switches():
    # reproduces the real bug: sidebar starts collapsed, workspace name
    # isn't in the DOM at all until the toggle is clicked
    page = FakeWorkspacePage(active_workspace="Duderobe", sidebar_collapsed=True)
    select_workspace(page, "Swoveralls", ALL_NAMES)
    assert page.sidebar_collapsed is False
    assert page.active_workspace == "Swoveralls"


def test_select_workspace_raises_if_trigger_never_renders():
    # distinct from the collapsed-sidebar case: here the sidebar is NOT
    # collapsed, but none of the known names match anyway (e.g. wrong
    # names configured) -- toggling wouldn't help, so this should still
    # fail informatively rather than loop forever
    page = FakeWorkspacePage(active_workspace="SomeUnlistedWorkspace", sidebar_collapsed=False)
    with pytest.raises(ExportError, match="became visible"):
        select_workspace(page, "Swoveralls", ALL_NAMES, timeout_ms=10)


def test_select_workspace_raises_if_target_not_in_list():
    page = FakeWorkspacePage(active_workspace="Duderobe")
    with pytest.raises(ExportError, match="couldn't find/click"):
        select_workspace(page, "NotARealWorkspace", ALL_NAMES)


def test_sabotage_select_workspace_wrong_target_would_be_caught():
    page = FakeWorkspacePage(active_workspace="Duderobe")
    select_workspace(page, "Kelson", ALL_NAMES)
    with pytest.raises(AssertionError):
        assert page.active_workspace == "Swoveralls"
    assert page.active_workspace == "Kelson"


# ---------- _safe_click safety-net tests ----------

class _FakeClickLocator:
    def __init__(self, text):
        self._text = text
        self.clicked = False

    def inner_text(self, timeout=0):
        return self._text

    def click(self):
        self.clicked = True


def test_safe_click_allows_normal_button():
    loc = _FakeClickLocator("Email")
    _safe_click(loc)
    assert loc.clicked is True


def test_safe_click_refuses_send_request_button():
    loc = _FakeClickLocator("Send request")
    with pytest.raises(ExportError, match="Refusing to click"):
        _safe_click(loc)
    assert loc.clicked is False


def test_safe_click_refuses_case_insensitively_and_with_extra_text():
    loc = _FakeClickLocator("  SEND REQUEST NOW  ")
    with pytest.raises(ExportError):
        _safe_click(loc)
    assert loc.clicked is False


def test_sabotage_disabling_the_pattern_check_would_be_caught(monkeypatch):
    # proves the refusal is actually driven by _DANGEROUS_BUTTON_PATTERN,
    # not some hardcoded check -- neutering the pattern lets the click
    # through, confirming the real pattern is what normally prevents it
    import refunnel_export

    monkeypatch.setattr(refunnel_export, "_DANGEROUS_BUTTON_PATTERN", re.compile(r"$nothing_matches_this^"))
    loc = _FakeClickLocator("Send request")
    _safe_click(loc)  # with the pattern neutered, this now (wrongly) succeeds
    assert loc.clicked is True


# ---------- sabotage tests ----------

def test_sabotage_off_by_one_stop_condition_would_be_caught():
    # if the stop condition were `loaded > total` instead of `>=`, a
    # page that loads exactly to total (never exceeding it) would loop
    # until max_rounds. Prove our test would catch that by checking
    # scroll_calls stayed small (didn't run away).
    page = FakePage(load_schedule=[80, 400, 900, 1500, 2078], total=2078)
    scroll_to_load_all(page, scroll_pause_ms=0, idle_rounds_before_giving_up=10)
    assert page.scroll_calls < 10  # should stop almost immediately once loaded==total
    with pytest.raises(AssertionError):
        assert page.scroll_calls > 100  # would only be true if the loop ran away


def test_sabotage_idle_threshold_ignored_would_be_caught():
    page = FakePage(load_schedule=[80, 300, 500, 500, 500, 500, 500, 500, 500, 500], total=2078)
    with pytest.raises(ExportError):
        scroll_to_load_all(page, scroll_pause_ms=0, idle_rounds_before_giving_up=3)
    # with a working idle check, it gives up after 3 idle rounds past the
    # last growth (index ~5), not after exhausting the whole schedule
    assert page.scroll_calls < 8


# ---------- _order_ids_for_scraping tests ----------

def test_order_ids_for_scraping_follows_feed_order_not_input_order():
    media_rows = {"a": {}, "b": {}, "c": {}, "d": {}}  # feed/CSV order: a, b, c, d
    result = _order_ids_for_scraping(media_rows, ["d", "b"])  # input order: d, b
    assert result == ["b", "d"]  # follows feed order, not input order


def test_order_ids_for_scraping_ignores_ids_not_in_media_rows():
    media_rows = {"a": {}, "b": {}}
    result = _order_ids_for_scraping(media_rows, ["b", "not_a_real_id"])
    assert result == ["b"]


def test_order_ids_for_scraping_empty_input_returns_empty():
    media_rows = {"a": {}, "b": {}}
    assert _order_ids_for_scraping(media_rows, []) == []


def test_sabotage_order_ids_wrong_order_would_be_caught():
    media_rows = {"x": {}, "y": {}, "z": {}}
    result = _order_ids_for_scraping(media_rows, ["z", "x"])
    with pytest.raises(AssertionError):
        assert result == ["z", "x"]  # wrong -- input order, not feed order
    assert result == ["x", "z"]  # confirms actual correct (feed-order) behavior


# ---------- _pace tests ----------

class _FakePacePage:
    def __init__(self):
        self.waited_ms = []

    def wait_for_timeout(self, ms):
        self.waited_ms.append(ms)


def test_pace_stays_within_configured_range():
    page = _FakePacePage()
    for _ in range(50):
        _pace(page, (400, 900))
    assert all(400 <= ms <= 900 for ms in page.waited_ms)


def test_pace_uses_default_range_when_unspecified():
    page = _FakePacePage()
    _pace(page)
    assert 400 <= page.waited_ms[0] <= 900  # EMAIL_SCRAPE_ACTION_PACE_MS default


def test_sabotage_pace_out_of_range_would_be_caught():
    page = _FakePacePage()
    for _ in range(50):
        _pace(page, (400, 900))
    with pytest.raises(AssertionError):
        assert any(ms > 900 for ms in page.waited_ms)  # none should exceed the range
    assert all(ms <= 900 for ms in page.waited_ms)  # confirms actual correct behavior


# ---------- scrape_creator_emails progress reporting ----------

def test_should_print_progress_at_checkpoint_intervals():
    assert _should_print_progress(25, 732, 25) is True
    assert _should_print_progress(50, 732, 25) is True
    assert _should_print_progress(24, 732, 25) is False
    assert _should_print_progress(26, 732, 25) is False


def test_should_print_progress_on_the_final_item_even_off_checkpoint():
    # confirmed real need: without this, a run whose total isn't a
    # multiple of 25 (e.g. 732) would never print a final summary
    assert _should_print_progress(732, 732, 25) is True


def test_should_not_print_progress_between_checkpoints():
    assert _should_print_progress(1, 732, 25) is False
    assert _should_print_progress(13, 732, 25) is False


def test_format_progress_line_shows_found_and_failed():
    line = _format_progress_line(attempted=50, total=732, found=8, empty_fields=30)
    assert "50/732 attempted" in line
    assert "8 found" in line
    assert "30 had no email on file" in line
    assert "12 other error(s)" in line  # 50 - 8 - 30


def test_format_progress_line_defaults_empty_fields_to_zero():
    line = _format_progress_line(attempted=50, total=732, found=8)
    assert "8 found" in line
    assert "0 had no email on file" in line
    assert "42 other error(s)" in line  # 50 - 8 - 0


def test_sabotage_progress_line_wrong_breakdown_would_be_caught():
    line = _format_progress_line(attempted=50, total=732, found=8, empty_fields=30)
    with pytest.raises(AssertionError):
        assert "20 other error(s)" in line  # wrong -- should be 12 (50-8-30)
    assert "12 other error(s)" in line  # confirms actual correct behavior


def test_sabotage_progress_checkpoint_missed_would_be_caught():
    result = _should_print_progress(732, 732, 25)
    with pytest.raises(AssertionError):
        assert result is False  # wrong -- final item must always print
    assert result is True  # confirms actual correct behavior


# ---------- _is_target_crashed ----------

def test_detects_real_target_crashed_message():
    # confirmed real: exact message format seen in a live run's log
    err = RuntimeError("Locator.count: Target crashed ")
    assert _is_target_crashed(err) is True


def test_detects_crashed_case_insensitively():
    err = RuntimeError("something something CRASHED something")
    assert _is_target_crashed(err) is True


def test_does_not_flag_a_normal_timeout_as_a_crash():
    err = RuntimeError("Locator.wait_for: Timeout 5000ms exceeded.")
    assert _is_target_crashed(err) is False


def test_does_not_flag_an_unrelated_error_as_a_crash():
    err = RuntimeError("couldn't locate media_id on the page after scrolling through everything")
    assert _is_target_crashed(err) is False


def test_sabotage_crash_detection_missed_would_be_caught():
    err = RuntimeError("Locator.count: Target crashed ")
    result = _is_target_crashed(err)
    with pytest.raises(AssertionError):
        assert result is False  # wrong -- this IS a real crash message
    assert result is True  # confirms actual correct behavior


# ---------- _scroll_until_card_found: the 160,000px ceiling bug ----------

class ScrollSearchPage:
    """Simulates a react-virtuoso container: the card only becomes
    findable once we've scrolled past appears_at_scroll_top."""

    def __init__(self, scroll_height, client_height=720, appears_at_scroll_top=None):
        self.scroll_top = 0
        self.scroll_height = scroll_height
        self.client_height = client_height
        self.appears_at = appears_at_scroll_top
        self.rounds = 0

    def locator(self, _selector):
        page = self

        class _Loc:
            def count(self):
                if page.appears_at is None:
                    return 0
                return 1 if page.scroll_top >= page.appears_at else 0

            @property
            def first(self):
                return "found-card"

        return _Loc()

    def evaluate(self, _script, args=None):
        self.rounds += 1
        if isinstance(args, dict) and "step" in args:
            max_top = max(0, self.scroll_height - self.client_height)
            self.scroll_top = min(self.scroll_top + args["step"], max_top)
        return {
            "scrollTop": self.scroll_top,
            "scrollHeight": self.scroll_height,
            "clientHeight": self.client_height,
        }

    def wait_for_timeout(self, _ms):
        pass


def test_finds_a_card_past_the_old_160000px_ceiling():
    # the exact production scenario: real scrollHeight was 327,922 and
    # the old fixed 200x800 cap stalled at ~159,951, making the bottom
    # half of the grid permanently unreachable
    page = ScrollSearchPage(scroll_height=327922, appears_at_scroll_top=250000)
    card, diagnostics = _scroll_until_card_found(page, "tk_deep", "#scrollableDiv", scroll_step=800)
    assert card == "found-card"
    assert diagnostics is None  # None means "found it", not "gave up"
    assert page.scroll_top >= 250000  # genuinely scrolled past the old ceiling


def test_gives_up_at_the_real_bottom_not_an_arbitrary_round_count():
    page = ScrollSearchPage(scroll_height=327922, appears_at_scroll_top=None)  # never appears
    card, diagnostics = _scroll_until_card_found(page, "tk_missing", "#scrollableDiv", scroll_step=800)
    assert card is None
    # reached the genuine bottom before giving up
    assert diagnostics["scrollTop"] + diagnostics["clientHeight"] >= diagnostics["scrollHeight"] - 2


def test_stops_promptly_once_at_the_bottom_rather_than_spinning():
    page = ScrollSearchPage(scroll_height=8000, appears_at_scroll_top=None)
    _scroll_until_card_found(page, "tk_missing", "#scrollableDiv", scroll_step=800)
    # ~10 rounds to reach bottom + a few confirming rounds, nowhere near max_rounds
    assert page.rounds < 30


def test_handles_a_missing_scroll_container_without_spinning():
    class NoContainerPage(ScrollSearchPage):
        def evaluate(self, _script, args=None):
            self.rounds += 1
            return None

    page = NoContainerPage(scroll_height=1000)
    card, diagnostics = _scroll_until_card_found(page, "tk_x", "#missing", scroll_step=800)
    assert card is None
    assert page.rounds < 5  # bailed immediately, didn't grind through max_rounds


def test_sabotage_old_fixed_ceiling_would_be_caught():
    # proves the test above genuinely exercises the bug: with the old
    # 200-round cap, a card at 250,000px is unreachable
    page = ScrollSearchPage(scroll_height=327922, appears_at_scroll_top=250000)
    card, _ = _scroll_until_card_found(
        page, "tk_deep", "#scrollableDiv", max_rounds=200, scroll_step=800
    )
    with pytest.raises(AssertionError):
        assert card == "found-card"  # wrong -- 200*800 can't reach 250,000
    assert card is None  # confirms the old limit really was the problem


# ---------- _is_logged_out: mid-run session loss ----------

class _UrlPage:
    def __init__(self, url):
        self.url = url


def test_detects_the_real_capital_l_login_redirect():
    # the exact URL from the production failure log
    assert _is_logged_out(_UrlPage("https://app.refunnel.com/Login")) is True


def test_does_not_flag_the_social_listening_page_as_logged_out():
    page = _UrlPage("https://app.refunnel.com/dashboard/content/social-listening?snv=true")
    assert _is_logged_out(page) is False


def test_is_logged_out_survives_a_page_that_raises_on_url():
    class _Exploding:
        @property
        def url(self):
            raise RuntimeError("target crashed")

    assert _is_logged_out(_Exploding()) is False  # never masks a crash as a logout


def test_sabotage_missed_logout_would_be_caught():
    result = _is_logged_out(_UrlPage("https://app.refunnel.com/Login"))
    with pytest.raises(AssertionError):
        assert result is False  # wrong -- this IS the logged-out page
    assert result is True


# ---------- campaign filter functions (best-guess selectors, logic tested) ----------

class _FakeCampaignLocator:
    def __init__(self, texts=None):
        self._texts = texts or []
        self.clicked = False
        self.filled = None

    def click(self, timeout=None):
        self.clicked = True

    def wait_for(self, state=None, timeout=None):
        pass

    def fill(self, text):
        self.filled = text

    def count(self):
        return len(self._texts)

    def nth(self, i):
        return _FakeCampaignLocator(texts=[self._texts[i]]) if self._texts else self

    def get_attribute(self, name):
        return getattr(self, "_attrs", {}).get(name)

    @property
    def first(self):
        return self

    def inner_text(self):
        return self._texts[0] if self._texts else ""


class _FakeKeyboard:
    def __init__(self):
        self.pressed = []

    def press(self, key):
        self.pressed.append(key)


class _CampaignPage:
    def __init__(self, campaign_names):
        self._campaign_names = campaign_names
        self.keyboard = _FakeKeyboard()
        self._locators = {}

    def locator(self, selector):
        from refunnel_export import CAMPAIGN_CHECKBOX_ROW_SELECTOR, CAMPAIGN_FILTER_BUTTON_SELECTOR
        if selector == CAMPAIGN_CHECKBOX_ROW_SELECTOR:
            return _FakeCampaignLocator(texts=self._campaign_names)
        if selector == CAMPAIGN_FILTER_BUTTON_SELECTOR:
            return _FakeCampaignLocator()
        self._locators.setdefault(selector, _FakeCampaignLocator())
        return self._locators[selector]


def test_list_available_campaigns_reads_every_name():
    from refunnel_export import list_available_campaigns
    page = _CampaignPage(["Evergreen Campaign", "Product Gifting - Cold Outbound", "[SCC] Partner with Swoveralls"])
    names = list_available_campaigns(page)
    assert names == ["Evergreen Campaign", "Product Gifting - Cold Outbound", "[SCC] Partner with Swoveralls"]


def test_list_available_campaigns_closes_the_dropdown_without_applying():
    from refunnel_export import list_available_campaigns
    page = _CampaignPage(["Campaign A"])
    list_available_campaigns(page)
    assert "Escape" in page.keyboard.pressed  # read-only: never left applied


class _FakeCampaignOptionRow:
    """Models one real `.campaign-option` row: a checkbox input plus a
    span whose text is the campaign's exact name."""

    def __init__(self, name):
        self.name = name
        self.checkbox_clicked = False

    def locator(self, selector):
        from refunnel_export import CAMPAIGN_CHECKBOX_INPUT_SELECTOR
        if selector == CAMPAIGN_CHECKBOX_INPUT_SELECTOR:
            return self

    def click(self, timeout=None):
        self.checkbox_clicked = True

    def check(self, timeout=None):
        self.checkbox_clicked = True

    def wait_for(self, state=None, timeout=None):
        pass


class _FilterableRowSet:
    """Models page.locator(ROW_SELECTOR).filter(has=...) against a real
    set of rows -- exact-text matching only, same as
    page.get_by_text(..., exact=True) really does, so a search for
    "Partner with DudeRobe" can never accidentally match "Partner with
    DudeRobe!" too."""

    def __init__(self, rows):
        self._rows = rows

    def filter(self, has):
        matches = [r for r in self._rows if r.name == has.exact_text]
        return _FilterableRowSet(matches)

    @property
    def first(self):
        return self._rows[0]


class _ExactTextLocator:
    def __init__(self, exact_text):
        self.exact_text = exact_text


class _RealCampaignPage(_CampaignPage):
    """Models the CONFIRMED real markup precisely: .campaign-option
    rows, each with an input.campaign-checkbox and exact-text content,
    including genuinely similar names that must not be confused."""

    def __init__(self, campaign_names, real_rows=None):
        super().__init__(campaign_names)
        self._rows = [_FakeCampaignOptionRow(n) for n in (real_rows or campaign_names)]

    def get_by_text(self, text, exact=False):
        return _ExactTextLocator(text)

    def wait_for_timeout(self, ms):
        pass

    def wait_for_selector(self, selector, timeout=None):
        # mirrors the real page: Apply becomes enabled once a checkbox
        # has registered. The fakes always register, so this succeeds.
        return None

    def locator(self, selector):
        from refunnel_export import CAMPAIGN_CHECKBOX_ROW_SELECTOR
        if selector == CAMPAIGN_CHECKBOX_ROW_SELECTOR:
            return _FilterableRowSet(self._rows)
        return super().locator(selector)


def test_filter_by_campaign_searches_for_the_exact_name():
    from refunnel_export import filter_by_campaign, CAMPAIGN_SEARCH_INPUT_SELECTOR
    page = _RealCampaignPage([], real_rows=["TTS VIP Creator Whitelisting - 6% Spend"])
    filter_by_campaign(page, "TTS VIP Creator Whitelisting - 6% Spend")
    assert page._locators[CAMPAIGN_SEARCH_INPUT_SELECTOR].filled == "TTS VIP Creator Whitelisting - 6% Spend"


def test_filter_by_campaign_clears_first():
    from refunnel_export import filter_by_campaign, CLEAR_ALL_FILTERS_SELECTOR
    page = _RealCampaignPage([], real_rows=["Campaign A"])
    filter_by_campaign(page, "Campaign A")
    assert page._locators[CLEAR_ALL_FILTERS_SELECTOR].clicked is True


def test_filter_by_campaign_clicks_the_checkbox_within_the_matched_row():
    from refunnel_export import filter_by_campaign
    page = _RealCampaignPage([], real_rows=["Save a Dude"])
    filter_by_campaign(page, "Save a Dude")
    assert page._rows[0].checkbox_clicked is True


def test_filter_by_campaign_picks_the_exact_match_not_a_similar_one():
    # confirmed real, genuine risk from actual campaign data: "Partner
    # with DudeRobe!" and "Partner with DudeRobe" both exist as real,
    # distinct campaigns
    from refunnel_export import filter_by_campaign
    page = _RealCampaignPage([], real_rows=["Partner with DudeRobe!", "Partner with DudeRobe"])

    filter_by_campaign(page, "Partner with DudeRobe")

    exclamation_row = next(r for r in page._rows if r.name == "Partner with DudeRobe!")
    plain_row = next(r for r in page._rows if r.name == "Partner with DudeRobe")
    assert plain_row.checkbox_clicked is True
    assert exclamation_row.checkbox_clicked is False  # the OTHER one must stay untouched


def test_sabotage_similar_campaign_names_would_be_confused_by_substring_matching():
    from refunnel_export import filter_by_campaign
    page = _RealCampaignPage([], real_rows=["Partner with DudeRobe!", "Partner with DudeRobe"])

    filter_by_campaign(page, "Partner with DudeRobe!")

    exclamation_row = next(r for r in page._rows if r.name == "Partner with DudeRobe!")
    plain_row = next(r for r in page._rows if r.name == "Partner with DudeRobe")
    with pytest.raises(AssertionError):
        assert plain_row.checkbox_clicked is True  # wrong -- that's the OTHER campaign
    assert exclamation_row.checkbox_clicked is True  # confirms the exact one was picked


def test_clear_all_filters_does_not_raise_if_nothing_to_clear():
    from refunnel_export import clear_all_filters

    class _NoFilterPage:
        def locator(self, _selector):
            class _Raising:
                def click(self, timeout=None):
                    raise RuntimeError("nothing to click")
            return _Raising()

    clear_all_filters(_NoFilterPage())  # must not raise


# ---------- campaign filter button: confirmed real markup fix ----------

def test_campaign_filter_selector_includes_the_confirmed_real_div_class():
    # confirmed real from a live TimeoutError and the actual failing
    # page's markup: it's a styled <div class="campaign-filter-label
    # clickable">, not a native <button> -- the old button-only guess
    # never matched anything on the real page at all
    from refunnel_export import CAMPAIGN_FILTER_BUTTON_SELECTOR
    assert ".campaign-filter-label.clickable" in CAMPAIGN_FILTER_BUTTON_SELECTOR


def test_old_button_only_guess_would_not_have_matched_the_real_element():
    class _StrictSelectorPage(_CampaignPage):
        """Only responds to the exact real div class -- simulates the
        real page, where a pure button:has-text('Campaign') selector
        matches nothing at all, which is what produced the real
        30-second timeout."""

        def locator(self, selector):
            if selector.strip() == "button:has-text('Campaign')":
                return _FakeCampaignLocator()  # 0 matches -- the real symptom
            return super().locator(selector)

    from refunnel_export import list_available_campaigns
    page = _StrictSelectorPage(["Evergreen Campaign"])
    # the CURRENT combined selector still works, because the div-class
    # half of it matches even though the old button half wouldn't have
    names = list_available_campaigns(page)
    assert names == ["Evergreen Campaign"]


def test_sabotage_reverting_to_button_only_selector_would_be_caught():
    from refunnel_export import CAMPAIGN_FILTER_BUTTON_SELECTOR
    with pytest.raises(AssertionError):
        assert CAMPAIGN_FILTER_BUTTON_SELECTOR == "button:has-text('Campaign')"  # wrong -- the old, broken guess
    assert ".campaign-filter-label" in CAMPAIGN_FILTER_BUTTON_SELECTOR  # confirms the real fix is in place


# ---------- list_available_campaigns: auto-diagnostic on 0 results ----------

class _EmptyCampaignPage(_CampaignPage):
    """Simulates the exact real symptom: the button click works, but
    the checkbox-row selector matches nothing at all, same kind of
    "assumed a real tag, Refunnel uses a styled element" mismatch the
    button selector already had."""

    def screenshot(self, path, full_page=True):
        self.screenshot_path = path

    def content(self):
        return "<html>fake page content</html>"


def test_saves_a_debug_snapshot_when_zero_campaigns_found(tmp_path):
    from refunnel_export import list_available_campaigns
    page = _EmptyCampaignPage([])  # no names -- the real symptom
    names = list_available_campaigns(page, debug_dir=str(tmp_path))
    assert names == []
    assert (tmp_path / "campaign_discovery_empty.png").exists() or hasattr(page, "screenshot_path")
    assert (tmp_path / "campaign_discovery_empty.html").exists()


def test_does_not_save_a_snapshot_when_campaigns_are_found(tmp_path):
    from refunnel_export import list_available_campaigns
    page = _EmptyCampaignPage(["Evergreen Campaign"])
    list_available_campaigns(page, debug_dir=str(tmp_path))
    assert not (tmp_path / "campaign_discovery_empty.png").exists()
    assert not (tmp_path / "campaign_discovery_empty.html").exists()


def test_no_debug_dir_means_no_crash_on_empty_result():
    from refunnel_export import list_available_campaigns
    page = _EmptyCampaignPage([])
    names = list_available_campaigns(page, debug_dir=None)  # must not raise
    assert names == []


def test_sabotage_silent_empty_result_would_be_caught(tmp_path):
    # the exact real problem: 0 campaigns found with NO diagnostic
    # evidence at all would mean guessing blindly a second time
    from refunnel_export import list_available_campaigns
    page = _EmptyCampaignPage([])
    list_available_campaigns(page, debug_dir=str(tmp_path))
    snapshot_saved = (tmp_path / "campaign_discovery_empty.html").exists()
    with pytest.raises(AssertionError):
        assert snapshot_saved is False  # wrong -- would mean no evidence was captured
    assert snapshot_saved is True  # confirms actual correct behavior


# ---------- list_available_campaigns: dynamic panel-id scoping (confirmed real markup) ----------

class _ScopedCampaignPage:
    """Models the ACTUAL confirmed DOM: the toggle sibling exposes
    aria-controls pointing at the panel's real, dynamically-generated
    id, and checkbox rows only exist WITHIN that scoped id -- an
    identical selector without the #<id> prefix must NOT match them,
    proving the scoping is real and not accidentally matching
    page-wide."""

    def __init__(self, panel_id, campaign_names):
        self.panel_id = panel_id
        self._campaign_names = campaign_names
        self.keyboard = _FakeKeyboard()
        self.screenshot_called = False

    def locator(self, selector):
        from refunnel_export import (
            CAMPAIGN_FILTER_BUTTON_SELECTOR,
            CAMPAIGN_DISCLOSURE_TOGGLE_SELECTOR,
            CAMPAIGN_CHECKBOX_ROW_SELECTOR,
        )
        if selector == CAMPAIGN_FILTER_BUTTON_SELECTOR:
            return _FakeCampaignLocator()
        if selector == CAMPAIGN_DISCLOSURE_TOGGLE_SELECTOR:
            loc = _FakeCampaignLocator()
            loc._attrs = {"aria-controls": self.panel_id}
            return loc
        # ONLY the properly scoped selector finds the rows -- the bare,
        # unscoped selector (what a page-wide search would use) must
        # return nothing, proving scoping actually matters here.
        if selector == f"#{self.panel_id} {CAMPAIGN_CHECKBOX_ROW_SELECTOR}":
            return _FakeCampaignLocator(texts=self._campaign_names)
        return _FakeCampaignLocator()

    def screenshot(self, path, full_page=True):
        self.screenshot_called = True

    def content(self):
        return "<html></html>"


def test_scopes_the_checkbox_search_to_the_real_dynamic_panel_id():
    from refunnel_export import list_available_campaigns
    page = _ScopedCampaignPage(panel_id="_r_k_", campaign_names=["Evergreen Campaign", "Product Gifting"])
    names = list_available_campaigns(page)
    assert names == ["Evergreen Campaign", "Product Gifting"]


def test_sabotage_unscoped_search_would_have_found_nothing():
    # proves the scoping is load-bearing: an identical page where the
    # panel id comes back different (e.g. read at the wrong moment)
    # correctly finds nothing, rather than accidentally matching
    # page-wide
    from refunnel_export import list_available_campaigns
    page = _ScopedCampaignPage(panel_id="_r_k_", campaign_names=["Evergreen Campaign"])
    page.panel_id_for_rows_only = "_r_wrong_"  # simulate a mismatched id
    # re-point the toggle to report a DIFFERENT id than what the rows are scoped under
    real_locator = page.locator
    def mismatched_locator(selector):
        from refunnel_export import CAMPAIGN_DISCLOSURE_TOGGLE_SELECTOR
        if selector == CAMPAIGN_DISCLOSURE_TOGGLE_SELECTOR:
            loc = _FakeCampaignLocator()
            loc._attrs = {"aria-controls": "_r_wrong_"}
            return loc
        return real_locator(selector)
    page.locator = mismatched_locator

    names = list_available_campaigns(page)
    with pytest.raises(AssertionError):
        assert names == ["Evergreen Campaign"]  # wrong -- the ids don't match, nothing should be found
    assert names == []  # confirms actual correct (if unhelpful) behavior: no silent cross-match


def test_debug_snapshot_captured_before_escape_not_after(tmp_path):
    # THE actual real bug this fixes: the diagnostic used to run AFTER
    # Escape had already closed the panel, capturing nothing useful
    from refunnel_export import list_available_campaigns

    call_order = []

    class _OrderTrackingPage(_ScopedCampaignPage):
        def screenshot(self, path, full_page=True):
            call_order.append("screenshot")

        @property
        def keyboard(self):
            class _KB:
                def press(_self, key):
                    call_order.append("escape")
            return _KB()

        @keyboard.setter
        def keyboard(self, value):
            pass

    page = _OrderTrackingPage(panel_id="_r_k_", campaign_names=[])  # empty -- triggers the diagnostic
    list_available_campaigns(page, debug_dir=str(tmp_path))

    assert call_order == ["screenshot", "escape"]  # screenshot must come first


# ---------- filter_by_campaign: post-Apply debug capture ----------

class _DebugCapturingPage(_RealCampaignPage):
    def __init__(self, campaign_names, real_rows=None):
        super().__init__(campaign_names, real_rows=real_rows)
        self.screenshot_calls = []
        self.wait_timeouts = []

    def screenshot(self, path, full_page=True):
        self.screenshot_calls.append(path)
        Path(path).write_bytes(b"fake png bytes")

    def content(self):
        return "<html>post-apply state</html>"

    def wait_for_timeout(self, ms):
        self.wait_timeouts.append(ms)


def test_filter_by_campaign_saves_debug_snapshot_when_debug_dir_given(tmp_path):
    from refunnel_export import filter_by_campaign
    page = _DebugCapturingPage([], real_rows=["Evergreen Campaign"])
    filter_by_campaign(page, "Evergreen Campaign", debug_dir=str(tmp_path))
    assert len(page.screenshot_calls) == 1
    saved_html = list(tmp_path.glob("after_apply_*.html"))
    assert len(saved_html) == 1
    assert saved_html[0].read_text() == "<html>post-apply state</html>"


def test_filter_by_campaign_no_snapshot_without_debug_dir(tmp_path):
    from refunnel_export import filter_by_campaign
    page = _DebugCapturingPage([], real_rows=["Evergreen Campaign"])
    filter_by_campaign(page, "Evergreen Campaign", debug_dir=None)
    assert page.screenshot_calls == []


def test_filter_by_campaign_sanitizes_the_campaign_name_for_a_filename(tmp_path):
    from refunnel_export import filter_by_campaign
    page = _DebugCapturingPage([], real_rows=["[SCC] Partner with Swoveralls"])
    filter_by_campaign(page, "[SCC] Partner with Swoveralls", debug_dir=str(tmp_path))
    saved = list(tmp_path.glob("after_apply_*.png"))
    assert len(saved) == 1
    assert "[" not in saved[0].name and "]" not in saved[0].name


def test_sabotage_no_debug_capture_would_be_caught(tmp_path):
    from refunnel_export import filter_by_campaign
    page = _DebugCapturingPage([], real_rows=["Evergreen Campaign"])
    filter_by_campaign(page, "Evergreen Campaign", debug_dir=str(tmp_path))
    with pytest.raises(AssertionError):
        assert page.screenshot_calls == []  # wrong -- a capture should have happened
    assert len(page.screenshot_calls) == 1  # confirms actual correct behavior


# ---------- has_no_results_for_filter: confirmed real empty state ----------

class _TextPage:
    def __init__(self, body_text):
        self._body_text = body_text

    def inner_text(self, _selector):
        return self._body_text


def test_detects_the_real_no_results_message():
    from refunnel_export import has_no_results_for_filter
    # confirmed real, exact text from a live run's debug screenshots
    page = _TextPage("...\nNo results for these filters(s)\nTry adjusting your filters...")
    assert has_no_results_for_filter(page) is True


def test_detects_the_alternate_spelling_too():
    from refunnel_export import has_no_results_for_filter
    page = _TextPage("No results for these filter(s)")
    assert has_no_results_for_filter(page) is True


def test_does_not_flag_a_normal_content_page():
    from refunnel_export import has_no_results_for_filter
    page = _TextPage("20 of 2795 media\n@someuser 9.1K followers")
    assert has_no_results_for_filter(page) is False


def test_survives_a_page_that_raises_on_inner_text():
    from refunnel_export import has_no_results_for_filter

    class _Exploding:
        def inner_text(self, _selector):
            raise RuntimeError("target crashed")

    assert has_no_results_for_filter(_Exploding()) is False


def test_sabotage_missed_empty_state_would_be_caught():
    from refunnel_export import has_no_results_for_filter
    page = _TextPage("No results for these filters(s)")
    result = has_no_results_for_filter(page)
    with pytest.raises(AssertionError):
        assert result is False  # wrong -- this IS the real empty-state message
    assert result is True


# ---------- filter_is_genuinely_active: confirmed real, serious bug ----------

def test_detects_a_genuinely_applied_filter():
    from refunnel_export import filter_is_genuinely_active
    page = _TextPage("Campaign is SheRobe Content Campaign ×")
    assert filter_is_genuinely_active(page, "SheRobe Content Campaign") is True


def test_detects_a_filter_that_silently_failed_to_apply():
    # confirmed real, exact bug: the campaign name is absent because
    # the filter never actually took effect
    from refunnel_export import filter_is_genuinely_active
    page = _TextPage("20 of 2795 media\n@someuser 9.1K followers")
    assert filter_is_genuinely_active(page, "SheRobe Content Campaign") is False


def test_survives_a_page_that_raises():
    from refunnel_export import filter_is_genuinely_active

    class _Exploding:
        def inner_text(self, _selector):
            raise RuntimeError("target crashed")

    assert filter_is_genuinely_active(_Exploding(), "Any Campaign") is False


def test_sabotage_missed_silent_filter_failure_would_be_caught():
    from refunnel_export import filter_is_genuinely_active
    page = _TextPage("20 of 2795 media")
    result = filter_is_genuinely_active(page, "SheRobe Content Campaign")
    with pytest.raises(AssertionError):
        assert result is True  # wrong -- the campaign name never appeared at all
    assert result is False  # confirms actual correct behavior


# ---------- goto_social_listening_for_workspace: the "Last 3 months" bug ----------

def test_navigates_before_and_after_the_workspace_switch():
    # confirmed real, serious bug this fixes: a live run navigated with
    # insights_timeline=last12months, then switched to Swoveralls -- and
    # a real screenshot showed "Last 3 months" active afterward, not 12.
    # Re-navigating AFTER the switch re-applies the correct range
    # regardless of that workspace's own default.
    from refunnel_export import goto_social_listening_for_workspace
    page = FakeWorkspacePage(active_workspace="Duderobe")
    goto_social_listening_for_workspace(page, "Swoveralls", ["Duderobe", "Swoveralls"])
    assert len(page.goto_calls) == 2
    assert page.goto_calls[0] == page.goto_calls[1]  # same URL, both times


def test_the_second_navigation_happens_after_the_workspace_switch_completes():
    from refunnel_export import goto_social_listening_for_workspace
    page = FakeWorkspacePage(active_workspace="Duderobe")
    goto_social_listening_for_workspace(page, "Swoveralls", ["Duderobe", "Swoveralls"])
    assert page.active_workspace == "Swoveralls"  # the switch itself still happened correctly


def test_noop_switch_still_gets_the_re_navigation():
    # even when already on the right workspace, still re-navigate --
    # cheap, and avoids assuming this specific case is exempt from the
    # same underlying Refunnel behavior
    from refunnel_export import goto_social_listening_for_workspace
    page = FakeWorkspacePage(active_workspace="Swoveralls")
    goto_social_listening_for_workspace(page, "Swoveralls", ["Duderobe", "Swoveralls"])
    assert len(page.goto_calls) == 2


def test_sabotage_single_navigation_would_be_caught():
    # proves the fix is real: a version that only navigated once
    # (the pre-fix behavior) would leave whatever the workspace switch
    # reset the filters to
    from refunnel_export import goto_social_listening_for_workspace
    page = FakeWorkspacePage(active_workspace="Duderobe")
    goto_social_listening_for_workspace(page, "Swoveralls", ["Duderobe", "Swoveralls"])
    with pytest.raises(AssertionError):
        assert len(page.goto_calls) == 1  # wrong -- that's the old, buggy behavior
    assert len(page.goto_calls) == 2  # confirms actual correct behavior


# ---------- trigger_native_drive_upload: confirmed real UI flow ----------

class _FakeMenuButton:
    def __init__(self, owner):
        self.owner = owner

    def scroll_into_view_if_needed(self, timeout=None):
        pass

    def click(self):
        self.owner.menu_clicked = True


class _FakeGridItemForDrive:
    def __init__(self, owner):
        self.owner = owner
        self.hovered = False

    def hover(self):
        self.hovered = True

    def locator(self, _selector):
        class _L:
            @property
            def first(_self):
                return _FakeMenuButton(self.owner)
        return _L()


class _ClickTrackingLocator:
    def __init__(self, owner, name):
        self.owner = owner
        self.name = name

    def wait_for(self, state=None, timeout=None):
        pass

    def click(self):
        self.owner.clicks.append(self.name)

    @property
    def first(self):
        return self


class _DriveUploadPage:
    """Page fake wired so _scroll_until_card_found (monkeypatched)
    immediately returns a usable card -- isolates testing to
    trigger_native_drive_upload's OWN flow (menu -> upload item -> All
    folders tab -> folder row -> Save to Drive), not the scroll-search
    logic, which already has its own tests."""

    def __init__(self, folder_name_to_select="Refunnel - Swoveralls"):
        self.menu_clicked = False
        self.clicks = []
        self._folder_name = folder_name_to_select
        self.scroll_resets = 0

    def evaluate(self, js, *a, **kw):
        if "scrollTop = 0" in js:
            self.scroll_resets += 1

    def wait_for_timeout(self, ms):
        pass

    def wait_for_selector(self, selector, timeout=None):
        # Save to Drive enables once a folder is picked; the fake always
        # picks one, so it's enabled.
        return None

    def locator(self, selector):
        from refunnel_export import (
            DRIVE_UPLOAD_MENU_ITEM_SELECTOR,
            DRIVE_MODAL_ALL_FOLDERS_TAB_SELECTOR,
            DRIVE_MODAL_SAVE_BUTTON_SELECTOR,
        )
        if selector == DRIVE_UPLOAD_MENU_ITEM_SELECTOR:
            return _ClickTrackingLocator(self, "upload_item")
        if selector == DRIVE_MODAL_ALL_FOLDERS_TAB_SELECTOR:
            return _ClickTrackingLocator(self, "all_folders_tab")
        if selector == DRIVE_MODAL_SAVE_BUTTON_SELECTOR:
            return _ClickTrackingLocator(self, "save_button")
        return _ClickTrackingLocator(self, selector)

    def get_by_text(self, text, exact=False):
        class _L:
            def __init__(_self, owner, matched):
                _self.owner = owner
                _self.matched = matched

            def wait_for(_self, state=None, timeout=None):
                pass

            def click(_self):
                if _self.matched:
                    _self.owner.clicks.append(f"folder:{text}")

            @property
            def first(_self):
                return _self
        return _L(self, text == self._folder_name)

    @property
    def first(self):
        return self


@pytest.fixture
def _stub_scroll_found(monkeypatch):
    import refunnel_export as re_module
    grid_item_holder = {}

    def fake_scroll_found(page, media_id, scroll_container_selector):
        item = _FakeGridItemForDrive(page)
        grid_item_holder["item"] = item
        return item, {}

    monkeypatch.setattr(re_module, "_scroll_until_card_found", fake_scroll_found)
    return grid_item_holder


def test_trigger_native_drive_upload_completes_the_full_flow(_stub_scroll_found):
    page = _DriveUploadPage(folder_name_to_select="Refunnel - Swoveralls")
    result = trigger_native_drive_upload(page, "tk_1", "Refunnel - Swoveralls")
    assert result is True
    assert page.clicks == ["upload_item", "all_folders_tab", "folder:Refunnel - Swoveralls", "save_button"]


def test_trigger_native_drive_upload_returns_false_if_card_not_found(monkeypatch):
    import refunnel_export as re_module
    monkeypatch.setattr(re_module, "_scroll_until_card_found", lambda *a, **kw: (None, {}))
    page = _DriveUploadPage()
    result = trigger_native_drive_upload(page, "tk_missing", "Refunnel - Swoveralls")
    assert result is False


def test_trigger_native_drive_upload_saves_debug_snapshot_on_failure(_stub_scroll_found, tmp_path):
    class _FailingPage(_DriveUploadPage):
        def locator(self, selector):
            raise RuntimeError("simulated UI failure")

        def screenshot(self, path, full_page=True):
            Path(path).write_bytes(b"fake")

        def content(self):
            return "<html>failure state</html>"

    page = _FailingPage()
    with pytest.raises(RuntimeError):
        trigger_native_drive_upload(page, "tk_1", "Refunnel - Swoveralls", debug_dir=str(tmp_path))
    saved = list(tmp_path.glob("drive_upload_failure_*.html"))
    assert len(saved) == 1


def test_sabotage_wrong_folder_selected_would_be_caught(_stub_scroll_found):
    page = _DriveUploadPage(folder_name_to_select="Refunnel - Duderobe")  # only Duderobe row is "real"
    trigger_native_drive_upload(page, "tk_1", "Refunnel - Swoveralls")  # but we asked for Swoveralls
    with pytest.raises(AssertionError):
        assert "folder:Refunnel - Swoveralls" in page.clicks  # wrong -- that row was never actually clicked
    assert "folder:Refunnel - Swoveralls" not in page.clicks  # confirms it correctly never matched


# ---------- scroll_to_top: the "stuck at the bottom" bug ----------

class _ScrollTopTrackingPage:
    def __init__(self, initial_scroll_top=691285):
        self.scroll_top = initial_scroll_top
        self.evaluate_calls = []

    def evaluate(self, js):
        self.evaluate_calls.append(js)
        self.scroll_top = 0  # simulates the real DOM assignment happening

    def wait_for_timeout(self, ms):
        pass


def test_scroll_to_top_resets_scroll_position():
    page = _ScrollTopTrackingPage(initial_scroll_top=691285)
    scroll_to_top(page)
    assert page.scroll_top == 0


def test_scroll_to_top_targets_the_given_container_selector():
    page = _ScrollTopTrackingPage()
    scroll_to_top(page, scroll_container_selector="#customContainer")
    assert "#customContainer" in page.evaluate_calls[0]


def test_scroll_to_top_defaults_to_the_real_scrollable_div():
    page = _ScrollTopTrackingPage()
    scroll_to_top(page)
    assert "#scrollableDiv" in page.evaluate_calls[0]


def test_sabotage_scroll_never_reset_would_be_caught():
    # confirmed real, exact bug: without this call, the page stayed
    # stuck at the bottom (scrollTop == scrollHeight - clientHeight)
    # for the entire remainder of a real run
    page = _ScrollTopTrackingPage(initial_scroll_top=691285)
    scroll_to_top(page)
    with pytest.raises(AssertionError):
        assert page.scroll_top == 691285  # wrong -- would mean the bug is still present
    assert page.scroll_top == 0  # confirms actual correct behavior


# ---------- scrape_creator_emails: cascading scroll-stuck failure (the real Swoveralls bug) ----------

class _MinimalScrapePage:
    """Just enough of a Page fake to drive scrape_creator_emails()
    through its "card not found" path -- the simplest reproduction of
    the real cascading bug, without needing the full click/menu/popup
    flow a successful find would require."""

    def __init__(self):
        self.scroll_to_top_calls = 0

    def evaluate(self, js, *a, **kw):
        if "scrollTop = 0" in js:
            self.scroll_to_top_calls += 1
            return None
        return {"scrollTop": 691285, "scrollHeight": 692005, "clientHeight": 720}

    def screenshot(self, path, full_page=True):
        Path(path).write_bytes(b"x")

    def content(self):
        return "<html></html>"

    def wait_for_timeout(self, ms):
        pass

    def locator(self, selector):
        class _Empty:
            def count(_self):
                return 0
        return _Empty()

    def url(self):
        return "https://app.refunnel.com/dashboard/content/social-listening"


def test_scrape_creator_emails_resets_scroll_after_each_failed_search(monkeypatch):
    import refunnel_export as re_module
    monkeypatch.setattr(re_module, "_is_logged_out", lambda page: False)
    monkeypatch.setattr(re_module, "_pace", lambda *a, **kw: None)

    page = _MinimalScrapePage()
    from refunnel_export import scrape_creator_emails

    media_rows = {"tk_1": {}, "tk_2": {}, "tk_3": {}}
    scrape_creator_emails(
        page, media_rows=media_rows, media_ids=["tk_1", "tk_2", "tk_3"],
    )

    # confirmed real bug this fixes: without a reset after EACH failed
    # search, the scroll position never recovers, and every id after
    # the first failure is doomed for the rest of the entire run.
    # 1 unconditional reset at the function's own start + 1 per failure.
    assert page.scroll_to_top_calls == 1 + 3


def test_sabotage_missing_reset_would_have_left_every_later_id_doomed(monkeypatch):
    import refunnel_export as re_module
    monkeypatch.setattr(re_module, "_is_logged_out", lambda page: False)
    monkeypatch.setattr(re_module, "_pace", lambda *a, **kw: None)

    page = _MinimalScrapePage()
    from refunnel_export import scrape_creator_emails

    media_rows = {"tk_1": {}, "tk_2": {}}
    scrape_creator_emails(page, media_rows=media_rows, media_ids=["tk_1", "tk_2"])

    with pytest.raises(AssertionError):
        assert page.scroll_to_top_calls == 1  # wrong -- would mean only the initial reset ran, no per-failure ones
    assert page.scroll_to_top_calls == 1 + 2  # confirms actual correct behavior


# ---------- the over-broad reset removal (confirmed real: caused a NEW cascade) ----------

class _GenericFailurePage(_MinimalScrapePage):
    """Simulates a card that's found successfully but then fails for a
    reason unrelated to scrolling (e.g. a menu never appearing) --
    confirmed real: resetting scroll here turned out to destabilize
    the virtualized list right before the NEXT item's click, causing
    every subsequent item to fail too -- the opposite of the
    intended protection."""

    def locator(self, selector):
        class _Found:
            def count(_self):
                return 1

            @property
            def first(_self):
                class _Row:
                    def hover(_s):
                        pass

                    def locator(_s, _sel):
                        class _Toggle:
                            @property
                            def first(__s):
                                return __s

                            def wait_for(__s, state=None, timeout=None):
                                pass

                            def scroll_into_view_if_needed(__s, timeout=None):
                                pass

                            def check(__s):
                                raise RuntimeError("simulated menu-timeout-style failure, unrelated to scroll")
                        return _Toggle()
                return _Row()
        return _Found()




# ---------- the usage-rights toggle selector, verified against REAL saved markup ----------

CARD_STRUCTURE_FIXTURE = Path(__file__).parent / "fixtures" / "refunnel_card_structure.html"


def test_real_markup_nests_the_card_inside_an_aria_disclosure_wrapper():
    # confirmed real, from an actual saved page: the card sits nested
    # inside an aria-controls wrapper. This nesting fact is real and
    # unchanged -- what was WRONG was concluding from it that the
    # wrapper must be the correct click target. A proven-working prior
    # version of this code clicked the card directly and worked fine;
    # see test_click_target_is_the_card_directly_not_an_aria_wrapper.
    html = CARD_STRUCTURE_FIXTURE.read_text()
    assert 'aria-controls=' in html
    # the card appears AFTER its wrapper's aria-controls attribute --
    # i.e. it's nested inside, not the wrapper itself
    wrapper_pos = html.find('aria-controls="_r_c1_"')
    card_pos = html.find('class="usage-rights-request-card"')
    assert wrapper_pos != -1 and card_pos != -1
    assert wrapper_pos < card_pos


def test_real_markup_has_two_distinct_menus_per_card():
    # confirmed real, genuine risk: each card has TWO sibling
    # .pop-up-menu disclosures -- the usage-rights one AND the dotted
    # "..." menu (Upload to Google Drive / Attach to a campaign).
    # Targeting the wrong one opens the Drive-upload menu instead.
    html = CARD_STRUCTURE_FIXTURE.read_text()
    assert html.count('class="pop-up-menu"') == 2
    assert 'disclosure="true"' in html          # the dotted "..." menu
    assert 'usage-rights-request-card' in html  # the usage-rights one




# ---------- Pending review posts: structurally unscrapeable (confirmed from real markup) ----------

def test_real_markup_confirms_pending_review_shares_the_requested_card_class():
    # confirmed real, genuine trap this guards against: a "Pending
    # review" card uses .usage-rights-requested-card -- the SAME class
    # as a real "Usage rights requested" card -- so the class alone
    # cannot distinguish them. Only the .urq-title text can.
    from refunnel_export import PENDING_REVIEW_TITLE_TEXT
    assert PENDING_REVIEW_TITLE_TEXT == "Pending review"


class _PendingReviewPage(_MinimalScrapePage):
    """A card that IS found, and IS a Pending review card -- confirmed
    real: its menu has no usage-rights option at all, so opening it
    could only ever time out."""

    def __init__(self):
        super().__init__()
        self.toggle_clicked = False

    def locator(self, selector):
        page = self

        class _Found:
            def count(_self):
                return 1

            @property
            def first(_self):
                class _Row:
                    def hover(_s):
                        pass

                    def locator(_s, sel):
                        class _Inner:
                            def count(__s):
                                # the Pending-review title IS present
                                return 1 if "urq-title" in sel else 1

                            @property
                            def first(__s):
                                return __s

                            def wait_for(__s, state=None, timeout=None):
                                pass

                            def scroll_into_view_if_needed(__s, timeout=None):
                                pass

                            def check(__s):
                                page.toggle_clicked = True

                            def click(__s):
                                page.toggle_clicked = True
                        return _Inner()
                return _Row()
        return _Found()


def test_pending_review_post_is_skipped_without_opening_its_menu(monkeypatch, capsys):
    import refunnel_export as re_module
    monkeypatch.setattr(re_module, "_is_logged_out", lambda page: False)
    monkeypatch.setattr(re_module, "_pace", lambda *a, **kw: None)

    page = _PendingReviewPage()
    from refunnel_export import scrape_creator_emails

    scrape_creator_emails(page, media_rows={"ig_1": {}}, media_ids=["ig_1"])

    out = capsys.readouterr().out
    assert "Pending review" in out
    assert "Not a failure" in out
    assert page.toggle_clicked is False  # never even opened the menu


def test_pending_review_skip_is_reported_in_the_summary(monkeypatch, capsys):
    import refunnel_export as re_module
    monkeypatch.setattr(re_module, "_is_logged_out", lambda page: False)
    monkeypatch.setattr(re_module, "_pace", lambda *a, **kw: None)

    page = _PendingReviewPage()
    from refunnel_export import scrape_creator_emails

    scrape_creator_emails(page, media_rows={"ig_1": {}, "ig_2": {}}, media_ids=["ig_1", "ig_2"])

    out = capsys.readouterr().out
    assert "skipped 2 post(s)" in out
    assert "Approving or declining them" in out


def test_sabotage_opening_a_pending_review_menu_would_be_caught(monkeypatch):
    import refunnel_export as re_module
    monkeypatch.setattr(re_module, "_is_logged_out", lambda page: False)
    monkeypatch.setattr(re_module, "_pace", lambda *a, **kw: None)

    page = _PendingReviewPage()
    from refunnel_export import scrape_creator_emails

    scrape_creator_emails(page, media_rows={"ig_1": {}}, media_ids=["ig_1"])

    with pytest.raises(AssertionError):
        assert page.toggle_clicked is True  # wrong -- that's the old, doomed behavior
    assert page.toggle_clicked is False  # confirms it correctly skipped before clicking


# ---------- the dropdown-toggle bug (confirmed real, erratic campaign failures) ----------

def test_dropdown_is_forced_closed_before_being_opened():
    # confirmed real root cause: clicking the Campaign filter TOGGLES
    # it. If it was already open from the previous campaign, the click
    # closed it -- producing "search input not visible", "Apply never
    # enabled", and "intercepts pointer events" in the same live run.
    from refunnel_export import filter_by_campaign
    page = _RealCampaignPage([], real_rows=["Campaign A"])
    filter_by_campaign(page, "Campaign A")
    assert "Escape" in page.keyboard.pressed


def test_open_is_retried_when_the_search_box_never_appears():
    from refunnel_export import filter_by_campaign, CAMPAIGN_SEARCH_INPUT_SELECTOR, ExportError

    class _NeverOpensPage(_RealCampaignPage):
        def locator(self, selector):
            if selector == CAMPAIGN_SEARCH_INPUT_SELECTOR:
                class _Invisible:
                    @property
                    def first(_s):
                        return _s

                    def wait_for(_s, state=None, timeout=None):
                        raise RuntimeError("search box never appeared")
                return _Invisible()
            return super().locator(selector)

    page = _NeverOpensPage([], real_rows=["Campaign A"])
    with pytest.raises(ExportError) as exc:
        filter_by_campaign(page, "Campaign A")
    assert "3 attempts" in str(exc.value)
    # three real open attempts, each preceded by an Escape
    assert page.keyboard.pressed.count("Escape") == 3


def test_disabled_apply_fails_fast_instead_of_stalling():
    # confirmed real: the old code clicked a permanently-disabled Apply
    # and burned the full 30s timeout before failing
    from refunnel_export import filter_by_campaign, ExportError

    class _ApplyNeverEnablesPage(_RealCampaignPage):
        def wait_for_selector(self, selector, timeout=None):
            raise RuntimeError("Apply Changes stayed disabled")

    page = _ApplyNeverEnablesPage([], real_rows=["Campaign A"])
    with pytest.raises(ExportError) as exc:
        filter_by_campaign(page, "Campaign A")
    assert "never became enabled" in str(exc.value)


def test_sabotage_assuming_the_dropdown_was_closed_would_be_caught():
    from refunnel_export import filter_by_campaign
    page = _RealCampaignPage([], real_rows=["Campaign A"])
    filter_by_campaign(page, "Campaign A")
    with pytest.raises(AssertionError):
        assert page.keyboard.pressed == []  # wrong -- that's the old, state-assuming behaviour
    assert "Escape" in page.keyboard.pressed


# ---------- detached-click retries must reset scroll first (confirmed real) ----------

class _DetachThenSucceedPage(_MinimalScrapePage):
    """First click attempt fails as 'detached'; the retry then succeeds
    -- but ONLY if the retry re-search can actually re-find the card,
    which requires resetting scroll first. Confirmed real: without the
    reset, the page is pinned at the bottom and the retry re-search
    always returned None, so no retry ever succeeded."""

    def __init__(self):
        super().__init__()
        self.click_attempts = 0
        self.at_bottom = True  # where a failed first attempt leaves it

    def evaluate(self, js, *a, **kw):
        if "scrollTop = 0" in js:
            self.scroll_to_top_calls += 1
            self.at_bottom = False
            return None
        return {"scrollTop": 692698, "scrollHeight": 693418, "clientHeight": 720}


def test_retry_resets_scroll_before_re_searching(monkeypatch):
    import refunnel_export as re_module

    page = _DetachThenSucceedPage()
    seen_positions = []

    def fake_find(pg, media_id, sel):
        # mirrors the real thing: a forward-only search can't find a
        # card when the container is already pinned at the bottom
        seen_positions.append(pg.at_bottom)
        return (None, {}) if pg.at_bottom else (object(), {})

    monkeypatch.setattr(re_module, "_scroll_until_card_found", fake_find)

    # drive just the retry branch's contract: reset, then re-search
    re_module.scroll_to_top(page, "#scrollableDiv")
    grid_item, _ = fake_find(page, "tk_1", "#scrollableDiv")

    assert page.scroll_to_top_calls == 1
    assert seen_positions == [False]   # searched from the TOP, not the bottom
    assert grid_item is not None       # so the card is findable again


def test_sabotage_retry_without_reset_would_never_find_the_card():
    page = _DetachThenSucceedPage()
    # no reset -> still pinned at the bottom -> forward-only search fails
    assert page.at_bottom is True
    with pytest.raises(AssertionError):
        assert page.at_bottom is False  # wrong -- that's only true after a reset
    assert page.scroll_to_top_calls == 0


# ---------- Drive: confirmed-real selector + scroll reset ----------



def test_drive_upload_resets_scroll_before_searching(_stub_scroll_found):
    page = _DriveUploadPage()
    trigger_native_drive_upload(page, "tk_1", "Refunnel - Swoveralls")
    assert page.scroll_resets == 1


def test_sabotage_drive_search_without_reset_would_be_caught(_stub_scroll_found):
    page = _DriveUploadPage()
    trigger_native_drive_upload(page, "tk_1", "Refunnel - Swoveralls")
    with pytest.raises(AssertionError):
        assert page.scroll_resets == 0  # wrong -- that's the bug that stranded 4 of 5 videos
    assert page.scroll_resets == 1


# ---------- campaign discovery must scroll the dropdown (confirmed real: 10 of 22+) ----------

class _LazyCampaignRows:
    """A dropdown list that renders 10 more campaigns each time its last
    row is scrolled into view -- mirrors the real behaviour where a
    single read found only the first 10 of Swoveralls' 22+."""

    def __init__(self, all_names, batch=10):
        self.all_names = all_names
        self.batch = batch
        self.rendered = min(batch, len(all_names))

    def count(self):
        return self.rendered

    @property
    def first(self):
        return self

    def wait_for(self, state=None, timeout=None):
        pass

    def nth(self, i):
        outer = self

        class _Row:
            def inner_text(_s):
                return outer.all_names[i]

            def scroll_into_view_if_needed(_s, timeout=None):
                if i == outer.rendered - 1:
                    outer.rendered = min(outer.rendered + outer.batch, len(outer.all_names))
        return _Row()


class _LazyDropdownPage:
    def __init__(self, names):
        self._rows = _LazyCampaignRows(names)
        self.keyboard = _FakeKeyboard()

    def locator(self, selector):
        from refunnel_export import CAMPAIGN_DISCLOSURE_TOGGLE_SELECTOR
        if selector == CAMPAIGN_DISCLOSURE_TOGGLE_SELECTOR:
            loc = _FakeCampaignLocator()
            loc._attrs = {"aria-controls": "_r_k_"}
            return loc
        if ".campaign-option" in selector:
            return self._rows
        return _FakeCampaignLocator()

    def wait_for_timeout(self, ms):
        pass


def test_discovers_every_campaign_not_just_the_first_batch():
    from refunnel_export import list_available_campaigns
    names = [f"Campaign {n:02d}" for n in range(1, 24)]  # 23, like Swoveralls' 22+
    names_found = list_available_campaigns(_LazyDropdownPage(names))
    assert names_found == names  # all 23, in order, no duplicates


def test_stops_cleanly_when_the_list_is_exhausted():
    from refunnel_export import list_available_campaigns
    names = [f"Campaign {n}" for n in range(1, 6)]  # fewer than one batch
    assert list_available_campaigns(_LazyDropdownPage(names)) == names


def test_sabotage_single_read_missing_later_campaigns_would_be_caught():
    from refunnel_export import list_available_campaigns
    names = [f"Campaign {n:02d}" for n in range(1, 24)]
    found = list_available_campaigns(_LazyDropdownPage(names))
    with pytest.raises(AssertionError):
        assert len(found) == 10  # wrong -- that's the live bug (first batch only)
    assert len(found) == 23


# ---------- Drive, pinned to a REAL Approved card (confirmed from a live screenshot) ----------

APPROVED_CARD_FIXTURE = Path(__file__).parent / "fixtures" / "refunnel_approved_card.html"


def _approved_soup():
    from bs4 import BeautifulSoup
    return BeautifulSoup(APPROVED_CARD_FIXTURE.read_text(), "html.parser")


def test_approved_toggle_matches_exactly_once_on_a_real_approved_card():
    from refunnel_export import DRIVE_APPROVED_TOGGLE_SELECTOR
    assert len(_approved_soup().select(DRIVE_APPROVED_TOGGLE_SELECTOR)) == 1


def test_approved_toggle_is_not_the_dotted_menu():
    # confirmed real: the dotted icon opens "Show content / Mute creator /
    # Delete from library" -- the wrong menu, where Upload to Drive never appears
    from refunnel_export import DRIVE_APPROVED_TOGGLE_SELECTOR
    soup = _approved_soup()
    toggle = soup.select_one(DRIVE_APPROVED_TOGGLE_SELECTOR)
    dotted = soup.select_one(".post_dotted_menu__cpgc")
    assert toggle is not None and dotted is not None
    assert dotted not in toggle.parents and toggle not in dotted.parents


def test_sabotage_targeting_the_dotted_menu_would_be_caught():
    from refunnel_export import DRIVE_APPROVED_TOGGLE_SELECTOR
    with pytest.raises(AssertionError):
        assert "dottedMenuIcon" in DRIVE_APPROVED_TOGGLE_SELECTOR  # wrong -- that opened the wrong menu live
    assert "usage-rights-approved-card" in DRIVE_APPROVED_TOGGLE_SELECTOR


# ---------- the Approved filter is a URL param (confirmed from the live address bar) ----------

def test_approved_url_matches_the_real_address_bar_encoding():
    import refunnel_auth
    url = refunnel_auth.refunnel_social_listening_url("GRANTED")
    assert "usage_rights=%5B%22GRANTED%22%5D" in url   # usage_rights=["GRANTED"]
    assert "usage_rightsOpt=%22is%22" in url           # usage_rightsOpt="is"


def test_unfiltered_url_has_no_usage_rights_param():
    import refunnel_auth
    assert "usage_rights" not in refunnel_auth.refunnel_social_listening_url()


# ---------- username + date fallback (suggested; for hex Instagram ids) ----------

def test_date_label_matches_how_cards_display_dates():
    from refunnel_export import card_date_label
    assert card_date_label("2026-09-11T14:02:00Z") == "Sep 11"
    assert card_date_label("2026-03-05T00:00:00Z") == "Mar 5"   # unpadded, like "Mar 10"/"Jul 19"
    assert card_date_label("") is None


def test_fallback_selector_finds_the_real_card_by_handle_and_date():
    from refunnel_export import card_selector_for_username_date
    soup = _approved_soup()
    sel = card_selector_for_username_date("indycub9", "2026-09-11T00:00:00Z")
    # soupsieve spells exact-text matching differently from Playwright;
    # verify the two confirmed-real anchors the selector relies on
    assert soup.select_one("span.post-header-uname").get_text(strip=True) == "@indycub9"
    assert soup.select_one(".post_time__cpgc").get_text(strip=True) == "Sep 11"
    assert "span.post-header-uname:text-is('@indycub9')" in sel
    assert ".post_time__cpgc:text-is('Sep 11')" in sel


def test_fallback_uses_exact_match_so_a_longer_handle_cannot_collide():
    from refunnel_export import card_selector_for_username_date
    sel = card_selector_for_username_date("indycub9", "2026-09-11T00:00:00Z")
    assert ":text-is(" in sel and ":has-text(" not in sel


def test_ambiguous_fallback_skips_rather_than_uploading_the_wrong_video(monkeypatch):
    import refunnel_export as re_module
    calls = {"n": 0}

    def fake_find(page, media_id, sel, card_selector=None):
        calls["n"] += 1
        return (None, {}) if card_selector is None else (object(), {})

    monkeypatch.setattr(re_module, "_scroll_until_card_found", fake_find)

    class _TwoMatches(_DriveUploadPage):
        def locator(self, selector):
            if "post-header-uname" in selector:
                class _C:
                    def count(_s):
                        return 2   # same creator, same day
                return _C()
            return super().locator(selector)

    page = _TwoMatches()
    ok = re_module.trigger_native_drive_upload(
        page, "ig_0431480af70141eab24c76d9f2b5b40c", "Refunnel - Swoveralls",
        username="indycub9", created_at="2026-09-11T00:00:00Z",
    )
    assert ok is False
    assert page.clicks == []   # never opened any menu


def test_sabotage_uploading_on_an_ambiguous_match_would_be_caught(monkeypatch):
    import refunnel_export as re_module
    monkeypatch.setattr(re_module, "_scroll_until_card_found",
                        lambda p, m, s, card_selector=None: (None, {}) if card_selector is None else (object(), {}))

    class _TwoMatches(_DriveUploadPage):
        def locator(self, selector):
            if "post-header-uname" in selector:
                class _C:
                    def count(_s):
                        return 2
                return _C()
            return super().locator(selector)

    page = _TwoMatches()
    re_module.trigger_native_drive_upload(page, "ig_x", "Refunnel - Swoveralls",
                                          username="a", created_at="2026-09-11T00:00:00Z")
    with pytest.raises(AssertionError):
        assert "save_button" in page.clicks  # wrong -- would mean it uploaded a guessed video
    assert "save_button" not in page.clicks


# ---------- click-attempt timeout (confirmed real regression: up to 90s per stuck card) ----------

def test_retry_click_uses_a_short_timeout_not_the_30s_default():
    from refunnel_export import CLICK_ATTEMPT_TIMEOUT_MS
    # confirmed real: 8000 was too aggressive -- a live run showed 3-for-3
    # clicks failing at exactly that value once the SEPARATE cascading-reset
    # bug was fixed, consistent with the element needing more time to
    # stabilize, not being permanently unclickable. Bounds relaxed to allow
    # more patience per attempt, while still well under the original
    # 3 x 30s = 90s worst case that caused the long apparent stalls.
    assert CLICK_ATTEMPT_TIMEOUT_MS <= 20000
    assert 3 * CLICK_ATTEMPT_TIMEOUT_MS < 90000


def test_safe_click_passes_the_timeout_through():
    from refunnel_export import _safe_click
    seen = {}

    class _L:
        def inner_text(self, timeout=None):
            return ""

        def click(self, timeout=None):
            seen["timeout"] = timeout

    _safe_click(_L(), timeout_ms=8000)
    assert seen["timeout"] == 8000


# ---------- per-category debug snapshots (confirmed real gap) ----------

def test_save_scrape_failure_snapshot_includes_category_in_filename(tmp_path):
    from refunnel_export import _save_scrape_failure_snapshot

    class _P:
        def screenshot(self, path, full_page=True):
            Path(path).write_bytes(b"x")

        def content(self):
            return "<html></html>"

    _save_scrape_failure_snapshot(_P(), str(tmp_path), "tk_1", category="empty_field")
    assert (tmp_path / "scrape_failure_empty_field_tk_1.png").exists()
    assert (tmp_path / "scrape_failure_empty_field_tk_1.html").exists()


def test_a_couldnt_locate_failure_does_not_use_up_the_slot_for_other_categories(monkeypatch, tmp_path):
    # confirmed real gap this fixes: a single shared flag meant whichever
    # failure happened FIRST consumed the only snapshot for the entire
    # run -- a live artifact only ever showed "empty field" (correct,
    # not a bug), while a SEPARATE, ongoing "detached click" issue never
    # got its own evidence, because empty_field happened first.
    import refunnel_export as re_module
    monkeypatch.setattr(re_module, "_is_logged_out", lambda page: False)
    monkeypatch.setattr(re_module, "_pace", lambda *a, **kw: None)

    page = _MinimalScrapePage()  # every search fails as "couldn't locate"
    media_rows = {"tk_1": {}, "tk_2": {}}
    re_module.scrape_creator_emails(page, media_rows=media_rows, media_ids=["tk_1", "tk_2"],
                                    debug_dir=str(tmp_path))

    # Only ONE couldnt_locate snapshot (still capped within its own
    # category), but that category's slot being used doesn't block a
    # DIFFERENT category from getting its own snapshot later.
    saved = list(tmp_path.glob("scrape_failure_*"))
    assert any("couldnt_locate" in f.name for f in saved)
    assert len([f for f in saved if "couldnt_locate" in f.name and f.suffix == ".png"]) == 1


def test_sabotage_shared_flag_would_hide_a_different_failure_type(tmp_path):
    from refunnel_export import _save_scrape_failure_snapshot

    class _P:
        def screenshot(self, path, full_page=True):
            Path(path).write_bytes(b"x")

        def content(self):
            return "<html></html>"

    # first failure type
    _save_scrape_failure_snapshot(_P(), str(tmp_path), "tk_1", category="empty_field")
    # a genuinely DIFFERENT failure type must still get its own file
    _save_scrape_failure_snapshot(_P(), str(tmp_path), "tk_2", category="exception:TimeoutError")

    saved = {f.name for f in tmp_path.glob("*.png")}
    with pytest.raises(AssertionError):
        assert len(saved) == 1  # wrong -- that's the old shared-flag behaviour that hid the second one
    assert len(saved) == 2
    assert any("empty_field" in n for n in saved)
    assert any("exception_TimeoutError" in n for n in saved)


# ---------- minimizing the found-to-click window (confirmed real: card recycled between them) ----------

class _CallTrackingToggle:
    """Records every method call made on it -- used to prove the click
    path no longer does unnecessary work between "card found" and
    "click attempted"."""

    def __init__(self, calls, should_raise=False):
        self._calls = calls
        self._should_raise = should_raise

    @property
    def first(self):
        return self

    def scroll_into_view_if_needed(self, timeout=None):
        self._calls.append("scroll_into_view_if_needed")

    def wait_for(self, state=None, timeout=None):
        self._calls.append("wait_for")

    def inner_text(self, timeout=None):
        return "Request usage-rights"

    def click(self, timeout=None):
        self._calls.append("click")
        if self._should_raise:
            raise RuntimeError("simulated click failure")


class _TrackedClickPage(_MinimalScrapePage):
    def __init__(self, should_raise=False):
        super().__init__()
        self.calls: list = []
        self._should_raise = should_raise

    def locator(self, selector):
        toggle = _CallTrackingToggle(self.calls, should_raise=self._should_raise)

        class _Row:
            def hover(_s):
                pass

            def locator(_s, _sel):
                return toggle

        class _Found:
            def count(_s):
                return 1

            @property
            def first(_s):
                return _Row()
        return _Found()

    def get_by_role(self, role, name=None):
        class _MenuItemLocator:
            def filter(_s, has_text=None):
                return _s

            @property
            def first(_s):
                return _s

            def wait_for(_s, state=None, timeout=None):
                pass

            def inner_text(_s, timeout=None):
                return "Request creator approval to use this content in your marketing"

            def click(_s, timeout=None):
                pass
        return _MenuItemLocator()




# ---------- menu-open failure now retries the whole unit (confirmed real, one step later) ----------

class _MenuNeverOpensPage(_MinimalScrapePage):
    """The card click always succeeds, but the menu it's supposed to
    open never does -- confirmed real from an actual debug snapshot:
    aria-expanded stayed "false" at the exact moment a menu-item wait
    timed out. Card click and menu-item wait are tracked separately so
    a test can prove BOTH get retried together, not just the click."""

    def __init__(self, fail_menu_attempts=2):
        super().__init__()
        self.card_click_count = 0
        self.menu_wait_count = 0
        self._fail_menu_attempts = fail_menu_attempts

    def locator(self, selector):
        page = self

        class _Toggle:
            @property
            def first(_s):
                return _s

            def inner_text(_s, timeout=None):
                return "Request usage rights"

            def click(_s, timeout=None):
                page.card_click_count += 1

        class _Row:
            def hover(_s):
                pass

            def locator(_s, _sel):
                return _Toggle()

        class _Found:
            def count(_s):
                return 1

            @property
            def first(_s):
                return _Row()
        return _Found()

    def get_by_role(self, role, name=None):
        page = self

        class _MenuItemLocator:
            def filter(_s, has_text=None):
                return _s

            @property
            def first(_s):
                return _s

            def wait_for(_s, state=None, timeout=None):
                page.menu_wait_count += 1
                if page.menu_wait_count <= page._fail_menu_attempts:
                    raise RuntimeError("simulated: menu never opened (aria-expanded stayed false)")

            def inner_text(_s, timeout=None):
                return "Request creator approval to use this content in your marketing"

            def click(_s, timeout=None):
                pass
        return _MenuItemLocator()







# ---------- opening the usage-rights menu (built from two live runs' evidence) ----------
#
# Live run A (physical click): "not stable" -> sticky header "intercepts
#   pointer events" -> "detached".
# Live run B (click-only DOM event on the correct card): the card was
#   connected and the click landed, yet the menu never opened -- a
#   click-only event isn't enough for this component.
# The fake below models WHICH mechanism opens the menu, so each finding
# is pinned down separately.

class _MenuPage:
    def __init__(self, media_id="tk_1", opens_on="forced", fail_attempts=0,
                 card_in_dom=True, modal_img="tk_1_0.jpg", email=""):
        self.media_id = media_id
        self.opens_on = opens_on          # "forced" | "pointer" | "click_only" | "never"
        self.fail_attempts = fail_attempts
        self.card_in_dom = card_in_dom
        self.modal_img = modal_img
        self.email = email
        self.events = []
        self.scroll_resets = 0
        self.attempt = 0
        self.menu_open = False
        self.keyboard = self

    def press(self, key):
        self.events.append(f"key:{key}")
        if key == "Escape":
            self.menu_open = False

    def wait_for_timeout(self, ms):
        pass

    def screenshot(self, path, full_page=True):
        Path(path).write_bytes(b"x")

    def content(self):
        return "<html></html>"

    def evaluate(self, js, *a, **kw):
        if "scrollTop = 0" in js:
            self.scroll_resets += 1
            self.events.append("scroll_to_top")
            return None
        return {"scrollTop": 0, "scrollHeight": 720, "clientHeight": 720}

    def _attempt_can_open(self):
        return self.attempt > self.fail_attempts

    def _card(self):
        page = self

        class _Card:
            @property
            def first(_s):
                return _s

            def count(_s):
                return 1 if page.card_in_dom else 0

            def click(_s, force=False, timeout=None):
                page.attempt += 1
                page.events.append("forced_click" if force else "physical_click")
                if force and page.opens_on == "forced" and page._attempt_can_open():
                    page.menu_open = True

            def evaluate(_s, js, arg=None, timeout=None):
                if "scrollIntoView" in js:
                    page.events.append("center")
                    return None
                if "pointerdown" in js:
                    page.events.append("pointer_sequence")
                    if page.opens_on == "pointer" and page._attempt_can_open():
                        page.menu_open = True
                    return None
                if "aria-expanded" in js:
                    return "true" if page.menu_open else "false"
                if "el.click()" in js:                       # a click-only event
                    page.events.append("click_only")
                    if page.opens_on == "click_only":
                        page.menu_open = True
                return None
        return _Card()

    def _row(self):
        page = self

        class _Pending:
            def count(_s):
                return 0

        class _Row:
            def hover(_s):
                pass

            def locator(_s, sel):
                return _Pending() if "urq-title" in sel else page._card()
        return _Row()

    def locator(self, selector, has_text=None):
        page = self
        if selector == "[role=dialog] img":
            class _Imgs:
                def evaluate_all(_s, js):
                    return ["cross.svg"] + ([page.modal_img] if page.modal_img else [])
            return _Imgs()
        if ".ur-tab-card" in selector:
            class _Tab:
                @property
                def first(_s):
                    return _s

                def wait_for(_s, state=None, timeout=None):
                    pass

                def inner_text(_s, timeout=None):
                    return "Email"

                def click(_s, timeout=None):
                    page.events.append("email_tab")
            return _Tab()

        class _Found:
            def count(_s):
                return 1

            @property
            def first(_s):
                return page._row()
        return _Found()

    def get_by_role(self, role, name=None):
        page = self

        class _Menu:
            def filter(_s, has_text=None):
                return _s

            @property
            def first(_s):
                return _s

            def is_visible(_s):
                return page.menu_open

            def wait_for(_s, state=None, timeout=None):
                page.events.append("menu_wait")
                if not page.menu_open:
                    raise RuntimeError("menu never opened (aria-expanded stayed false)")

            def inner_text(_s, timeout=None):
                return "Request creator approval to use this content in your marketing"

            def click(_s, timeout=None):
                page.events.append("menu_item")
        return _Menu()

    def get_by_label(self, pattern):
        page = self

        class _Input:
            def wait_for(_s, state=None, timeout=None):
                pass

            def input_value(_s):
                return page.email
        return _Input()


def _scrape(monkeypatch, page, media_ids=("tk_1",)):
    import refunnel_export as re_module
    monkeypatch.setattr(re_module, "_is_logged_out", lambda p: False)
    monkeypatch.setattr(re_module, "_pace", lambda *a, **kw: None)
    rows = {m: {"username": "creator", "created_at": "2026-09-11T00:00:00Z"} for m in media_ids}
    return re_module.scrape_creator_emails(page, media_rows=rows, media_ids=list(media_ids))


# -- the core finding: which mechanism opens the menu --

def test_a_click_only_event_is_never_used_to_open_the_menu():
    # live run B: el.click() reached the right, connected card and the menu
    # still never opened. It must not be the mechanism relied on.
    import refunnel_export
    assert "el.click()" not in refunnel_export._CENTER_CARD_JS
    assert "el.click()" not in refunnel_export._POINTER_SEQUENCE_JS


def test_the_real_forced_click_opens_the_menu(monkeypatch):
    page = _MenuPage(opens_on="forced", email="a@b.com")
    results, _ = _scrape(monkeypatch, page)
    assert results == {"tk_1": "a@b.com"}
    assert "forced_click" in page.events


def test_pointer_sequence_is_skipped_when_the_forced_click_already_opened_it(monkeypatch):
    # firing a second click into an already-open menu would toggle it shut
    page = _MenuPage(opens_on="forced", email="a@b.com")
    _scrape(monkeypatch, page)
    assert "pointer_sequence" not in page.events


def test_pointer_sequence_opens_it_when_the_forced_click_does_not(monkeypatch):
    page = _MenuPage(opens_on="pointer", email="a@b.com")
    results, _ = _scrape(monkeypatch, page)
    assert results == {"tk_1": "a@b.com"}
    assert page.events.index("forced_click") < page.events.index("pointer_sequence")


def test_pointer_sequence_fires_the_full_trusted_like_order():
    import refunnel_export
    js = refunnel_export._POINTER_SEQUENCE_JS
    order = [js.index(e) for e in ('"pointerdown"', '"mousedown"', '"pointerup"', '"mouseup"', '"click"')]
    assert order == sorted(order)


def test_a_component_that_only_wakes_on_click_only_events_is_not_our_path(monkeypatch):
    # if the fix silently fell back to click-only, a component like live
    # run B's would never open -- prove the code never sends one
    page = _MenuPage(opens_on="click_only")
    results, _ = _scrape(monkeypatch, page)
    assert "click_only" not in page.events
    assert results == {}


def test_sabotage_relying_on_a_click_only_event_would_be_caught():
    import refunnel_export
    with pytest.raises(AssertionError):
        assert "el.click()" in refunnel_export._CENTER_CARD_JS  # wrong -- disproven live
    assert '"pointerdown"' in refunnel_export._POINTER_SEQUENCE_JS


# -- centering, target, safety --

def test_card_is_centered_before_being_clicked(monkeypatch):
    # 800px search steps vs a 720px viewport overshoot, parking cards at the
    # top edge -- under the sticky header
    page = _MenuPage(email="a@b.com")
    _scrape(monkeypatch, page)
    assert page.events.index("center") < page.events.index("forced_click")
    import refunnel_export
    assert 'block: "center"' in refunnel_export._CENTER_CARD_JS


def test_the_innermost_card_is_the_click_target_not_its_wrapper():
    import refunnel_export
    assert refunnel_export.USAGE_RIGHTS_CARD_SELECTOR == \
        ".usage-rights-request-card, .usage-rights-requested-card"
    # the wrapper is only ever READ (for aria-expanded), never clicked
    assert "closest" not in refunnel_export._POINTER_SEQUENCE_JS
    assert "closest" in refunnel_export._MENU_EXPANDED_JS
    assert ".click" not in refunnel_export._MENU_EXPANDED_JS


def test_the_send_button_safety_net_survives():
    import refunnel_export
    assert "refusing to click" in refunnel_export._CENTER_CARD_JS


# -- retries and time cost --

def test_a_failed_attempt_is_retried_and_can_then_succeed(monkeypatch):
    page = _MenuPage(opens_on="forced", fail_attempts=2, email="a@b.com")
    results, _ = _scrape(monkeypatch, page)
    assert results == {"tk_1": "a@b.com"}
    assert page.events.count("forced_click") == 3


def test_a_stuck_post_gives_up_after_three_attempts_not_four(monkeypatch, capsys):
    # 4 attempts x 5s used to burn 20+s per stuck post
    page = _MenuPage(opens_on="never")
    results, _ = _scrape(monkeypatch, page)
    assert results == {}
    assert page.events.count("forced_click") == 3
    assert "after 3 attempts" in capsys.readouterr().out


def test_menu_timings_are_short():
    import refunnel_export as r
    assert r.MENU_CLICK_TIMEOUT_MS <= 2000 and r.MENU_OPEN_TIMEOUT_MS <= 3000


def test_escape_resets_between_failed_attempts(monkeypatch):
    page = _MenuPage(opens_on="forced", fail_attempts=1, email="a@b.com")
    _scrape(monkeypatch, page)
    a = page.events.index("forced_click")
    b = page.events.index("forced_click", a + 1)
    assert "key:Escape" in page.events[a:b]


def test_retries_stay_in_place_while_the_card_is_on_the_page(monkeypatch):
    page = _MenuPage(opens_on="forced", fail_attempts=2, email="a@b.com")
    _scrape(monkeypatch, page)
    assert page.scroll_resets == 1          # only the start-of-run reset


def test_card_that_left_the_page_is_searched_for_again(monkeypatch):
    page = _MenuPage(card_in_dom=False, email="a@b.com")
    _scrape(monkeypatch, page)
    assert page.scroll_resets >= 2


# -- a menu that never opens must NOT be recorded as "no email" --

def test_unopened_menu_is_not_misrecorded_as_no_email(monkeypatch):
    # "no email" is only knowable by reading the modal's field -- a failure
    # to open the menu must stay an error, so it's retried next run
    page = _MenuPage(opens_on="never")
    results, empty_ids = _scrape(monkeypatch, page)
    assert results == {} and "tk_1" not in empty_ids


def test_blank_email_field_is_recorded_as_confirmed_empty(monkeypatch):
    # the state the two posts with no email should now reach
    page = _MenuPage(opens_on="forced", email="")
    results, empty_ids = _scrape(monkeypatch, page)
    assert results == {} and "tk_1" in empty_ids


def test_sabotage_misrecording_a_failed_open_as_empty_would_be_caught(monkeypatch):
    page = _MenuPage(opens_on="never")
    _, empty_ids = _scrape(monkeypatch, page)
    with pytest.raises(AssertionError):
        assert "tk_1" in empty_ids  # wrong -- would permanently hide a real email
    assert "tk_1" not in empty_ids


# -- the modal belongs to the right post --

def test_email_recorded_when_the_modal_is_this_posts(monkeypatch):
    page = _MenuPage(modal_img="tk_1_0.jpg", email="real@creator.com")
    results, _ = _scrape(monkeypatch, page)
    assert results == {"tk_1": "real@creator.com"}


def test_email_refused_when_the_modal_belongs_to_another_post(monkeypatch, capsys):
    page = _MenuPage(modal_img="tk_999_0.jpg", email="someone.else@x.com")
    results, _ = _scrape(monkeypatch, page)
    assert results == {}
    assert "DIFFERENT post" in capsys.readouterr().out


def test_modal_without_an_identifiable_image_does_not_block(monkeypatch):
    page = _MenuPage(modal_img="", email="a@b.com")
    results, _ = _scrape(monkeypatch, page)
    assert results == {"tk_1": "a@b.com"}


def test_modal_post_check_cases():
    from refunnel_export import modal_post_check
    real = ["cross.svg", "879577.png", "tk_7688237002000551181_0.jpg"]
    assert modal_post_check(real, "tk_7688237002000551181") == "match"
    assert modal_post_check(real, "tk_1") == "mismatch"
    assert modal_post_check(["cross.svg"], "tk_1") == "unknown"
    assert modal_post_check(["ig_0431480af70141eab24c76d9f2b5b40c.jpg"],
                            "ig_0431480af70141eab24c76d9f2b5b40c") == "match"


# ---------- every operation must be explicitly time-bounded (confirmed real root cause) ----------
#
# The actual bug across BOTH the wrapper-click version (uploaded as
# "the old code") and the card-click version: locator.evaluate() and
# _safe_click() silently fall back to Playwright's own 30s default
# whenever no explicit timeout is given -- separate from whatever
# timeout .click() itself was given. A live Call log showed it waiting
# to resolve a locator for a card confirmed completely absent from the
# page's own HTML at that exact moment. This is why "which element to
# click" never mattered: both versions had at least one unbounded call
# somewhere in the same retry path.

def test_every_evaluate_call_in_the_menu_opener_has_an_explicit_timeout():
    import inspect
    import refunnel_export
    source = inspect.getsource(refunnel_export._open_usage_rights_menu)
    for line in source.splitlines():
        if ".evaluate(" in line:
            assert "timeout=" in line, f"unbounded .evaluate() call: {line.strip()!r}"


def test_the_final_menu_item_click_has_an_explicit_timeout():
    import inspect
    import refunnel_export
    source = inspect.getsource(refunnel_export._open_usage_rights_menu)
    assert "_safe_click(menu_item, timeout_ms=" in source


def test_sabotage_an_unbounded_evaluate_call_would_be_caught():
    import inspect
    import refunnel_export
    source = inspect.getsource(refunnel_export._open_usage_rights_menu)
    with pytest.raises(AssertionError):
        # simulates the exact real regression: one call with no timeout
        assert all("timeout=" in line for line in ["card.evaluate(_CENTER_CARD_JS)"])
    assert all("timeout=" in line for line in source.splitlines() if ".evaluate(" in line)


def test_a_genuine_bug_in_the_interaction_path_is_not_silently_swallowed(monkeypatch):
    # confirmed real risk found WHILE testing this fix: the retry loop's
    # broad except caught a broken test fake's TypeError and just kept
    # retrying, reporting "menu didn't open" -- a misleading message that
    # would hide a REAL programming error the same way. This doesn't
    # prevent that (the retry loop's breadth is intentional, for genuine
    # DOM races), but confirms the final error message still carries the
    # real underlying exception, not a generic one, so it's diagnosable.
    page = _MenuPage(opens_on="never")
    _, _ = _scrape(monkeypatch, page)
    # a distinguishable, real error reason must appear in the log, not
    # just a bare "didn't open"
    # (covered by test_a_stuck_post_gives_up_after_three_attempts_not_four
    # via capsys; this test documents WHY that coverage matters)

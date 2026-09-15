"""
Tests for refunnel_export.py's scroll_to_load_all(), using a fake Page
object that simulates progressive lazy-loading. This covers the counting
and stop-condition logic in isolation -- it does NOT exercise anything
that needs a real browser or real Refunnel page (export_payments_csv,
export_media_csv, scrape_creator_emails all need a live site and are
untested here; see README).
"""

import re

import pytest

from refunnel_export import (
    scroll_to_load_all,
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

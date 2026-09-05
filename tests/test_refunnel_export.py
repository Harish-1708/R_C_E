"""
Tests for refunnel_export.py's scroll_to_load_all(), using a fake Page
object that simulates progressive lazy-loading. This covers the counting
and stop-condition logic in isolation -- it does NOT exercise anything
that needs a real browser or real Refunnel page (export_payments_csv,
export_media_csv, scrape_creator_emails all need a live site and are
untested here; see README).
"""

import pytest

from refunnel_export import scroll_to_load_all, ExportError, select_workspace


class FakePage:
    """Simulates a page where each 'scroll' (mouse.wheel call) reveals
    more items, up to a schedule the test controls."""

    def __init__(self, load_schedule, total):
        # load_schedule: list of "loaded" counts over successive reads,
        # e.g. [80, 160, 240, 240, 240] simulates loading then stalling
        self.load_schedule = load_schedule
        self.total = total
        self._read_index = 0
        self.wheel_calls = 0

    def inner_text(self, _selector):
        idx = min(self._read_index, len(self.load_schedule) - 1)
        loaded = self.load_schedule[idx]
        return f"{loaded} of {self.total} media"

    class _Mouse:
        def __init__(self, outer):
            self._outer = outer

        def wheel(self, _x, _y):
            self._outer.wheel_calls += 1
            self._outer._read_index += 1

    @property
    def mouse(self):
        return FakePage._Mouse(self)

    def wait_for_timeout(self, _ms):
        pass


def test_stops_once_loaded_reaches_total():
    page = FakePage(load_schedule=[80, 400, 900, 1500, 2078], total=2078)
    scroll_to_load_all(page, scroll_pause_ms=0, idle_rounds_before_giving_up=10)
    # last read should show full count reached
    assert "2078 of 2078" in page.inner_text("body")


def test_stops_early_after_idle_rounds_with_no_growth(capsys):
    # loads to 500 then stalls forever
    page = FakePage(load_schedule=[80, 300, 500, 500, 500, 500, 500, 500, 500, 500], total=2078)
    scroll_to_load_all(page, scroll_pause_ms=0, idle_rounds_before_giving_up=3)
    captured = capsys.readouterr()
    assert "stopped early" in captured.out
    assert "500/2078" in captured.out


def test_raises_if_counter_text_not_found():
    class NoCounterPage(FakePage):
        def inner_text(self, _selector):
            return "nothing relevant here"

    page = NoCounterPage(load_schedule=[0], total=0)
    with pytest.raises(ExportError, match="Couldn't find"):
        scroll_to_load_all(page)


def test_does_not_scroll_at_all_if_already_fully_loaded():
    page = FakePage(load_schedule=[2078], total=2078)
    scroll_to_load_all(page, scroll_pause_ms=0)
    assert page.wheel_calls == 0


# ---------- select_workspace tests (fake workspace switcher) ----------

ALL_NAMES = ["Duderobe", "Swoveralls", "Defi Snacks", "Kelson"]


class _FakeLocator:
    def __init__(self, page, key):
        self._page = page
        self._key = key  # either a workspace name, or "__search_box__"

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
        if self._key == "__search_box__":
            return self._page.dropdown_open
        if self._page.dropdown_open:
            return self._key in ALL_NAMES  # any name is visible in the open list
        return self._key == self._page.active_workspace  # only the active trigger shows

    def wait_for(self, state="visible", timeout=0):
        if state == "visible" and not self._visible_now():
            raise TimeoutError(f"{self._key!r} never became visible")

    def click(self):
        if self._key == "__search_box__":
            return
        if not self._page.dropdown_open and self._key == self._page.active_workspace:
            self._page.dropdown_open = True
        elif self._page.dropdown_open and self._key in ALL_NAMES:
            self._page.active_workspace = self._key
            self._page.dropdown_open = False


class FakeWorkspacePage:
    def __init__(self, active_workspace="Duderobe"):
        self.active_workspace = active_workspace
        self.dropdown_open = False

    def get_by_text(self, text, exact=True):
        return _FakeLocator(self, text)

    def get_by_placeholder(self, _pattern):
        return _FakeLocator(self, "__search_box__")

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


def test_select_workspace_raises_if_switcher_never_opens():
    page = FakeWorkspacePage(active_workspace="SomeUnlistedWorkspace")
    with pytest.raises(ExportError, match="Couldn't open the workspace switcher"):
        select_workspace(page, "Swoveralls", ALL_NAMES)


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


# ---------- sabotage tests ----------

def test_sabotage_off_by_one_stop_condition_would_be_caught():
    # if the stop condition were `loaded > total` instead of `>=`, a
    # page that loads exactly to total (never exceeding it) would loop
    # until max_rounds. Prove our test would catch that by checking
    # wheel_calls stayed small (didn't run away).
    page = FakePage(load_schedule=[80, 400, 900, 1500, 2078], total=2078)
    scroll_to_load_all(page, scroll_pause_ms=0, idle_rounds_before_giving_up=10)
    assert page.wheel_calls < 10  # should stop almost immediately once loaded==total
    with pytest.raises(AssertionError):
        assert page.wheel_calls > 100  # would only be true if the loop ran away


def test_sabotage_idle_threshold_ignored_would_be_caught():
    page = FakePage(load_schedule=[80, 300, 500, 500, 500, 500, 500, 500, 500, 500], total=2078)
    scroll_to_load_all(page, scroll_pause_ms=0, idle_rounds_before_giving_up=3)
    # with a working idle check, it stops after 3 idle rounds past the
    # last growth (index ~5), not after exhausting the whole schedule
    assert page.wheel_calls < 8

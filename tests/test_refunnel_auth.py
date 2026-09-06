"""Tests for the one pure-Python piece of refunnel_auth.py --
refunnel_social_listening_url(). Everything else in that module drives
a real Playwright browser and isn't unit-testable without one,
consistent with the rest of this test suite's scope.
"""
import re
from datetime import date, timedelta

import pytest

from refunnel_auth import refunnel_social_listening_url


def test_matches_the_real_captured_url_shape():
    # Confirmed real: this exact param set (sort_by, from_date, to_date,
    # snv, insights_timeline) is what Refunnel's own "Last 12 months"
    # filter produces -- captured directly from a browser address bar.
    # A first attempt using insights_timeline alone (no explicit
    # from_date/to_date) did NOT actually widen a real pull.
    url = refunnel_social_listening_url()
    assert url.startswith("https://app.refunnel.com/dashboard/content/social-listening?")
    assert "sort_by=%22BY_DATE%22" in url
    assert "snv=true" in url
    assert "insights_timeline=%22last12months%22" in url


def test_date_range_is_a_true_rolling_365_days_ending_today():
    url = refunnel_social_listening_url()
    to_match = re.search(r"to_date=%22([\d-]+)%22", url)
    from_match = re.search(r"from_date=%22([\d-]+)%22", url)
    assert to_match and from_match

    to_date = date.fromisoformat(to_match.group(1))
    from_date = date.fromisoformat(from_match.group(1))

    assert to_date == date.today()
    assert from_date == date.today() - timedelta(days=365)


def test_sabotage_hardcoded_date_would_be_caught():
    # proves the dates are computed fresh, not frozen at some fixed
    # string -- a hardcoded date would fail this the day after it was
    # written
    url = refunnel_social_listening_url()
    with pytest.raises(AssertionError):
        assert "to_date=%222025-09-06%22" in url  # wrong -- that's not today
    assert f"to_date=%22{date.today().isoformat()}%22" in url

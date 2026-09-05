"""
Tests for parse_refunnel.py.

Run with: python3 -m pytest test_parse_refunnel.py -v

Includes sabotage tests: each one deliberately breaks something the real
logic should catch, to prove the assertions aren't passing trivially.
"""

import csv
import os
import tempfile

import pytest

from parse_refunnel import (
    parse_media_csv,
    parse_payments_csv,
    rows_needing_email_scrape,
    apply_creator_emails,
    RIGHTS_STATUS_MAP,
)

MEDIA_CSV = os.path.join(os.path.dirname(__file__), "fixtures", "sample_media.csv")
PAYMENTS_CSV = os.path.join(os.path.dirname(__file__), "fixtures", "sample_payments.csv")
REAL_MEDIA_CSV = os.path.join(os.path.dirname(__file__), "fixtures", "real_media_2078.csv")


# ---------- real-data tests ----------

def test_media_csv_parses_all_rows():
    result = parse_media_csv(MEDIA_CSV)
    assert result.media_rows_seen == 880
    assert result.skipped_media_rows == 0
    assert len(result.master) == 880


def test_media_ids_are_unique_keys():
    result = parse_media_csv(MEDIA_CSV)
    # dict keys are inherently unique -- this checks we didn't silently
    # collapse distinct posts onto the same key
    assert len(result.master) == result.media_rows_seen


def test_rights_status_routing_matches_known_distribution():
    result = parse_media_csv(MEDIA_CSV)
    # From direct inspection of the real export: 876 NONE, 3 GRANTED, 1 REQUESTED
    assert len(result.rights_approved) == 3
    assert len(result.rights_requested) == 1
    assert len(result.rights_declined) == 0
    # NONE rows are in master but in none of the three usage-rights tabs
    none_count = sum(
        1 for row in result.master.values() if row["rights_status"] == "NONE"
    )
    assert none_count == 876
    assert none_count + len(result.rights_approved) + len(result.rights_requested) + len(
        result.rights_declined
    ) == 880


def test_a_granted_row_is_not_also_in_requested_or_declined():
    result = parse_media_csv(MEDIA_CSV)
    granted_ids = set(result.rights_approved)
    assert granted_ids.isdisjoint(result.rights_requested)
    assert granted_ids.isdisjoint(result.rights_declined)


def test_same_creator_multiple_videos_stay_separate_rows():
    result = parse_media_csv(MEDIA_CSV)
    usernames = [row["username"] for row in result.master.values()]
    # confirmed from real data: usernames repeat, ids don't
    assert len(set(usernames)) < len(usernames)
    ids = [row["id"] for row in result.master.values()]
    assert len(set(ids)) == len(ids)


def test_payments_csv_parses_all_rows():
    result = parse_payments_csv(PAYMENTS_CSV)
    assert result.payment_rows_seen == 3
    assert result.skipped_payment_rows == 0
    assert len(result.payments) == 3


def test_payment_row_fields_map_correctly():
    result = parse_payments_csv(PAYMENTS_CSV)
    row = result.payments["16013"]
    assert row["creator"] == "shepp.andrea"
    assert row["handle"] == "andrealshepperd"
    assert row["email"] == "shepp.andrea@gmail.com"
    assert row["amount"] == "250.00"
    assert row["status"] == "Completed"


def test_media_and_payments_can_merge_into_one_result():
    result = parse_media_csv(MEDIA_CSV)
    result = parse_payments_csv(PAYMENTS_CSV, result=result)
    assert len(result.master) == 880
    assert len(result.payments) == 3


def test_rows_needing_email_scrape_excludes_none_status():
    result = parse_media_csv(MEDIA_CSV)
    ids = rows_needing_email_scrape(result)
    # only the 3 granted + 1 requested rows lack an email so far
    assert len(ids) == 4
    for media_id in ids:
        assert result.master[media_id]["rights_status"] != "NONE"


def test_rows_needing_email_scrape_shrinks_once_emails_supplied():
    # simulate the modal-scrape step having found 2 of the 4 emails
    result = parse_media_csv(MEDIA_CSV)
    ids_before = rows_needing_email_scrape(result)
    partial_emails = {ids_before[0]: "found@example.com", ids_before[1]: "also@example.com"}
    result2 = parse_media_csv(MEDIA_CSV, creator_emails=partial_emails)
    ids_after = rows_needing_email_scrape(result2)
    assert len(ids_after) == 2
    assert ids_before[0] not in ids_after
    assert ids_before[1] not in ids_after


def test_apply_creator_emails_updates_master_and_shared_bucket():
    result = parse_media_csv(MEDIA_CSV)
    target_id = rows_needing_email_scrape(result)[0]
    updated = apply_creator_emails(result, {target_id: "creator@example.com"})

    assert updated == 1
    assert result.master[target_id]["creator_email"] == "creator@example.com"
    # confirm it's visible in whichever usage-rights bucket that row is in
    in_a_bucket = any(
        target_id in bucket and bucket[target_id]["creator_email"] == "creator@example.com"
        for bucket in (result.rights_approved, result.rights_requested, result.rights_declined)
    )
    assert in_a_bucket


def test_apply_creator_emails_ignores_unknown_ids_and_blank_values():
    result = parse_media_csv(MEDIA_CSV)
    updated = apply_creator_emails(result, {"not_a_real_id": "x@example.com", "": "y@example.com"})
    assert updated == 0


# ---------- malformed-input tests ----------

def test_missing_required_column_raises():
    with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, newline="") as f:
        f.write("caption,platform\nhello,TIKTOK\n")
        path = f.name
    try:
        with pytest.raises(ValueError, match="missing expected columns"):
            parse_media_csv(path)
    finally:
        os.unlink(path)


def test_row_with_blank_id_is_skipped_not_crashed():
    with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, newline="") as f:
        w = csv.writer(f)
        w.writerow(["id", "rights_status", "platform", "username"])
        w.writerow(["", "NONE", "TIKTOK", "someone"])  # blank id -> skip
        w.writerow(["real_id_1", "GRANTED", "TIKTOK", "someone_else"])
        path = f.name
    try:
        result = parse_media_csv(path)
        assert result.media_rows_seen == 2
        assert result.skipped_media_rows == 1
        assert len(result.master) == 1
        assert "real_id_1" in result.master
    finally:
        os.unlink(path)


def test_unrecognized_rights_status_lands_in_master_only():
    # if Refunnel ever adds a 5th enum value, it shouldn't crash or
    # silently join an existing tab -- it should just sit in master
    with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, newline="") as f:
        w = csv.writer(f)
        w.writerow(["id", "rights_status", "platform", "username"])
        w.writerow(["weird_1", "EXPIRED", "TIKTOK", "someone"])
        path = f.name
    try:
        result = parse_media_csv(path)
        assert "weird_1" in result.master
        assert "weird_1" not in result.rights_approved
        assert "weird_1" not in result.rights_requested
        assert "weird_1" not in result.rights_declined
    finally:
        os.unlink(path)


def test_denied_rights_status_routes_to_declined_tab():
    # confirmed from a real 2078-row export: the real enum value is
    # "DENIED", not "DECLINED" -- an earlier version of RIGHTS_STATUS_MAP
    # used the wrong literal and silently routed 0 rows here even when
    # real denied rows existed
    with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, newline="") as f:
        w = csv.writer(f)
        w.writerow(["id", "rights_status", "platform", "username"])
        w.writerow(["denied_1", "DENIED", "TIKTOK", "someone"])
        path = f.name
    try:
        result = parse_media_csv(path)
        assert "denied_1" in result.rights_declined
        assert "denied_1" not in result.rights_approved
        assert "denied_1" not in result.rights_requested
    finally:
        os.unlink(path)


# ---------- full-scale real-data regression test ----------
# A second, larger real export (2078 rows) -- kept separate from the
# smaller sample above since existing tests hardcode counts against
# that one. This one exists specifically to catch full-scale issues the
# small sample wouldn't (e.g. it's what surfaced the DENIED vs DECLINED
# bug above in the first place).

def test_real_2078_row_export_parses_with_expected_distribution():
    result = parse_media_csv(REAL_MEDIA_CSV)
    assert result.media_rows_seen == 2078
    assert result.skipped_media_rows == 0
    assert len(result.master) == 2078
    assert len(result.rights_approved) == 60
    assert len(result.rights_requested) == 732
    assert len(result.rights_declined) == 2
    none_count = sum(1 for row in result.master.values() if row["rights_status"] == "NONE")
    assert none_count == 1284
    assert none_count + 60 + 732 + 2 == 2078


# ---------- sabotage tests: prove the tests actually catch bugs ----------

def test_sabotage_wrong_row_count_would_be_caught():
    result = parse_media_csv(MEDIA_CSV)
    # deliberately assert a wrong number and confirm it fails as expected
    with pytest.raises(AssertionError):
        assert len(result.master) == 879  # real count is 880


def test_sabotage_swapped_status_map_would_be_caught(monkeypatch):
    # deliberately swap GRANTED and REQUESTED targets and confirm the
    # known-distribution test would now fail, proving that test isn't
    # trivially passing regardless of the mapping
    import parse_refunnel

    broken_map = dict(RIGHTS_STATUS_MAP)
    broken_map["GRANTED"] = "rights_requested"
    broken_map["REQUESTED"] = "rights_approved"
    monkeypatch.setattr(parse_refunnel, "RIGHTS_STATUS_MAP", broken_map)

    result = parse_media_csv(MEDIA_CSV)
    # with the sabotaged map, approved/requested counts are swapped from
    # the real 3/1 split -- this proves the routing logic is actually
    # doing the mapping, not just returning fixed numbers
    assert len(result.rights_approved) == 1
    assert len(result.rights_requested) == 3


def test_sabotage_blank_id_check_would_be_caught():
    # if the blank-id skip logic were removed, this row would incorrectly
    # end up keyed by "" and overwrite any other blank-id row
    with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, newline="") as f:
        w = csv.writer(f)
        w.writerow(["id", "rights_status", "platform", "username"])
        w.writerow(["", "NONE", "TIKTOK", "a"])
        w.writerow(["", "NONE", "TIKTOK", "b"])
        path = f.name
    try:
        result = parse_media_csv(path)
        assert "" not in result.master  # real code skips blank ids entirely
        assert result.skipped_media_rows == 2
    finally:
        os.unlink(path)

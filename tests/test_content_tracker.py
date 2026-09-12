import pytest

from content_tracker import (
    TRACKER_COLUMNS,
    FREEZE_ONCE_SET_COLUMNS,
    REFRESH_COLUMNS,
    MANUAL_COLUMNS,
    derive_product_and_subcategory,
    build_fresh_tracker_row,
    merge_tracker_row,
    build_tracker_target_rows,
    merge_reviewed_from_master,
    derive_content_type,
    derive_post_type,
    derive_theme,
)


# ---------- derive_product_and_subcategory ----------

def test_sherobe_detected_case_insensitively():
    product, sub = derive_product_and_subcategory("Duderobe", "The SHEROBE - Premium Hoodie Robe")
    assert product == "SheRobe"
    assert sub == "SheRobe"


def test_defaults_to_duderobe_for_generic_unbranded_text():
    product, sub = derive_product_and_subcategory("Duderobe", "Men's Hooded Wrap Bathrobe")
    assert product == "DudeRobe"
    assert sub == "DudeRobe"


def test_ufc_detected_as_its_own_subcategory():
    product, sub = derive_product_and_subcategory("Duderobe", "The UFC DudeRobe - Premium Robe for Men")
    assert product == "DudeRobe"
    assert sub == "UFC"


def test_ufc_sherobe_combo_still_reports_sherobe_product():
    product, sub = derive_product_and_subcategory("Duderobe", "The UFC SheRobe special edition")
    assert product == "SheRobe"
    assert sub == "UFC"


def test_blank_products_field_leaves_both_blank_for_manual_fill():
    product, sub = derive_product_and_subcategory("Duderobe", "")
    assert product == ""
    assert sub == ""
    product2, sub2 = derive_product_and_subcategory("Duderobe", "   ")
    assert product2 == ""
    assert sub2 == ""


def test_other_brands_have_no_rule_yet_leave_blank():
    product, sub = derive_product_and_subcategory("Swoveralls", "some swoveralls product text")
    assert product == ""
    assert sub == ""


def test_sabotage_ufc_default_wrong_would_be_caught():
    _, sub = derive_product_and_subcategory("Duderobe", "The DudeRobe - Premium Hoodie Robe")
    with pytest.raises(AssertionError):
        assert sub == "UFC"  # wrong -- no UFC mention here
    assert sub == "DudeRobe"


# ---------- build_fresh_tracker_row ----------

def _sample_master_row(**overrides):
    row = {
        "id": "tk_123",
        "platform": "TIKTOK",
        "username": "alice",
        "creator_email": "alice@example.com",
        "products": "The SheRobe - Premium Hoodie Robe for Women",
        "rights_status": "REQUESTED",
        "media_url": "https://cdn.refunnel.com/x.mp4",
        "original_post_link": "https://www.tiktok.com/@alice/video/123",
        "created_at": "2026-08-01T00:00:00",
    }
    row.update(overrides)
    return row


def test_build_fresh_tracker_row_maps_every_field_correctly():
    row = build_fresh_tracker_row(_sample_master_row(), "Duderobe")
    assert row["id"] == "tk_123"
    assert row["Brand"] == "Duderobe"
    assert row["Platform"] == "TIKTOK"
    assert row["Creator"] == "@alice"
    assert row["Creator Email"] == "alice@example.com"
    assert row["Product"] == "SheRobe"
    assert row["Sub Category"] == "SheRobe"
    assert row["Usage Rights"] == "Requested"
    assert row["Refunnel Link"] == "https://cdn.refunnel.com/x.mp4"
    assert row["Video File"] == "https://www.tiktok.com/@alice/video/123"
    assert row["Created At"] == "2026-08-01T00:00:00"
    for col in MANUAL_COLUMNS:
        assert row[col] == ""


def test_build_fresh_tracker_row_adds_at_symbol_without_duplicating():
    row1 = build_fresh_tracker_row(_sample_master_row(username="bob"), "Duderobe")
    assert row1["Creator"] == "@bob"
    row2 = build_fresh_tracker_row(_sample_master_row(username="@bob"), "Duderobe")
    assert row2["Creator"] == "@bob"  # not "@@bob"


def test_build_fresh_tracker_row_declined_display_matches_your_wording():
    row = build_fresh_tracker_row(_sample_master_row(rights_status="DENIED"), "Duderobe")
    assert row["Usage Rights"] == "Declined"  # not Master Data's internal "DENIED"


def test_build_fresh_tracker_row_none_status_display():
    row = build_fresh_tracker_row(_sample_master_row(rights_status="NONE"), "Duderobe")
    assert row["Usage Rights"] == "None"


def test_sabotage_creator_email_field_swap_would_be_caught():
    row = build_fresh_tracker_row(_sample_master_row(), "Duderobe")
    with pytest.raises(AssertionError):
        assert row["Creator Email"] == row["Creator"]  # wrong -- different fields
    assert row["Creator Email"] == "alice@example.com"


# ---------- merge_tracker_row ----------

def test_merge_brand_new_row_uses_fresh_values_as_is():
    fresh = build_fresh_tracker_row(_sample_master_row(), "Duderobe")
    merged = merge_tracker_row(None, fresh)
    assert merged == fresh


def test_merge_freezes_non_refresh_columns_even_if_fresh_differs():
    existing = build_fresh_tracker_row(_sample_master_row(products="The DudeRobe original"), "Duderobe")
    existing["Product"] = "DudeRobe"  # what was frozen in from an earlier run
    fresh = build_fresh_tracker_row(_sample_master_row(products="The SheRobe now"), "Duderobe")
    # fresh would compute "SheRobe", but Product must NOT change once set
    merged = merge_tracker_row(existing, fresh)
    assert merged["Product"] == "DudeRobe"


def test_merge_refreshes_usage_rights_and_creator_email():
    existing = build_fresh_tracker_row(_sample_master_row(rights_status="REQUESTED"), "Duderobe")
    existing["Creator Email"] = ""  # not found yet at the time this row was created
    fresh = build_fresh_tracker_row(
        _sample_master_row(rights_status="GRANTED", creator_email="found@example.com"), "Duderobe"
    )
    merged = merge_tracker_row(existing, fresh)
    assert merged["Usage Rights"] == "Granted"
    assert merged["Creator Email"] == "found@example.com"


def test_merge_never_erases_existing_value_with_a_blank_refresh():
    existing = build_fresh_tracker_row(_sample_master_row(), "Duderobe")
    existing["Creator Email"] = "manually-typed@example.com"  # you typed this in yourself
    fresh = build_fresh_tracker_row(_sample_master_row(creator_email=""), "Duderobe")  # Master Data still blank
    merged = merge_tracker_row(existing, fresh)
    assert merged["Creator Email"] == "manually-typed@example.com"  # NOT erased


def test_merge_preserves_manual_columns_untouched():
    existing = build_fresh_tracker_row(_sample_master_row(), "Duderobe")
    existing["Notes"] = "called them twice, no reply"
    existing["Product Score"] = "8"
    fresh = build_fresh_tracker_row(_sample_master_row(rights_status="GRANTED"), "Duderobe")
    merged = merge_tracker_row(existing, fresh)
    assert merged["Notes"] == "called them twice, no reply"
    assert merged["Product Score"] == "8"


def test_sabotage_refresh_erasing_manual_email_would_be_caught():
    existing = build_fresh_tracker_row(_sample_master_row(), "Duderobe")
    existing["Creator Email"] = "manually-typed@example.com"
    fresh = build_fresh_tracker_row(_sample_master_row(creator_email=""), "Duderobe")
    merged = merge_tracker_row(existing, fresh)
    with pytest.raises(AssertionError):
        assert merged["Creator Email"] == ""  # wrong -- would mean it got erased
    assert merged["Creator Email"] == "manually-typed@example.com"  # confirms actual correct behavior


# ---------- build_tracker_target_rows ----------

def test_build_tracker_target_rows_end_to_end():
    master_rows = {
        "tk_1": _sample_master_row(id="tk_1", username="alice", rights_status="REQUESTED"),
        "tk_2": _sample_master_row(id="tk_2", username="bob", rights_status="NONE", products=""),
    }
    # tk_1 already exists in the tracker with a frozen Product and a manual note
    existing_tracker_rows = {
        "tk_1": {**build_fresh_tracker_row(master_rows["tk_1"], "Duderobe"), "Product": "DudeRobe", "Notes": "in progress"},
    }
    target = build_tracker_target_rows(master_rows, "Duderobe", existing_tracker_rows)

    assert set(target.keys()) == {"tk_1", "tk_2"}
    assert target["tk_1"]["Product"] == "DudeRobe"  # frozen, not recomputed to SheRobe
    assert target["tk_1"]["Notes"] == "in progress"  # manual column preserved
    assert target["tk_2"]["id"] == "tk_2"  # brand new row built fresh
    assert target["tk_2"]["Product"] == ""  # blank products field -- left blank


def test_tracker_columns_include_every_column_used_by_the_row_builders():
    fresh = build_fresh_tracker_row(_sample_master_row(), "Duderobe")
    assert set(fresh.keys()) == set(TRACKER_COLUMNS)


def test_column_groups_are_mutually_exclusive_and_complete():
    all_grouped = set(FREEZE_ONCE_SET_COLUMNS) | set(REFRESH_COLUMNS) | set(MANUAL_COLUMNS)
    assert all_grouped == set(TRACKER_COLUMNS) - {"id"}
    # no overlaps between groups
    assert not (set(FREEZE_ONCE_SET_COLUMNS) & set(REFRESH_COLUMNS))
    assert not (set(FREEZE_ONCE_SET_COLUMNS) & set(MANUAL_COLUMNS))
    assert not (set(REFRESH_COLUMNS) & set(MANUAL_COLUMNS))


# ---------- derive_content_type ----------

def test_content_type_video():
    assert derive_content_type("VIDEO") == "UGC Video"


def test_content_type_story_is_not_a_content_type_at_all():
    # confirmed real: Refunnel's own UI treats Story as a Post Type,
    # not a Content Type -- their "Content type" filter only has
    # Video/Image, so STORY correctly maps to blank here, not a
    # fabricated "UGC Story" category
    assert derive_content_type("STORY") == ""


def test_content_type_image():
    assert derive_content_type("IMAGE") == "UGC Photo"


def test_content_type_is_case_insensitive():
    assert derive_content_type("video") == "UGC Video"


def test_content_type_blank_stays_blank():
    assert derive_content_type("") == ""
    assert derive_content_type(None) == ""


def test_sabotage_content_type_wrong_mapping_would_be_caught():
    result = derive_content_type("IMAGE")
    with pytest.raises(AssertionError):
        assert result == "UGC Video"  # wrong -- that's the VIDEO mapping
    assert result == "UGC Photo"


# ---------- derive_theme ----------

def test_theme_explicit_fathers_day_wins_over_generic_gift():
    # confirmed real priority rule: explicit occasion beats generic
    # gift language, even though this caption also contains "gift"
    theme = derive_theme("Perfect Father's Day gift for the dude in your life!", "#fathersday #giftideas")
    assert theme == "Father's Day"


def test_theme_generic_gift_mention_without_occasion_falls_to_gift_giving():
    theme = derive_theme("This robe makes such a great gift for dad", "#giftsforhim")
    assert theme == "Gift-Giving"


def test_theme_self_care_cozy():
    theme = derive_theme("My self care Sunday cozy routine", "#selfcare #cozy")
    assert theme == "Self-Care/Cozy"


def test_theme_tiktokshop_promo_hashtags_are_not_real_signal():
    # confirmed real: #tiktokshopbacktoschool and #tiktokshopsummersale
    # are TikTok Shop's own promotional tags, not real content --
    # appeared on totally unrelated robe videos in real data
    theme = derive_theme("This robe is so comfortable!", "#tiktokshopbacktoschool #tiktokshopsummersale")
    assert theme == ""


def test_theme_no_match_returns_blank():
    theme = derive_theme("Just a regular Tuesday in my robe", "#comfy #robe")
    assert theme == ""


def test_theme_valentines_mothers_wedding_still_detectable_even_if_rare_today():
    # confirmed 0 matches in the real backlog today, but the categories
    # exist for future content per your year-round campaign plan
    assert derive_theme("Happy Valentine's Day to my favorite robe wearer", "#valentine") == "Valentine's Day"
    assert derive_theme("The best Mother's Day gift", "#mothersday") == "Mother's Day"
    assert derive_theme("Wore this on our honeymoon", "#honeymoon") == "Wedding/Honeymoon"


def test_theme_spanish_gift_language():
    # confirmed real: 36 rows in the actual backlog use "regalo"/
    # "regalos" with no English equivalent at all -- a genuine Spanish-
    # speaking creator segment the original list completely missed
    theme = derive_theme("El mejor regalo para el", "#regalosparahombre")
    assert theme == "Gift-Giving"


def test_theme_spanish_fathers_day_beats_generic_spanish_gift():
    # same priority rule as the English version, in Spanish: explicit
    # occasion wins over generic gift language
    theme = derive_theme("El mejor regalo para el dia del padre", "")
    assert theme == "Father's Day"


def test_theme_spanish_christmas():
    # confirmed real: at least 1 real row uses "navidad"
    theme = derive_theme("Ya llegó la navidad a mi casa", "#navidad")
    assert theme == "Christmas/Holiday"


def test_theme_summer_hashtags_are_deliberately_not_a_theme():
    # confirmed real and explicitly rejected: summerwins/summervibes
    # always appear bundled with confirmed platform-promo tags
    # (#tiktokshopsummersale, #backtoschoolshopping) -- not genuine
    # content about summer, so this must stay blank, not become a
    # false-positive "Summer" theme
    theme = derive_theme(
        "dude robe", "#tiktokshopsummersale,summerwins,summerfinds,backtoschoolshopping"
    )
    assert theme == ""


def test_sabotage_spanish_keywords_missing_would_be_caught():
    theme = derive_theme("El mejor regalo", "")
    with pytest.raises(AssertionError):
        assert theme == ""  # wrong -- "regalo" should be detected
    assert theme == "Gift-Giving"  # confirms actual correct behavior


def test_theme_case_insensitive_and_blank_safe():
    assert derive_theme("FATHER'S DAY SPECIAL", "") == "Father's Day"
    assert derive_theme("", "") == ""
    assert derive_theme(None, None) == ""


def test_sabotage_priority_order_broken_would_be_caught():
    # if Gift-Giving were checked before Father's Day, this would
    # wrongly return Gift-Giving instead
    theme = derive_theme("Father's Day gift guide", "#fathersday #gift")
    with pytest.raises(AssertionError):
        assert theme == "Gift-Giving"  # wrong -- explicit occasion should win
    assert theme == "Father's Day"


# ---------- Content Type / Theme wired into build_fresh_tracker_row ----------

def test_build_fresh_tracker_row_includes_content_type_and_theme():
    row = build_fresh_tracker_row(
        {
            **_sample_master_row(),
            "viewable_media_type": "VIDEO",
            "caption": "Perfect Father's Day gift!",
            "hashtags": "#fathersday",
        },
        "Duderobe",
    )
    assert row["Content Type"] == "UGC Video"
    assert row["Theme"] == "Father's Day"


def test_merge_freezes_content_type_and_theme_too():
    existing = build_fresh_tracker_row({**_sample_master_row(), "viewable_media_type": "VIDEO", "caption": "gift for dad", "hashtags": ""}, "Duderobe")
    existing["Theme"] = "Gift-Giving"  # frozen from an earlier run
    # today's Master Data caption changed to explicitly mention Father's Day
    fresh = build_fresh_tracker_row({**_sample_master_row(), "viewable_media_type": "VIDEO", "caption": "Father's Day special", "hashtags": "#fathersday"}, "Duderobe")
    merged = merge_tracker_row(existing, fresh)
    assert merged["Theme"] == "Gift-Giving"  # frozen, NOT recomputed


# ---------- the real bug: new column added after rows already existed ----------

def test_merge_gives_a_first_value_to_a_column_that_never_existed_before():
    # confirmed real bug: a row created BEFORE Content Type/Theme were
    # added to the schema has no key for them at all -- they must NOT
    # stay frozen at blank forever just because the row itself already
    # existed for other columns
    existing = build_fresh_tracker_row({**_sample_master_row()}, "Duderobe")
    del existing["Content Type"]  # simulates a row from before this column existed
    del existing["Theme"]
    fresh = build_fresh_tracker_row(
        {**_sample_master_row(), "viewable_media_type": "VIDEO", "caption": "Father's Day gift", "hashtags": "#fathersday"},
        "Duderobe",
    )
    merged = merge_tracker_row(existing, fresh)
    assert merged["Content Type"] == "UGC Video"
    assert merged["Theme"] == "Father's Day"


def test_merge_still_freezes_a_column_that_already_has_a_real_value():
    # the fix must NOT undo the original freeze behavior for a column
    # that genuinely already has a value
    existing = build_fresh_tracker_row({**_sample_master_row(), "viewable_media_type": "VIDEO"}, "Duderobe")
    existing["Content Type"] = "UGC Video"  # already set from an earlier run
    fresh = build_fresh_tracker_row({**_sample_master_row(), "viewable_media_type": "IMAGE"}, "Duderobe")
    merged = merge_tracker_row(existing, fresh)
    assert merged["Content Type"] == "UGC Video"  # NOT recomputed to UGC Photo


def test_merge_blank_freeze_column_can_still_pick_up_a_later_value():
    # if a freeze-once-set column is genuinely blank (not because it's
    # new, just because the source data was blank), it's allowed to
    # fill in once real source data appears -- "frozen" should mean
    # "protects a real value", not "permanently stuck at nothing"
    existing = build_fresh_tracker_row({**_sample_master_row(), "products": ""}, "Duderobe")
    assert existing["Product"] == ""  # blank, as expected
    fresh = build_fresh_tracker_row({**_sample_master_row(), "products": "The DudeRobe"}, "Duderobe")
    merged = merge_tracker_row(existing, fresh)
    assert merged["Product"] == "DudeRobe"


def test_sabotage_missing_column_bug_would_be_caught():
    existing = build_fresh_tracker_row({**_sample_master_row()}, "Duderobe")
    del existing["Theme"]
    fresh = build_fresh_tracker_row(
        {**_sample_master_row(), "caption": "Father's Day gift", "hashtags": "#fathersday"}, "Duderobe"
    )
    merged = merge_tracker_row(existing, fresh)
    with pytest.raises(AssertionError):
        assert merged["Theme"] == ""  # wrong -- confirmed real bug: frozen at blank forever
    assert merged["Theme"] == "Father's Day"  # confirms actual correct behavior


# ---------- derive_post_type ----------

def test_post_type_video():
    assert derive_post_type("VIDEO") == "Video"


def test_post_type_reels():
    assert derive_post_type("REELS") == "Reels"


def test_post_type_story():
    # confirmed real: Story IS a valid Post Type (unlike Content Type,
    # where it doesn't belong at all) -- cross-referenced against
    # platform in a real export: both Instagram+STORY and TikTok+STORY
    # exist
    assert derive_post_type("STORY") == "Story"


def test_post_type_private():
    assert derive_post_type("PRIVATE") == "Private"


def test_post_type_is_case_insensitive():
    assert derive_post_type("video") == "Video"


def test_post_type_blank_stays_blank():
    assert derive_post_type("") == ""
    assert derive_post_type(None) == ""


def test_sabotage_post_type_wrong_mapping_would_be_caught():
    result = derive_post_type("REELS")
    with pytest.raises(AssertionError):
        assert result == "Video"  # wrong -- that's a different mapping
    assert result == "Reels"


# ---------- Post Type wired into build_fresh_tracker_row / merge ----------

def test_build_fresh_tracker_row_includes_post_type():
    row = build_fresh_tracker_row({**_sample_master_row(), "post_type": "REELS"}, "Duderobe")
    assert row["Post Type"] == "Reels"


def test_merge_freezes_post_type_once_set():
    existing = build_fresh_tracker_row({**_sample_master_row(), "post_type": "VIDEO"}, "Duderobe")
    existing["Post Type"] = "Video"
    fresh = build_fresh_tracker_row({**_sample_master_row(), "post_type": "REELS"}, "Duderobe")
    merged = merge_tracker_row(existing, fresh)
    assert merged["Post Type"] == "Video"  # frozen, NOT recomputed to Reels


def test_merge_gives_post_type_a_first_value_for_a_pre_existing_row():
    # same migration scenario as Content Type/Theme: a row that existed
    # before Post Type was added to the schema must get a real first
    # value, not stay frozen at blank forever
    existing = build_fresh_tracker_row(_sample_master_row(), "Duderobe")
    del existing["Post Type"]
    fresh = build_fresh_tracker_row({**_sample_master_row(), "post_type": "STORY"}, "Duderobe")
    merged = merge_tracker_row(existing, fresh)
    assert merged["Post Type"] == "Story"


def test_sabotage_post_type_migration_bug_would_be_caught():
    existing = build_fresh_tracker_row(_sample_master_row(), "Duderobe")
    del existing["Post Type"]
    fresh = build_fresh_tracker_row({**_sample_master_row(), "post_type": "REELS"}, "Duderobe")
    merged = merge_tracker_row(existing, fresh)
    with pytest.raises(AssertionError):
        assert merged["Post Type"] == ""  # wrong -- would mean the migration bug is back
    assert merged["Post Type"] == "Reels"


def test_build_tracker_target_rows_end_to_end_includes_post_type():
    master_rows = {
        "tk_1": _sample_master_row(id="tk_1", post_type="VIDEO"),
        "tk_2": _sample_master_row(id="tk_2", post_type="REELS"),
    }
    target = build_tracker_target_rows(master_rows, "Duderobe", {})
    assert target["tk_1"]["Post Type"] == "Video"
    assert target["tk_2"]["Post Type"] == "Reels"


# ---------- merge_reviewed_from_master ----------

def test_merge_reviewed_from_master_pulls_in_a_yes():
    target_rows = {"tk_1": {"Reviewed": ""}}
    master_rows = {"tk_1": {"Reviewed": "Yes"}}
    merged = merge_reviewed_from_master(target_rows, master_rows)
    assert merged == 1
    assert target_rows["tk_1"]["Reviewed"] == "Yes"


def test_merge_reviewed_from_master_never_overwrites_an_existing_tracker_value():
    target_rows = {"tk_1": {"Reviewed": "Yes"}}
    master_rows = {"tk_1": {"Reviewed": ""}}  # blank in Master Data
    merged = merge_reviewed_from_master(target_rows, master_rows)
    assert merged == 0
    assert target_rows["tk_1"]["Reviewed"] == "Yes"  # untouched, not erased


def test_merge_reviewed_from_master_does_nothing_when_both_blank():
    target_rows = {"tk_1": {"Reviewed": ""}}
    master_rows = {"tk_1": {"Reviewed": ""}}
    merged = merge_reviewed_from_master(target_rows, master_rows)
    assert merged == 0
    assert target_rows["tk_1"]["Reviewed"] == ""


def test_merge_reviewed_from_master_handles_id_missing_from_master_rows():
    target_rows = {"tk_1": {"Reviewed": ""}}
    master_rows = {}  # id not present at all
    merged = merge_reviewed_from_master(target_rows, master_rows)
    assert merged == 0
    assert target_rows["tk_1"]["Reviewed"] == ""


def test_sabotage_reviewed_sync_direction_missing_would_be_caught():
    # this is the exact real scenario: 20 rows reviewed in Master Data,
    # 0 in the tracker -- confirms the fix actually closes that gap
    target_rows = {f"tk_{i}": {"Reviewed": ""} for i in range(20)}
    master_rows = {f"tk_{i}": {"Reviewed": "Yes"} for i in range(20)}
    merged = merge_reviewed_from_master(target_rows, master_rows)
    with pytest.raises(AssertionError):
        assert merged == 0  # wrong -- would mean the real bug is still there
    assert merged == 20  # confirms actual correct behavior
    assert all(row["Reviewed"] == "Yes" for row in target_rows.values())


# ---------- Reviewed sync now uses the shared strict rule (audit fix) ----------

def test_merge_reviewed_from_master_ignores_a_non_reviewed_value():
    # confirmed real bug: "No" used to be copied across as though it
    # meant reviewed, because any non-blank value counted
    target_rows = {"tk_1": {"Reviewed": ""}}
    master_rows = {"tk_1": {"Reviewed": "No"}}
    merged = merge_reviewed_from_master(target_rows, master_rows)
    assert merged == 0
    assert target_rows["tk_1"]["Reviewed"] == ""


def test_merge_reviewed_from_master_accepts_alternate_true_spellings():
    target_rows = {"tk_1": {"Reviewed": ""}, "tk_2": {"Reviewed": ""}}
    master_rows = {"tk_1": {"Reviewed": "TRUE"}, "tk_2": {"Reviewed": "y"}}
    merged = merge_reviewed_from_master(target_rows, master_rows)
    assert merged == 2


def test_merge_reviewed_overwrites_a_stale_non_reviewed_tracker_value():
    # tracker says "No" (not a reviewed marker), master says "Yes" --
    # the real marking should win rather than being blocked by noise
    target_rows = {"tk_1": {"Reviewed": "No"}}
    master_rows = {"tk_1": {"Reviewed": "Yes"}}
    merge_reviewed_from_master(target_rows, master_rows)
    assert target_rows["tk_1"]["Reviewed"] == "Yes"


def test_merge_reviewed_handles_none_valued_reviewed_cell():
    target_rows = {"tk_1": {"Reviewed": None}}
    master_rows = {"tk_1": {"Reviewed": None}}
    assert merge_reviewed_from_master(target_rows, master_rows) == 0


def test_sabotage_no_value_synced_as_reviewed_would_be_caught():
    target_rows = {"tk_1": {"Reviewed": ""}}
    master_rows = {"tk_1": {"Reviewed": "No"}}
    merge_reviewed_from_master(target_rows, master_rows)
    with pytest.raises(AssertionError):
        assert target_rows["tk_1"]["Reviewed"] == "No"  # wrong -- must not sync
    assert target_rows["tk_1"]["Reviewed"] == ""  # confirms actual correct behavior

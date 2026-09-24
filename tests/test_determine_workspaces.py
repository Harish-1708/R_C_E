import pytest

from determine_workspaces import select_workspaces, UnknownWorkspaceError

WORKSPACES = [
    {"name": "Duderobe", "schedule_enabled": True},
    {"name": "Swoveralls", "schedule_enabled": False},
    {"name": "Defi Snacks", "schedule_enabled": False},
    {"name": "Kelson", "schedule_enabled": False},
]


def test_scheduled_trigger_only_returns_enabled_workspaces():
    result = select_workspaces(WORKSPACES, event_name="schedule")
    assert [w["name"] for w in result] == ["Duderobe"]


def test_manual_trigger_with_no_input_matches_scheduled_set():
    result = select_workspaces(WORKSPACES, event_name="workflow_dispatch", workspace_input=None)
    assert [w["name"] for w in result] == ["Duderobe"]


def test_manual_trigger_all_enabled_matches_scheduled_set():
    result = select_workspaces(WORKSPACES, event_name="workflow_dispatch", workspace_input="all-enabled")
    assert [w["name"] for w in result] == ["Duderobe"]


def test_manual_trigger_all_returns_every_workspace_regardless_of_flag():
    result = select_workspaces(WORKSPACES, event_name="workflow_dispatch", workspace_input="all")
    assert [w["name"] for w in result] == ["Duderobe", "Swoveralls", "Defi Snacks", "Kelson"]


def test_manual_trigger_specific_disabled_workspace_still_runs():
    # this is the core requirement: schedule off doesn't block manual runs
    result = select_workspaces(WORKSPACES, event_name="workflow_dispatch", workspace_input="Swoveralls")
    assert [w["name"] for w in result] == ["Swoveralls"]


def test_manual_trigger_is_case_insensitive():
    result = select_workspaces(WORKSPACES, event_name="workflow_dispatch", workspace_input="swoveralls")
    assert [w["name"] for w in result] == ["Swoveralls"]


def test_manual_trigger_unknown_workspace_raises_with_known_list():
    with pytest.raises(UnknownWorkspaceError, match="Duderobe"):
        select_workspaces(WORKSPACES, event_name="workflow_dispatch", workspace_input="NotARealBrand")


def test_no_workspaces_enabled_returns_empty_on_schedule():
    all_disabled = [dict(w, schedule_enabled=False) for w in WORKSPACES]
    result = select_workspaces(all_disabled, event_name="schedule")
    assert result == []


# ---------- sabotage tests ----------

def test_sabotage_forgetting_schedule_filter_would_be_caught():
    # if select_workspaces ignored schedule_enabled entirely on a
    # scheduled trigger, this would return all 4 instead of 1
    result = select_workspaces(WORKSPACES, event_name="schedule")
    with pytest.raises(AssertionError):
        assert len(result) == 4
    assert len(result) == 1


def test_sabotage_manual_disabled_workspace_blocked_would_be_caught():
    # proves manual override actually works, not just returning [] for
    # a disabled workspace
    result = select_workspaces(WORKSPACES, event_name="workflow_dispatch", workspace_input="Kelson")
    with pytest.raises(AssertionError):
        assert result == []
    assert [w["name"] for w in result] == ["Kelson"]


# ---------- a schedule-triggered run can be pinned to one brand (dedicated per-brand workflows) ----------

def test_scheduled_trigger_with_a_specific_workspace_input_returns_only_that_one():
    # CONFIRMED REAL need: a dedicated, per-brand workflow (Swoveralls
    # on its own scheduled window, Duderobe on another) is itself
    # schedule-triggered, but must always resolve to that ONE brand --
    # even though Swoveralls has schedule_enabled: False in this set.
    result = select_workspaces(WORKSPACES, event_name="schedule", workspace_input="Swoveralls")
    assert [w["name"] for w in result] == ["Swoveralls"]


def test_scheduled_trigger_with_no_input_is_completely_unaffected():
    # an ordinary scheduled run (the shared full-pipeline workflow)
    # never sets this input -- must behave exactly as before
    result = select_workspaces(WORKSPACES, event_name="schedule", workspace_input=None)
    assert [w["name"] for w in result] == ["Duderobe"]


def test_scheduled_trigger_with_all_enabled_input_matches_the_scheduled_set():
    result = select_workspaces(WORKSPACES, event_name="schedule", workspace_input="all-enabled")
    assert [w["name"] for w in result] == ["Duderobe"]


def test_scheduled_trigger_with_unknown_workspace_input_raises():
    with pytest.raises(UnknownWorkspaceError) as exc:
        select_workspaces(WORKSPACES, event_name="schedule", workspace_input="Nonexistent")
    assert "Duderobe" in str(exc.value)


def test_sabotage_a_dedicated_scheduled_workflow_picking_up_every_brand_would_be_caught():
    result = select_workspaces(WORKSPACES, event_name="schedule", workspace_input="Swoveralls")
    with pytest.raises(AssertionError):
        # wrong -- would mean the "Swoveralls-only" workflow silently
        # ran every enabled brand instead, defeating the whole point
        # of a dedicated per-brand schedule
        assert [w["name"] for w in result] == ["Duderobe"]
    assert [w["name"] for w in result] == ["Swoveralls"]

"""
determine_workspaces.py

Decides which workspace(s) a given GitHub Actions run should sync,
based on config/workspaces.yaml and how the workflow was triggered:

    - scheduled run           -> only workspaces with schedule_enabled: true
    - manual run, no input / "all-enabled" -> same as scheduled (the normal set)
    - manual run, "all"       -> every configured workspace, ignoring schedule_enabled
    - manual run, a specific workspace name -> just that one, regardless
      of its schedule_enabled setting (manual runs always work, per your
      "off schedule, but can still run manually" requirement)

select_workspaces() is pure (config in, list out) so it's fully testable
without touching GitHub Actions. main() is the thin CLI wrapper the
workflow actually calls, which prints a single-line JSON array to stdout
for the workflow to capture into a matrix.
"""

from __future__ import annotations

import json
import os
import sys
from typing import List, Optional

import yaml


class UnknownWorkspaceError(ValueError):
    pass


def load_config(path: str = "config/workspaces.yaml") -> List[dict]:
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data.get("workspaces", [])


def select_workspaces(
    workspaces: List[dict],
    event_name: str,
    workspace_input: Optional[str] = None,
) -> List[dict]:
    scheduled_set = [w for w in workspaces if w.get("schedule_enabled")]

    # CONFIRMED REAL need this addition covers: a dedicated, per-brand
    # workflow (e.g. Swoveralls on its own 12-hour window, Duderobe on
    # the other) is itself schedule-triggered, but must always resolve
    # to that ONE brand, never the full scheduled_set. Previously
    # workspace_input was read only for workflow_dispatch, so a
    # schedule-triggered run had no way to narrow itself at all.
    # Scoped narrowly: only when a specific, non-"all"/"all-enabled"
    # name is explicitly given does this apply to a scheduled run --
    # an ordinary scheduled run (no input set) is completely unaffected
    # and keeps returning the full scheduled_set exactly as before.
    if event_name == "schedule":
        choice = (workspace_input or "").strip()
        if choice and choice.lower() not in ("all", "all-enabled"):
            for w in workspaces:
                if w.get("name", "").lower() == choice.lower():
                    return [w]
            known = ", ".join(w.get("name", "") for w in workspaces)
            raise UnknownWorkspaceError(f"'{choice}' doesn't match any configured workspace. Known: {known}")
        return scheduled_set

    # workflow_dispatch (or anything else -- treated the same as manual)
    choice = (workspace_input or "all-enabled").strip()

    if choice == "" or choice.lower() == "all-enabled":
        return scheduled_set

    if choice.lower() == "all":
        return list(workspaces)

    for w in workspaces:
        if w.get("name", "").lower() == choice.lower():
            return [w]

    known = ", ".join(w.get("name", "") for w in workspaces)
    raise UnknownWorkspaceError(f"'{choice}' doesn't match any configured workspace. Known: {known}")


def main() -> int:
    event_name = os.environ.get("GITHUB_EVENT_NAME", "workflow_dispatch")
    workspace_input = os.environ.get("WORKSPACE_INPUT")

    try:
        workspaces = load_config()
        selected = select_workspaces(workspaces, event_name, workspace_input)
    except (UnknownWorkspaceError, FileNotFoundError, yaml.YAMLError) as e:
        print(f"determine_workspaces failed: {e}", file=sys.stderr)
        return 1

    if not selected:
        print(
            "determine_workspaces: no workspaces selected (schedule_enabled is false "
            "for all of them, and this wasn't a manual run with a specific choice).",
            file=sys.stderr,
        )

    print(json.dumps(selected))
    return 0


if __name__ == "__main__":
    sys.exit(main())

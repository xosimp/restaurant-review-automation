"""Every Ask tool is classified (release-safety audit 10/26/26, A04-09).

A tool reaches a module's data either through its `module` flag (tool_allowed
drops it when the viewer_restaurant has that module off — off for the plan or
for the login's role), through a name set tool_allowed reads (intel, the DSR,
principal-only, staff contact, team settings), or through a per-viewer
projection inside the tool. The last kind is the one a new tool can get wrong
silently, so each such tool is listed in ask_cavnar_tools._MODULELESS_REVIEWED
with its reason — the Ask twin of auth._UNGATED_PREFIXES for routes.
"""
import inspect
import json

import pytest

import ask_cavnar_tools as tools
import models
from models import Restaurant, create_restaurant, get_restaurant

OWNER = {"id": 1, "role": "client", "is_admin": 0, "username": "erik"}
MANAGER = {"id": 2, "role": "manager", "is_admin": 0, "username": "dana"}

# The name sets tool_allowed gates a module-None tool by.
GATING_SETS = {
    "_INTEL_TOOLS": tools._INTEL_TOOLS,
    "_DSR_TOOLS": tools._DSR_TOOLS,
    "_PRINCIPAL_TOOLS": tools._PRINCIPAL_TOOLS,
    "_STAFF_CONTACT_TOOLS": tools._STAFF_CONTACT_TOOLS,
    "_TEAM_SETTINGS_TOOLS": tools._TEAM_SETTINGS_TOOLS,
}


def _name(t):
    return t["spec"]["name"]


def _gated_by_set(name):
    return [k for k, s in GATING_SETS.items() if name in s]


def test_every_moduleless_tool_is_in_a_name_set_or_reviewed():
    unclassified = [_name(t) for t in tools.TOOLS
                    if not t.get("module") and not _gated_by_set(_name(t))
                    and _name(t) not in tools._MODULELESS_REVIEWED]
    assert unclassified == [], (
        "a tool with no module flag must be gated by a tool_allowed name set or listed in "
        f"ask_cavnar_tools._MODULELESS_REVIEWED with the projection it applies: {unclassified}")


def test_every_reviewed_entry_is_a_real_moduleless_ungated_tool_with_a_reason():
    for name, why in tools._MODULELESS_REVIEWED.items():
        assert name in tools._BY_NAME, f"stale _MODULELESS_REVIEWED entry: {name}"
        t = tools._BY_NAME[name]
        assert not t.get("module"), f"{name} has a module flag ({t['module']}); drop it from the reviewed list"
        assert not _gated_by_set(name), f"{name} is gated by {_gated_by_set(name)}; drop it from the reviewed list"
        assert isinstance(why, str) and len(why.strip()) >= 20, f"{name}: say why it is safe for every login"


@pytest.mark.parametrize("set_name", sorted(GATING_SETS))
def test_every_name_set_entry_names_a_real_tool(set_name):
    stale = sorted(n for n in GATING_SETS[set_name] if n not in tools._BY_NAME)
    assert stale == [], f"{set_name} names tools that do not exist: {stale}"


def test_a_tool_whose_function_projects_by_viewer_is_handed_the_viewer():
    """A projection inside a tool is dead code unless run_read_tool passes
    `_viewer` (the tool's `wants_viewer`): the tool would then answer every
    login as an unrestricted caller."""
    mismatched = []
    for t in tools.TOOLS:
        fn = t.get("fn")
        if fn is None:
            continue
        takes = "_viewer" in inspect.signature(fn).parameters
        if takes != bool(t.get("wants_viewer")):
            mismatched.append(_name(t))
    assert mismatched == []


# ── behaviour: a manager (no FOOD_COST_VIEW) through the real gates ─────────

@pytest.fixture
def rid(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    return create_restaurant(Restaurant(name="Classify Co", owner_email="c@x.test", timezone="America/Chicago",
                                        module_reviews=1, module_labor=1, module_inventory=1,
                                        module_marketing=1), db_path=db_path)


def _view(rid, user):
    return tools.viewer_restaurant(get_restaurant(rid), user)


def test_a_manager_is_refused_every_food_cost_tool_and_the_principal_tools(rid):
    manager, owner = _view(rid, MANAGER), _view(rid, OWNER)
    food = [_name(t) for t in tools.TOOLS if t.get("module") == "module_inventory"]
    assert food, "no Food Cost tool found — the module flag strings changed"
    for name in food + sorted(tools._PRINCIPAL_TOOLS):
        assert not tools.tool_allowed(name, manager), name
        assert tools.tool_allowed(name, owner), name
    reads = [n for n in food if tools._BY_NAME[n]["kind"] == "read"]
    for name in reads:
        out = json.loads(tools.run_read_tool(name, rid, {}, restaurant=manager))
        assert "not available to this login" in (out.get("error") or ""), name


def test_read_goals_shows_a_food_cost_goal_to_the_owner_and_not_to_a_manager(rid, db_path):
    import goals
    goals.set_goal(rid, "food_cost_pct", 30, user_id=OWNER["id"], authority="principal", db_path=db_path)
    owner_out = json.loads(tools.run_read_tool("read_goals", rid, {}, restaurant=_view(rid, OWNER)))
    manager_out = json.loads(tools.run_read_tool("read_goals", rid, {}, restaurant=_view(rid, MANAGER)))
    assert any(g.get("metric") == "food_cost_pct" for g in owner_out["goals"] + owner_out["waiting_for_the_owner"])
    assert not any(g.get("metric") == "food_cost_pct"
                   for g in manager_out["goals"] + manager_out["waiting_for_the_owner"])

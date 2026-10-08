"""Context re-audit 10/7/26 (restaurant_context, the page reads, the DSR
narrative, Ask) — the fixes, held on their real paths.

  #1  a view-as session (the owner's login dict + acting_admin_id) and the
      owner never share one cached memory section or Ask snapshot: support
      never reads the owner's author-only line, and the owner is never
      served support's copy — in either order.
"""
import sys

import pytest

import models
import owner_memory
import restaurant_context as rc
from models import Restaurant, create_restaurant

OWNER_ID = 11


@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    real = models.get_conn

    def conn(*a, **k):
        return real(db_path)
    for mod in list(sys.modules.values()):
        if mod is not None and getattr(mod, "get_conn", None) is real:
            monkeypatch.setattr(mod, "get_conn", conn)
    monkeypatch.setattr(models, "get_conn", conn)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    import webhooks
    monkeypatch.setattr(webhooks, "fire_webhook", lambda *a, **k: None)
    import ask_cavnar
    rc.invalidate()
    ask_cavnar._CONTEXT_CACHE.clear()
    yield
    rc.invalidate()
    ask_cavnar._CONTEXT_CACHE.clear()


def _rid(name="Reaudit Grill"):
    return create_restaurant(Restaurant(name=name, owner_email=f"{name.split()[0].lower()}@x.test",
                                        timezone="America/Chicago"))


def _owner(rid):
    return {"id": OWNER_ID, "restaurant_id": rid, "role": "client", "is_admin": 0, "username": "erik"}


def _view_as(rid, admin_id=99):
    return dict(_owner(rid), acting_admin_id=admin_id, acting_admin="sam", acting_admin_role="support",
                device_type="admin-view-as")


PRIVATE = "Remind me to call the landlord about the lease"


def _memory(rid, viewer):
    return rc.section(rid, "memory", viewer=viewer,
                      params={"surface": "ask", "budget_chars": 4000, "whole": True})


@pytest.mark.parametrize("first", ["owner", "view_as"])
def test_view_as_and_the_owner_never_share_a_memory_section(first):
    rid = _rid()
    owner_memory.remember(rid, PRIVATE, audience="author", user=_owner(rid))
    order = [("owner", _owner(rid)), ("view_as", _view_as(rid))]
    if first == "view_as":
        order.reverse()
    seen = {who: _memory(rid, v) for who, v in order}
    assert PRIVATE in seen["owner"].text
    assert PRIVATE not in seen["view_as"].text
    assert seen["owner"].scope != seen["view_as"].scope
    # And again from L1 (the second read of each must hold too).
    assert PRIVATE in _memory(rid, _owner(rid)).text
    assert PRIVATE not in _memory(rid, _view_as(rid)).text
    # A cold process (L1 dropped) reads L2 under each one's own scope.
    rc.invalidate()
    assert PRIVATE in _memory(rid, _owner(rid)).text
    assert PRIVATE not in _memory(rid, _view_as(rid)).text


def test_the_scope_names_the_login_behind_a_view_as_and_two_admins_differ():
    rid = _rid()
    owner = rc.viewer_scope(_owner(rid), "memory")
    sam = rc.viewer_scope(_view_as(rid, 99), "memory")
    jo = rc.viewer_scope(_view_as(rid, 98), "memory")
    assert owner.startswith(f"login:{OWNER_ID}:")
    assert sam.startswith("login:99:") and jo.startswith("login:98:")
    assert len({owner, sam, jo}) == 3


@pytest.mark.parametrize("first", ["owner", "view_as"])
def test_view_as_and_the_owner_never_share_an_ask_snapshot(first):
    import ask_cavnar
    import ask_cavnar_tools
    rid = _rid()
    owner_memory.remember(rid, PRIVATE, audience="author", user=_owner(rid))
    r = models.get_restaurant(rid)
    order = [("owner", _owner(rid)), ("view_as", _view_as(rid))]
    if first == "view_as":
        order.reverse()
    seen = {who: ask_cavnar.build_context(ask_cavnar_tools.viewer_restaurant(r, u)) for who, u in order}
    assert PRIVATE in seen["owner"]
    assert PRIVATE not in seen["view_as"]


# ── #3 the canary ───────────────────────────────────────────────────────────

def _canary_runs(workflow):
    c = models.get_conn()
    try:
        return [dict(r) for r in c.execute("SELECT restaurant_id, start_tier, final_tier, canary FROM ai_runs "
                                           "WHERE workflow=? ORDER BY created_at, rowid", (workflow,)).fetchall()]
    finally:
        c.close()


@pytest.fixture
def orch_clean(monkeypatch):
    import ai_orchestrator as orch
    orch._OVERRIDES.clear()
    monkeypatch.setattr(orch, "REPLAY_SAMPLE_RATE", 0.0)
    monkeypatch.delenv("AI_CANARY_RESTAURANTS", raising=False)
    yield orch
    orch._OVERRIDES.clear()


def _passing_run(orch, workflow, rid, start=None):
    seen = []
    rr = orch.generate(workflow, rid, attempt=lambda route, notes: seen.append(route.tier) or "ok",
                       check=lambda r: orch.Verdict.passed(), start=start)
    return rr, seen


def test_a_canaried_workflow_starts_cheap_only_for_the_canary_restaurant(orch_clean):
    orch = orch_clean
    # The default canary is restaurant 4 (Simple EJ's Demo).
    rr, seen = _passing_run(orch, "labor_insight", 4)
    assert seen == ["T1"] and rr.tier == "T1"
    rr, seen = _passing_run(orch, "labor_insight", 5)
    assert seen == ["T2"] and rr.tier == "T2", "everyone else keeps the model the read ran on before"
    canary, held = _canary_runs("labor_insight")
    assert canary["canary"] == 1 and canary["start_tier"] is None
    # Held back a rung: its start_tier says so, so the learner's escalation
    # rate (start_tier NULL) reads the canary's runs only.
    assert held["canary"] == 0 and held["start_tier"] == "T2" and held["final_tier"] == "T2"


def test_a_held_back_run_has_nowhere_to_climb_and_a_pre_routed_start_stands(orch_clean):
    orch = orch_clean
    seen = []
    rr = orch.generate("labor_insight", 5, attempt=lambda route, notes: seen.append(route.tier) or "x",
                       check=lambda r: orch.Verdict.failed("validation_refuse", "an invented figure"))
    assert seen == ["T2"] and not rr.ok and rr.escalations == 0
    # A 3-star review the drafter pre-routes to T2 is T2 either way.
    assert _passing_run(orch, "draft_response", 4, start="T2")[1] == ["T2"]
    assert _passing_run(orch, "draft_response", 5, start="T2")[1] == ["T2"]
    assert _passing_run(orch, "draft_response", 5, start="T1")[1] == ["T2"]


def test_the_env_names_the_canary_and_the_console_turns_it_off(orch_clean, monkeypatch):
    import ai_workflows as wf
    orch = orch_clean
    monkeypatch.setenv("AI_CANARY_RESTAURANTS", "5, 9")
    assert wf.canary_restaurants() == frozenset({5, 9})
    assert _passing_run(orch, "staff_answer", 9)[1] == ["T1"]
    assert _passing_run(orch, "staff_answer", 4)[1] == ["T2"]
    monkeypatch.setenv("AI_CANARY_RESTAURANTS", "")
    assert _passing_run(orch, "task_sheet_starter", 4)[1] == ["T2"]
    # The console's override: the canary is over, every restaurant starts on T1.
    orch.set_override("task_sheet_starter", {"canary": False}, actor="test")
    assert wf.policy("task_sheet_starter").canary is False
    assert _passing_run(orch, "task_sheet_starter", 4)[1] == ["T1"]
    assert _canary_runs("task_sheet_starter")[-1]["canary"] is None
    # A workflow never canaried records NULL and runs its ladder as written.
    assert _passing_run(orch, "marketing_insight", 5)[1] == ["T1"]
    assert _canary_runs("marketing_insight")[-1]["canary"] is None


def test_every_workflow_whose_first_rung_is_cheaper_than_its_old_model_is_canaried():
    """Asserted against the registry, so a new Haiku-first policy on a
    Sonnet call site cannot ship to everyone on day one. labor_schedule is
    the owner's own decision (Sonnet 5.5 first on the stored-week eval)."""
    import ai_utils
    import ai_workflows as wf
    rank = {ai_utils.HAIKU: 1, ai_utils.SONNET: 2}
    exempt = {"labor_schedule"}
    for name, p in wf.POLICIES.items():
        if name in exempt or not p.purpose or p.ladder[0] not in ("T1", "T2"):
            continue
        before = ai_utils.MODELS[p.purpose][1]
        first = {"T1": ai_utils.HAIKU, "T2": ai_utils.SONNET}[p.ladder[0]]
        cheaper = before in rank and rank[first] < rank[before]
        assert p.canary == cheaper, name
    assert {n for n, p in wf.POLICIES.items() if p.canary} == {
        "labor_insight", "draft_response", "staff_answer", "task_sheet_starter"}


def test_the_canary_column_is_added_at_boot_to_an_old_table(tmp_path, monkeypatch):
    import sqlite3
    import ai_orchestrator as orch
    path = str(tmp_path / "old.db")
    c = sqlite3.connect(path)
    # The table as it was before the column (the schema less its last line).
    old = orch._SCHEMA.split("CREATE TABLE IF NOT EXISTS ai_runs (", 1)[1].split(");", 1)[0]
    old = old.replace(",\n    canary INTEGER", "")
    assert "canary" not in old
    c.execute("CREATE TABLE ai_runs (" + old + ")")
    c.commit()
    c.close()

    def conn(p=None):
        k = sqlite3.connect(p or path)
        k.row_factory = sqlite3.Row
        return k
    monkeypatch.setattr(orch, "_conn", conn)
    orch.init_ai_orchestration(path)
    c = sqlite3.connect(path)
    cols = {r[1] for r in c.execute("PRAGMA table_info(ai_runs)")}
    c.close()
    assert "canary" in cols
    orch.init_ai_orchestration(path)          # twice: no error


def test_the_canary_is_documented():
    env = open("docs/ops/ENVIRONMENT.md", encoding="utf-8").read()
    lib = open("PROMPT_LIBRARY.md", encoding="utf-8").read()
    assert "`AI_CANARY_RESTAURANTS`" in env and "AI_CANARY_RESTAURANTS" in lib and "ai_runs.canary" in lib

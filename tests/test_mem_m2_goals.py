"""Memory audit 9/29/26 (workstream M2, owner_goals): the owner's goal is the
target a module judges against, a teammate's goal waits for the owner, and
the schedule builds its week on the owner's own DSR budget.
"""
import json
from datetime import date, datetime, timedelta

import pytest
from flask import Flask

import ask_cavnar_tools as tools
import auth
import goals
import memory_context
import models
import owner_memory
import thresholds
from models import Restaurant, create_restaurant, get_restaurant


@pytest.fixture(autouse=True)
def _db(monkeypatch, db_path):
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    import metrics, schedule_economics
    for mod in (models, auth, goals, metrics, schedule_economics):
        monkeypatch.setattr(mod, "get_conn", redirect, raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    auth.init_auth(db_path=db_path)
    owner_memory.invalidate_targets()
    yield
    owner_memory.invalidate_targets()


def _rid(food=None, labor=None, **kw):
    """A restaurant; `food` / `labor` set that target as the owner's own."""
    kw.setdefault("name", "Goal Co")
    kw.setdefault("owner_email", "goal@x.test")
    rid = create_restaurant(Restaurant(**kw))
    conn = models.get_conn()
    if food is not None:
        conn.execute("UPDATE restaurants SET food_cost_target=?, food_cost_target_source='set' WHERE id=?", (food, rid))
    if labor is not None:
        conn.execute("UPDATE restaurants SET labor_target_pct=?, labor_target_source='set' WHERE id=?", (labor, rid))
    conn.commit()
    conn.close()
    return rid


def _owner(rid, uid=1):
    return {"id": uid, "restaurant_id": rid, "is_admin": 0, "role": "client", "username": "erik"}


def _manager(rid, uid=2):
    return {"id": uid, "restaurant_id": rid, "is_admin": 0, "role": "manager", "username": "dana"}


def _deadline(days=60):
    return (date.today() + timedelta(days=days)).isoformat()


# ── one target resolver, the goal first ──────────────────────────────────────

def test_the_owners_goal_is_the_target_every_module_reads():
    rid = _rid(food=30.0, labor=30.0)
    assert owner_memory.target_for(rid, "food_cost_pct") is None
    goals.set_goal(rid, "food_cost_pct", 28, deadline=_deadline(), user_id=1, authority="principal")
    r = get_restaurant(rid)
    t = thresholds.target_for(r, "food")
    assert t["pct"] == 28.0 and t["source"] == "goal"
    assert t["label"].startswith("your goal of 28% by ") and "20" not in t["label"].split("by ")[1][:3]
    assert t["setting"] == {"pct": 30.0, "source": "set", "label": "your target"}
    assert thresholds.target_value(r, "food") == 28.0
    assert thresholds.target_value(r, "labor") == 30.0, "a food goal moves only food"
    import notify
    assert notify.labor_target_for(r) == 30.0
    goals.set_goal(rid, "labor_pct", 26, user_id=1, authority="principal")
    assert notify.labor_target_for(get_restaurant(rid)) == 26.0
    ot = owner_memory.target_for(rid, "labor")
    assert ot["source"] == "goal" and ot["value"] == 26.0 and ot["until"] is None


def test_a_goal_whose_date_passed_stops_overriding_the_setting():
    rid = _rid(food=30.0)
    g = goals.set_goal(rid, "food_cost_pct", 28, deadline=_deadline(10), user_id=1, authority="principal")
    conn = models.get_conn()
    conn.execute("UPDATE owner_goals SET deadline=? WHERE id=?", ((date.today() - timedelta(days=1)).isoformat(),
                                                                  g["id"]))
    conn.commit()
    conn.close()
    owner_memory.invalidate_targets(rid)
    assert thresholds.target_for(get_restaurant(rid), "food")["source"] == "set"


def test_ending_a_goal_puts_the_setting_back_at_once():
    rid = _rid(food=31.0)
    g = goals.set_goal(rid, "food_cost_pct", 28, user_id=1, authority="principal")
    assert thresholds.target_value(get_restaurant(rid), "food") == 28.0
    goals.end_goal(rid, g["id"])
    assert thresholds.target_value(get_restaurant(rid), "food") == 31.0


def test_a_seeded_target_never_overwrites_a_setting_because_a_goal_exists():
    rid = _rid(labor=27.0)
    goals.set_goal(rid, "labor_pct", 25, user_id=1, authority="principal")
    assert thresholds.target_source(get_restaurant(rid), "labor") == "goal"
    assert thresholds.target_source(get_restaurant(rid), "labor", include_goal=False) == "set"


# ── a teammate's goal is proposed; the owner confirms ───────────────────────

def test_a_managers_goal_through_ask_is_proposed_and_changes_nothing_until_confirmed():
    rid = _rid(labor=30.0)
    view = tools.viewer_restaurant(get_restaurant(rid), _manager(rid))
    out = json.loads(tools.run_read_tool("set_goal", rid, {"metric": "labor_pct", "target": 25}, restaurant=view))
    assert out["proposed"] is True and "owner" in out["note"]
    assert goals.progress(rid) == [] and thresholds.target_value(get_restaurant(rid), "labor") == 30.0
    prop = goals.proposed(rid)[0]
    assert prop["created_by"] == 2 and prop["authority"] == "delegate" and prop["source"] == "ask"
    assert goals.confirm_goal(rid, prop["id"], user_id=1)["target"] == 25
    assert thresholds.target_value(get_restaurant(rid), "labor") == 25.0


def test_an_owners_goal_through_ask_is_active_and_carries_who_set_it():
    rid = _rid()
    view = tools.viewer_restaurant(get_restaurant(rid), _owner(rid))
    out = json.loads(tools.run_read_tool("set_goal", rid, {"metric": "labor_pct", "target": 27}, restaurant=view))
    assert out["ok"] and not out.get("proposed")
    g = goals.progress(rid)[0]
    assert g["created_by"] == 1 and g["authority"] == "principal" and g["confirmed_by"] == 1


@pytest.fixture
def client(db_path):
    import strategy_routes
    app = Flask(__name__)
    app.register_blueprint(strategy_routes.strategy_mobile_bp)
    return app.test_client()


def _bearer(db_path, rid, username, role=None):
    uid = auth.create_user(rid, username, f"{username}@x.test", "pw", db_path=db_path)
    if role:
        conn = models.get_conn()
        conn.execute("UPDATE users SET role=? WHERE id=?", (role, uid))
        conn.commit()
        conn.close()
    return uid, {"Authorization": f"Bearer {auth.create_session(uid, db_path=db_path)}"}


def test_the_goals_routes_propose_for_a_manager_and_confirm_for_the_owner(client, db_path):
    rid = _rid(module_labor=1)
    _o, owner_h = _bearer(db_path, rid, "erik")
    _m, mgr_h = _bearer(db_path, rid, "dana", role="manager")
    r = client.post("/mobile/api/goals", headers=mgr_h, json={"metric": "labor_pct", "target": 24}).get_json()
    assert r["ok"] and r["proposed"] is True
    listing = client.get("/mobile/api/goals", headers=owner_h).get_json()
    assert listing["goals"] == [] and listing["can_confirm"] is True
    prop = listing["proposed"][0]
    assert prop["proposed_by"] == "Dana, manager" and prop["summary"].startswith("Labor % 24%")
    assert client.post(f"/mobile/api/goals/{prop['id']}/confirm", headers=mgr_h).status_code == 403
    ok = client.post(f"/mobile/api/goals/{prop['id']}/confirm", headers=owner_h).get_json()
    assert ok["ok"] and ok["goal"]["status"] == "active"
    assert client.get("/mobile/api/goals", headers=owner_h).get_json()["goals"][0]["target"] == 24


# ── goals reach the prompts for the metrics in play ─────────────────────────

def test_goal_lines_serve_each_surface_its_own_metrics():
    rid = _rid()
    goals.set_goal(rid, "labor_pct", 26, deadline=_deadline(), user_id=1, authority="principal")
    goals.set_goal(rid, "food_cost_pct", 28, user_id=1, authority="principal")
    labor = memory_context.memory_context(rid, "labor_read").text
    food = memory_context.memory_context(rid, "food_read").text
    ask = memory_context.memory_context(rid, "ask").text
    assert "Labor %" in labor and "Food cost" not in labor
    assert "Food cost" in food and "Labor %" not in food
    assert "Labor %" in ask and "Food cost" in ask
    assert "THE OWNER'S GOALS" in ask
    # a manager's view of Ask drops the food-cost goal (no Food Cost view)
    mgr = memory_context.memory_context(rid, "ask", viewer=_manager(rid)).text
    assert "Food cost" not in mgr and "Labor %" in mgr


def test_asks_snapshot_carries_the_goals():
    import ask_cavnar
    rid = _rid()
    goals.set_goal(rid, "labor_pct", 26, user_id=1, authority="principal")
    text = ask_cavnar.build_context(get_restaurant(rid))
    assert "Goal — Labor %" in text


# ── the schedule reads the owner's own budget for the week ──────────────────

def test_the_schedules_week_revenue_is_the_owners_dsr_budget_when_most_nights_are_budgeted(db_path):
    import schedule_economics as econ
    rid = _rid()
    week = [(date(2026, 10, 5) + timedelta(days=i)).isoformat() for i in range(7)]
    conn = models.get_conn()
    for d in week[:4]:
        conn.execute("INSERT OR REPLACE INTO dsr_budgets (restaurant_id, business_date, net) VALUES (?,?,?)",
                     (rid, d, 5000))
    conn.commit()
    conn.close()
    assert econ.budgeted_week_revenue(rid, week, db_path=db_path)["value"] is None, "4 of 7 is not enough"
    conn = models.get_conn()
    for d in week[4:6]:
        conn.execute("INSERT OR REPLACE INTO dsr_budgets (restaurant_id, business_date, net) VALUES (?,?,?)",
                     (rid, d, 9000))
    conn.commit()
    conn.close()
    out = econ.projected_weekly_revenue(rid, db_path=db_path, week_dates=week)
    assert out["value"] == 4 * 5000 + 2 * 9000
    assert out["source"].startswith("your budget (6 of 7 nights budgeted")

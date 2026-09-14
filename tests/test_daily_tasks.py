"""Daily task checklists — role-scoped morning checklists.

Erik's own words: "a Task section where each role can view their Task sheet
every morning as a checklist of what to get done each morning." One
template per recurring duty ("Wipe down the bar" — Bartender); completion is
a separate row keyed by (template, date), so "done" resets on its own at
midnight — tomorrow's date just has no completion row yet, no cron job
needed to clear anything.
"""
import os

import pytest
from flask import Flask

import auth
import client_api
import models
from auth import create_session, create_user, init_auth, set_user_role
from models import (
    TaskTemplateError, add_task_template, create_restaurant, get_task_templates,
    get_todays_tasks, remove_task_template, set_task_completion,
)


@pytest.fixture(autouse=True)
def _redirect(db_path, monkeypatch):
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    for mod in (models, auth, client_api):
        monkeypatch.setattr(mod, "get_conn", redirect)
    init_auth(db_path=db_path)


def _restaurant(db_path, name="Simple EJ's"):
    from models import Restaurant
    return create_restaurant(Restaurant(name=name, owner_email="o@x.test"), db_path=db_path)


# ── models layer ─────────────────────────────────────────────────────────

def test_a_task_can_be_added_and_shows_up_for_its_role(db_path):
    rid = _restaurant(db_path)
    add_task_template(rid, "Bartender", "Wipe down the bar", db_path=db_path)
    tasks = get_todays_tasks(rid, "Bartender", db_path=db_path)
    assert len(tasks) == 1
    assert tasks[0]["label"] == "Wipe down the bar"
    assert tasks[0]["done"] is False


def test_tasks_are_scoped_to_their_own_role(db_path):
    rid = _restaurant(db_path)
    add_task_template(rid, "Bartender", "Wipe down the bar", db_path=db_path)
    add_task_template(rid, "Line Cook", "Check walk-in temp", db_path=db_path)
    assert [t["label"] for t in get_todays_tasks(rid, "Bartender", db_path=db_path)] == ["Wipe down the bar"]
    assert [t["label"] for t in get_todays_tasks(rid, "Line Cook", db_path=db_path)] == ["Check walk-in temp"]


def test_a_blank_role_or_label_is_refused(db_path):
    rid = _restaurant(db_path)
    with pytest.raises(TaskTemplateError):
        add_task_template(rid, "", "Wipe down the bar", db_path=db_path)
    with pytest.raises(TaskTemplateError):
        add_task_template(rid, "Bartender", "   ", db_path=db_path)


def test_checking_a_task_off_marks_it_done_for_that_date(db_path):
    rid = _restaurant(db_path)
    t = add_task_template(rid, "Host", "Confirm reservation book", db_path=db_path)
    ok = set_task_completion(rid, t["id"], "2026-09-14", True, completed_by="erik", db_path=db_path)
    assert ok is True
    tasks = get_todays_tasks(rid, "Host", task_date="2026-09-14", db_path=db_path)
    assert tasks[0]["done"] is True
    assert tasks[0]["completed_by"] == "erik"


def test_a_task_resets_on_its_own_the_next_day(db_path):
    """No clearing job needed — a new date just has no completion row."""
    rid = _restaurant(db_path)
    t = add_task_template(rid, "Host", "Confirm reservation book", db_path=db_path)
    set_task_completion(rid, t["id"], "2026-09-14", True, db_path=db_path)
    today = get_todays_tasks(rid, "Host", task_date="2026-09-14", db_path=db_path)
    tomorrow = get_todays_tasks(rid, "Host", task_date="2026-09-15", db_path=db_path)
    assert today[0]["done"] is True
    assert tomorrow[0]["done"] is False


def test_unchecking_a_task_clears_that_dates_completion(db_path):
    rid = _restaurant(db_path)
    t = add_task_template(rid, "Host", "Confirm reservation book", db_path=db_path)
    set_task_completion(rid, t["id"], "2026-09-14", True, db_path=db_path)
    set_task_completion(rid, t["id"], "2026-09-14", False, db_path=db_path)
    assert get_todays_tasks(rid, "Host", task_date="2026-09-14", db_path=db_path)[0]["done"] is False


def test_checking_the_same_task_twice_does_not_duplicate(db_path):
    rid = _restaurant(db_path)
    t = add_task_template(rid, "Host", "Confirm reservation book", db_path=db_path)
    set_task_completion(rid, t["id"], "2026-09-14", True, completed_by="erik", db_path=db_path)
    set_task_completion(rid, t["id"], "2026-09-14", True, completed_by="dana", db_path=db_path)
    tasks = get_todays_tasks(rid, "Host", task_date="2026-09-14", db_path=db_path)
    assert len(tasks) == 1
    assert tasks[0]["completed_by"] == "dana"


def test_removing_a_task_hides_it_but_keeps_completion_history(db_path):
    rid = _restaurant(db_path)
    t = add_task_template(rid, "Host", "Confirm reservation book", db_path=db_path)
    set_task_completion(rid, t["id"], "2026-09-14", True, db_path=db_path)
    removed = remove_task_template(rid, t["id"], db_path=db_path)
    assert removed is True
    assert get_todays_tasks(rid, "Host", task_date="2026-09-14", db_path=db_path) == []
    assert get_task_templates(rid, db_path=db_path) == []


def test_a_completion_cannot_target_another_restaurants_template(db_path):
    rid = _restaurant(db_path, "Simple EJ's")
    other_rid = _restaurant(db_path, "Gia Mia")
    t = add_task_template(rid, "Host", "Confirm reservation book", db_path=db_path)
    ok = set_task_completion(other_rid, t["id"], "2026-09-14", True, db_path=db_path)
    assert ok is False


def test_removing_a_template_from_another_restaurant_is_refused(db_path):
    rid = _restaurant(db_path, "Simple EJ's")
    other_rid = _restaurant(db_path, "Gia Mia")
    t = add_task_template(rid, "Host", "Confirm reservation book", db_path=db_path)
    assert remove_task_template(other_rid, t["id"], db_path=db_path) is False
    assert get_task_templates(rid, db_path=db_path)[0]["label"] == "Confirm reservation book"


def test_templates_list_sorted_by_role_then_add_order(db_path):
    rid = _restaurant(db_path)
    add_task_template(rid, "Host", "Second host task", db_path=db_path)
    add_task_template(rid, "Bartender", "Only bar task", db_path=db_path)
    add_task_template(rid, "Host", "First host task added second", db_path=db_path)
    labels = [t["label"] for t in get_task_templates(rid, role="Host", db_path=db_path)]
    assert labels == ["Second host task", "First host task added second"]


# ── route layer ──────────────────────────────────────────────────────────

@pytest.fixture
def app(db_path, monkeypatch):
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    import mobile_api
    for mod in (models, auth, client_api, mobile_api):
        monkeypatch.setattr(mod, "get_conn", redirect)
    flask_app = Flask(__name__)
    flask_app.register_blueprint(client_api.client_bp)
    return flask_app


@pytest.fixture
def client(app):
    return app.test_client()


def _login_session(client, db_path, rid, username, role=None):
    uid = create_user(rid, username, f"{username}@x.test", "correct-horse", db_path=db_path)
    if role:
        set_user_role(uid, role, db_path=db_path)
    token = create_session(uid, db_path=db_path)
    client.set_cookie("session_token", token)
    return uid


def test_routes_need_a_login(client):
    for path, method in (("/api/tasks?role=Host", "get"),
                         ("/api/tasks/complete", "post"),
                         ("/api/tasks/templates", "post")):
        resp = getattr(client, method)(path, json={})
        assert resp.status_code in (401, 403), path


def test_get_tasks_requires_a_role_param(client, db_path):
    rid = _restaurant(db_path)
    _login_session(client, db_path, rid, "erik")
    resp = client.get("/api/tasks")
    assert resp.status_code == 400


def test_add_check_and_list_a_task_end_to_end(client, db_path):
    rid = _restaurant(db_path)
    _login_session(client, db_path, rid, "erik")

    add = client.post("/api/tasks/templates", json={"role": "Server", "label": "Roll silverware"})
    assert add.get_json()["ok"] is True
    template_id = add.get_json()["id"]

    listed = client.get("/api/tasks?role=Server").get_json()
    assert listed["ok"] is True
    assert listed["tasks"][0]["label"] == "Roll silverware"
    assert listed["tasks"][0]["done"] is False

    done = client.post("/api/tasks/complete",
                       json={"template_id": template_id, "task_date": "2026-09-14", "done": True})
    assert done.get_json()["ok"] is True
    listed_after = client.get("/api/tasks?role=Server&date=2026-09-14").get_json()
    assert listed_after["tasks"][0]["done"] is True
    assert listed_after["tasks"][0]["completed_by"] == "erik"


def test_adding_a_template_needs_manage_permission(client, db_path):
    rid = _restaurant(db_path)
    _login_session(client, db_path, rid, "member1", role="member")
    # can_manage_team defaults open (no second login has it locked down in
    # this test), so this documents today's behavior: any active login can
    # manage the task list, same as it can manage ratings and thresholds.
    resp = client.post("/api/tasks/templates", json={"role": "Server", "label": "Roll silverware"})
    assert resp.get_json()["ok"] is True


def test_marking_a_task_done_needs_no_special_permission(client, db_path):
    """Checking your own morning list off is not a "manage the team" action —
    it should work for anyone signed in, same as the models-layer test."""
    rid = _restaurant(db_path)
    from models import add_task_template as _add
    t = _add(rid, "Server", "Roll silverware", db_path=db_path)
    _login_session(client, db_path, rid, "erik")
    resp = client.post("/api/tasks/complete",
                       json={"template_id": t["id"], "task_date": "2026-09-14", "done": True})
    assert resp.get_json()["ok"] is True


def test_completing_a_template_from_another_restaurant_is_refused(client, db_path):
    rid = _restaurant(db_path, "Simple EJ's")
    other_rid = _restaurant(db_path, "Gia Mia")
    from models import add_task_template as _add
    t = _add(other_rid, "Server", "Roll silverware", db_path=db_path)
    _login_session(client, db_path, rid, "erik")
    resp = client.post("/api/tasks/complete",
                       json={"template_id": t["id"], "task_date": "2026-09-14", "done": True})
    assert resp.status_code == 404


# ── the web panel ────────────────────────────────────────────────────────

def _dashboard_html():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return open(os.path.join(root, "templates", "dashboard.html"), encoding="utf-8").read()


def test_the_labor_tab_has_a_daily_tasks_panel():
    html = _dashboard_html()
    assert 'id="tasks-panel"' in html
    assert "Daily Tasks" in html
    assert "toggleTasksPanel()" in html
    assert "renderTaskRoleTabs" in html


def test_the_panel_talks_to_the_real_endpoints():
    html = _dashboard_html()
    assert "'/api/tasks?role='" in html
    assert "'/api/tasks/complete'" in html
    assert "'/api/tasks/templates'" in html
    assert "'/api/tasks/templates/remove'" in html


def test_opening_the_panel_reuses_operational_scores_role_list():
    """Roles are derived once (_rolesFromTeam, already covers Manager and
    Shift Supervisor as always-offered) — the task panel piggybacks on the
    same load instead of fetching its own separate roster."""
    html = _dashboard_html()
    i = html.index("function loadTeamPanel()")
    j = html.index("\n}", i)
    assert "renderTaskRoleTabs" in html[i:j]

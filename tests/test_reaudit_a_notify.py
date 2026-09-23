"""Notifications, issues and Ask (re-audit A-5 … A-28, notification side).

Nothing is sent: SMS, email and push are recorded by stubs, and no model
is called.
"""
import json
import os
import sys
from datetime import date, datetime, timedelta

import pytest

import auth
import models
import notify
import push
import webhooks
from models import Restaurant, create_restaurant, get_conn, update_restaurant

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(autouse=True)
def _world(monkeypatch, db_path):
    import ops
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    for mod in list(sys.modules.values()):
        try:
            if getattr(mod, "get_conn", None) is real:
                monkeypatch.setattr(mod, "get_conn", redirect)
        except Exception:
            pass
    for mod in (models, notify, auth, push, webhooks, ops):
        monkeypatch.setattr(mod, "get_conn", redirect, raising=False)
        monkeypatch.setattr(mod, "DB_PATH", db_path, raising=False)
    auth.init_auth(db_path=db_path)
    push.init_push(db_path=db_path)
    webhooks.init_webhooks(db_path)
    claimed = set()

    def _claim(job, period):
        if (job, period) in claimed:
            return False
        claimed.add((job, period))
        return True
    monkeypatch.setattr(ops, "claim_period", _claim)


def _rid(db_path, **kw):
    kw.setdefault("name", "Notice Co")
    kw.setdefault("owner_email", "o@x.test")
    kw.setdefault("timezone", "America/Chicago")
    return create_restaurant(Restaurant(**kw), db_path=db_path)


def _log(db_path, rid, alert_type, n=1):
    conn = get_conn(db_path)
    for _ in range(n):
        conn.execute("INSERT INTO alert_log (restaurant_id, alert_type, fired_at) VALUES (?,?,datetime('now'))",
                     (rid, alert_type))
    conn.commit()
    conn.close()


def _user(db_path, rid, role, email):
    conn = get_conn(db_path)
    uid = conn.execute("INSERT INTO users (username, email, password_hash, restaurant_id, role) VALUES (?,?,?,?,?)",
                       (email, email, "x", rid, role)).lastrowid
    conn.commit()
    conn.close()
    return {"id": uid, "role": role, "is_admin": 0, "email": email, "restaurant_id": rid}


# ── A-5: the briefing budget counts only briefings ─────────────────────────

def test_a_brief_and_three_issues_do_not_spend_the_budget(db_path):
    rid = _rid(db_path)
    _log(db_path, rid, "morning_brief")
    _log(db_path, rid, "issue", 3)
    _log(db_path, rid, "coverage", 2)
    _log(db_path, rid, "daily_briefing")
    assert notify.briefing_allowed(rid, "intraday_pulse", db_path) is True
    assert notify.briefing_allowed(rid, "closing_summary", db_path) is True


def test_real_briefings_still_spend_it(db_path):
    rid = _rid(db_path)
    _log(db_path, rid, "intraday_pulse", notify.BRIEFING_NORMAL_PER_DAY)
    assert notify.briefing_allowed(rid, "demand_opportunity", db_path) is False


# ── A-6: staff task notices have their own types ───────────────────────────

def test_a_staff_request_is_a_shift_request_notice(db_path, monkeypatch):
    import shift_requests, strategy_jobs
    sent = []
    monkeypatch.setattr(strategy_jobs, "_reach", lambda rid_, t, *a, **k: sent.append(t) or 1)
    shift_requests._tell_managers(_rid(db_path), "Drop request", "Ana asked to drop tonight.", db_path)
    assert sent == ["shift_request"]


def test_a_shift_request_is_heard_at_calm_and_does_not_break_focus(db_path):
    rid = _rid(db_path)
    update_restaurant(rid, {"briefing_level": "calm"}, db_path=db_path)
    for t in ("shift_request", "labor_reminder"):
        assert notify.briefing_allowed(rid, t, db_path) is True
        assert push.priority_of(t) > push.P1_ACT_NOW, "not time-sensitive"
        assert push.module_of(t) == "labor"
    import client_api
    assert "clocked in" not in client_api._NOTIFICATION_LABELS["shift_request"]


def test_the_labor_reminder_is_its_own_type():
    import inspect, strategy_jobs
    src = inspect.getsource(strategy_jobs.run_labor_reminders)
    assert '"labor_reminder"' in src and '"coverage"' not in src


def test_every_new_type_is_mapped_everywhere():
    """A new alert type needs its label, module, priority, briefing set and
    iOS route — the mapping A-5/A-6/A-18/A-22 found missing."""
    import client_api
    ios = open(os.path.join(ROOT, "ios/CavnarAI/CavnarAI/Push/DeepLinkRouter.swift"), encoding="utf-8").read()
    for t in ("shift_request", "labor_reminder", "schedule_publish_held", "schedule_publish_pending",
              "order_send_held", "milestone"):
        assert t in client_api._NOTIFICATION_LABELS, t
        assert t in push.NOTIFICATION_MODULE, t
        assert t in push.PRIORITY, t
        assert t in models.NON_ALERT_TYPES, t
        assert f'"{t}"' in ios, t
    for t in ("schedule_publish_held", "order_send_held", "shift_request", "labor_reminder"):
        assert t in notify.BRIEFING_ALWAYS, t


# ── A-7 / A-14: the payload names its module and its location ──────────────

def test_every_push_carries_its_module_and_location(db_path, monkeypatch):
    rid = _rid(db_path)
    got = []

    class _Now:
        def submit(self, fn, token_row, alert_type, title, body, data, db):
            got.append((alert_type, data))
    monkeypatch.setattr(push, "_push_executor", lambda: _Now())
    monkeypatch.setattr(push, "get_device_tokens", lambda *a, **k: [{"user_id": 1, "restaurant_id": 99}])
    push.fire_push(rid, "critical_low", "t", "b", data={"x": 1}, db_path=db_path)
    assert got == [("critical_low", {"x": 1, "restaurant_id": rid, "module": "inventory"})]


def test_the_sound_follows_the_location_the_alert_is_about(db_path, monkeypatch):
    """A group owner's phone registered at B hears A's alert; A has sound
    off, so the push is silent."""
    a = _rid(db_path, name="A")
    b = _rid(db_path, name="B")
    update_restaurant(a, {"push_sound": 0}, db_path=db_path)
    uid = auth.create_user(b, "own", "own@x.test", "correct-horse", db_path=db_path)
    push.register_device_token(uid, b, "t" * 64, db_path=db_path)
    row = push.get_device_tokens(b, db_path=db_path)[0]
    calls = []

    class _Resp:
        status_code = 200

        def json(self):
            return {}

    class _Client:
        def post(self, url, content=None, headers=None):
            calls.append(json.loads(content))
            return _Resp()
    monkeypatch.setattr(push, "_client", lambda: _Client())
    monkeypatch.setattr(push, "_provider_jwt", lambda: "jwt")
    push._deliver(row, "1star", "t", "b", {"restaurant_id": a, "module": "reviews"}, db_path=db_path)
    assert "sound" not in calls[0]["aps"]
    assert calls[0]["cavnar"]["restaurant_id"] == a


# ── A-8: loss issues only for LOSS_VIEW ───────────────────────────────────

def test_a_loss_issue_is_hidden_from_a_manager_everywhere(db_path, monkeypatch):
    import issues, action_queue, ask_cavnar_tools as tools
    rid = _rid(db_path)
    issues.create_issue(rid, "loss", "Sam (POS id 12) approved 64% of comps", notify=False, db_path=db_path)
    issues.create_issue(rid, "manual", "Fix the walk-in door", notify=False, db_path=db_path)
    manager = _user(db_path, rid, "manager", "m@x.test")
    owner = _user(db_path, rid, "owner", "own@x.test")
    assert issues.viewer_sees_loss(owner) is True and issues.viewer_sees_loss(manager) is False
    titles = lambda rows: [i["title"] for i in rows]
    assert len(issues.list_issues(rid, db_path=db_path)) == 2
    assert titles(issues.list_issues(rid, db_path=db_path, sees_loss=False)) == ["Fix the walk-in door"]
    assert issues.summary(rid, db_path=db_path, sees_loss=False)["open"] == 1
    # Home's action queue
    r = models.get_restaurant(rid, db_path)
    queued = [i["title"] for i in action_queue.items(rid, viewer=manager, db_path=db_path, restaurant=r)["items"]
              if i["kind"] == "issue"]
    assert queued == ["Fix the walk-in door"]
    # Ask
    view = tools.viewer_restaurant(r, manager)
    out = tools._read_open_issues(rid, _viewer=view)
    assert [i["title"] for i in out["issues"]] == ["Fix the walk-in door"]
    owner_view = tools.viewer_restaurant(r, owner)
    assert len(tools._read_open_issues(rid, _viewer=owner_view)["issues"]) == 2
    # The issue list route (web and mobile share the body)
    import flask, strategy_routes
    monkeypatch.setattr(strategy_routes, "_rid", lambda u: rid)
    with flask.Flask(__name__).test_request_context("/issues"):
        payload, status = strategy_routes._do_issues_list(manager)
    assert status == 200 and titles(payload["issues"]) == ["Fix the walk-in door"]


# ── A-10 / A-15: results at the restaurant's own hour, to the right people ─

def test_a_win_is_told_at_nine_local_not_at_chicagos_six(db_path, monkeypatch):
    import strategy_jobs, scheduler, outcomes
    rid = _rid(db_path, timezone="America/Los_Angeles")
    told = []
    monkeypatch.setattr(strategy_jobs, "_tell_owners_what_worked", lambda rows, db: told.append(rows) or len(rows))
    monkeypatch.setattr(outcomes, "list_outcomes", lambda *a, **k: [
        {"id": 7, "verdict": "improved", "evaluate_on": date.today().isoformat(), "dollars_monthly": 900.0,
         "metric": "labor_pct", "title": "Cut Tuesday prep"}])
    at = {"h": 4}
    monkeypatch.setattr(scheduler, "local_due",
                        lambda r, hour, until=14, claim_key=None, **k: hour <= at["h"] < until)
    strategy_jobs.run_outcome_wins(db_path=db_path)
    assert told == [], "4am in Los Angeles"
    at["h"] = 9
    strategy_jobs.run_outcome_wins(db_path=db_path)
    assert len(told) == 1 and told[0][0]["id"] == 7
    strategy_jobs.run_outcome_wins(db_path=db_path)
    assert told[-1] == [], "each result is told once"


def test_the_evaluation_pass_no_longer_pushes(db_path, monkeypatch):
    import strategy_jobs, outcomes
    monkeypatch.setattr(outcomes, "evaluate_due", lambda **k: [
        {"restaurant_id": 1, "verdict": "improved", "dollars_monthly": 900.0}])
    monkeypatch.setattr(strategy_jobs, "_reach", lambda *a, **k: pytest.fail("pushed at operator time"))
    strategy_jobs.run_outcome_evaluations(db_path=db_path)


def test_milestones_wait_for_the_restaurants_own_morning(db_path, monkeypatch):
    import strategy_jobs, scheduler, milestones
    _rid(db_path, timezone="Pacific/Honolulu")
    checked = []
    monkeypatch.setattr(milestones, "check_all", lambda rid_, **k: checked.append(rid_) or [])
    monkeypatch.setattr(scheduler, "local_due", lambda *a, **k: False)
    strategy_jobs.run_milestones(db_path=db_path)
    assert checked == []


def test_a_food_cost_win_reaches_only_people_who_can_open_food_cost(db_path, monkeypatch):
    import strategy_jobs, morning_brief
    rid = _rid(db_path)
    owner = _user(db_path, rid, "owner", "own@x.test")
    manager = _user(db_path, rid, "manager", "m@x.test")
    monkeypatch.setattr(morning_brief, "recipients", lambda *a, **k: [dict(owner, grants=[]), dict(manager, grants=[])])
    monkeypatch.setattr(push, "get_device_tokens", lambda *a, **k: [{"user_id": owner["id"]}, {"user_id": manager["id"]}])
    pushed = []
    monkeypatch.setattr(push, "fire_push", lambda rid_, t, title, body, data=None, db_path=None, user_ids=None:
                        pushed.append(set(user_ids)))
    strategy_jobs._tell_owners_what_worked([
        {"restaurant_id": rid, "verdict": "improved", "dollars_monthly": 1200.0, "metric": "food_cost_pct",
         "title": "Reprice the burrata", "metric_label": "Food cost %"}], db_path)
    assert pushed == [{owner["id"]}]


def test_a_push_inside_quiet_hours_arrives_silently(db_path, monkeypatch):
    import strategy_jobs, morning_brief
    rid = _rid(db_path)
    owner = _user(db_path, rid, "owner", "own@x.test")
    monkeypatch.setattr(morning_brief, "recipients", lambda *a, **k: [dict(owner, grants=[])])
    monkeypatch.setattr(push, "get_device_tokens", lambda *a, **k: [{"user_id": owner["id"]}])
    monkeypatch.setattr(models, "is_in_quiet_hours", lambda *a, **k: True)
    pushed = []
    monkeypatch.setattr(push, "fire_push", lambda *a, data=None, **k: pushed.append(data))
    strategy_jobs._reach(rid, "milestone", "t", "b", {}, db_path)
    assert pushed and pushed[0].get("quiet") is True


# ── A-16: the morning batch keeps each alert's webhooks ────────────────────

def test_a_combined_morning_still_fires_each_alerts_webhooks(db_path, monkeypatch):
    rid = _rid(db_path)
    fired = []
    monkeypatch.setattr(webhooks, "fire_webhook", lambda rid_, event, payload, db=None: fired.append(
        (event, payload.get("alert_type"))))
    monkeypatch.setattr(notify, "send_sms", lambda *a, **k: True)
    monkeypatch.setattr(notify, "_send_alert_email", lambda *a, **k: True)
    monkeypatch.setattr(push, "fire_push", lambda *a, **k: None)
    notify.deliver_alert(rid, "daily_briefing", "sms", "subject", "<p>x</p>", db_path=db_path,
                         covered_types=["labor_over", "food_waste"])
    assert ("labor.over_target", "labor_over") in fired
    assert ("alert.fired", "labor_over") in fired and ("alert.fired", "food_waste") in fired
    assert ("alert.fired", "daily_briefing") in fired


# ── A-18: the auto-publish notice names the real time ──────────────────────

def test_the_publish_notice_states_when_it_really_goes_out(db_path, monkeypatch):
    """The job may run until 2pm; a 1:30pm pass sends at 3:30pm, not "11am"."""
    import scheduler, strategy_jobs, schedule_intel, time_utils
    monkeypatch.setattr(schedule_intel, "watched_dates", lambda *a, **k: {"watched"})
    rid = _rid(db_path, module_labor=1)
    update_restaurant(rid, {"auto_publish_schedule": 1}, db_path=db_path)
    conn = get_conn(db_path)
    for w in ("2026-09-07", "2026-09-14", "2026-09-21"):
        conn.execute("INSERT INTO schedule_history (restaurant_id, week_start, week_end, schedule_csv, published_at) "
                     "VALUES (?,?,?,?, datetime('now'))", (rid, w, w, "employee,role\nA,server"))
    conn.execute("INSERT INTO schedule_history (restaurant_id, week_start, week_end, schedule_csv) VALUES (?,?,?,?)",
                 (rid, "2099-01-04", "2099-01-10", "employee,role\nA,server"))
    conn.commit()
    conn.close()
    late = datetime(2026, 9, 25, 13, 30)                    # a Friday, late in the window
    monkeypatch.setattr(time_utils, "restaurant_now", lambda *a, **k: late)
    monkeypatch.setattr(scheduler, "local_due", lambda *a, **k: True)
    monkeypatch.setattr("client_api.publish_blockers", lambda *a, **k: [])
    monkeypatch.setattr(models, "get_all_restaurants", lambda *a, **k: [models.get_restaurant(rid, db_path=db_path)])
    told = []
    monkeypatch.setattr(strategy_jobs, "_reach", lambda rid_, t, title, *a, **k: told.append((t, title)) or 1)
    scheduler.run_auto_publish_schedules()
    assert told == [("schedule_publish_pending", "Next week's schedule goes to staff at 3:30pm")]


# ── A-21: Ask counts only what asks for action, for this viewer ─────────────

def test_ask_does_not_count_briefs_and_sign_ins_as_needing_action(db_path):
    import ask_cavnar, ask_cavnar_tools as tools
    rid = _rid(db_path)
    for t in ("morning_brief", "login", "outcome_achieved", "closing_summary"):
        _log(db_path, rid, t)
    assert tools._read_alerts(rid)["outstanding"] == 0
    assert "Still needing action" not in ask_cavnar._alerts_context(rid)
    _log(db_path, rid, "labor_over")
    out = tools._read_alerts(rid)
    assert out["outstanding"] == 1
    assert "fired_at" not in out["alerts"][0] and "/" in out["alerts"][0]["fired_on"]


def test_ask_leaves_out_alerts_the_viewer_cannot_open(db_path):
    import ask_cavnar, ask_cavnar_tools as tools
    rid = _rid(db_path, module_inventory=1, module_labor=1)
    _log(db_path, rid, "critical_low")
    _log(db_path, rid, "labor_over")
    r = models.get_restaurant(rid, db_path)
    manager = _user(db_path, rid, "manager", "m@x.test")
    view = tools.viewer_restaurant(r, manager)
    if "inventory" not in tools._denied(view):
        pytest.skip("managers can open Food Cost in this build")
    types = [a["type"] for a in tools._read_alerts(rid, _viewer=view)["alerts"]]
    assert "critical_low" not in types and "labor_over" in types
    ctx = ask_cavnar._alerts_context(rid, viewer=view)
    assert "Running out" not in ctx


# ── A-22: a held order is its own notice ──────────────────────────────────

def test_a_held_order_is_not_overwritten_by_the_queued_ones():
    import inspect, strategy_jobs
    src = inspect.getsource(strategy_jobs.run_trusted_orders)
    assert '"order_send_held", "A supplier order was held"' in src
    assert '"collapse_key": f"order-{row[\'id\']}"' in src
    assert push._collapse_id(1, "order_send_pending", {"collapse_key": "order-5"}) != \
        push._collapse_id(1, "order_send_pending", {"collapse_key": "order-6"})


# ── A-23: the while-away nudge counts honestly ────────────────────────────

def test_while_away_counts_written_reviews_owed_replies_and_real_alerts(db_path, monkeypatch):
    import scheduler, strategy_jobs
    rid = _rid(db_path)
    conn = get_conn(db_path)
    conn.execute("INSERT INTO users (username, email, password_hash, restaurant_id, role, last_login) "
                 "VALUES ('o','o@x.test','x',?, 'owner', datetime('now','-20 days'))", (rid,))
    # One review written while away; two old ones imported while away.
    conn.execute("INSERT INTO reviews (restaurant_id, platform, external_id, author, rating, text, review_date, "
                 "fetched_at, response_status) VALUES (?, 'google', 'new', 'A', 5, 'x', date('now','-3 days'), "
                 "datetime('now','-3 days'), 'pending')", (rid,))
    for ext in ("old1", "old2"):
        conn.execute("INSERT INTO reviews (restaurant_id, platform, external_id, author, rating, text, review_date, "
                     "fetched_at, response_status) VALUES (?, 'google', ?, 'B', 4, 'x', date('now','-300 days'), "
                     "datetime('now','-2 days'), 'pending')", (rid, ext))
    conn.commit()
    conn.close()
    _log(db_path, rid, "morning_brief", 10)
    _log(db_path, rid, "login", 3)
    _log(db_path, rid, "labor_over")
    lines = []
    monkeypatch.setattr(strategy_jobs, "_reach", lambda rid_, t, title, body, data, db, **k:
                        lines.extend(k.get("lines") or []) or 1)
    monkeypatch.setattr(models, "get_all_restaurants", lambda *a, **k: [models.get_restaurant(rid, db_path)])
    scheduler.send_while_away_nudges()
    text = " ".join(lines)
    assert "1 new review came in" in text
    assert "1 is waiting on your approval" in text
    assert "1 alert fired" in text


# ── A-24: web waits for the job, as iOS does ──────────────────────────────

def test_the_web_confirm_waits_for_a_background_job():
    import ask_cavnar_tools as tools
    html = open(os.path.join(ROOT, "templates/dashboard.html"), encoding="utf-8").read()
    body = html[html.index("function _runAskCavnarProposal"):html.index("window.sendAskCavnar = function")]
    assert "d.job_id" in body and "_pollAskCavnarJob" in body
    for name in ("generate_schedule", "refresh_competitors"):
        route = tools._BY_NAME[name]["route"]
        assert route["status"]["web"] and route["status"]["mobile"]


# ── A-27 / A-28: the issue tick ───────────────────────────────────────────

def _routed(db_path, rid):
    import issues
    conn = get_conn(db_path)
    cid = conn.execute("INSERT INTO alert_contacts (restaurant_id, name, phone, sms_consent) VALUES (?,?,?,1)",
                       (rid, "Mgr", "+15555550111")).lastrowid
    conn.commit()
    conn.close()
    issues.set_routing(rid, "manager", cid, db_path=db_path)
    return cid


def test_a_paused_restaurant_is_not_texted_held_issues(db_path, monkeypatch):
    import issues
    rid = _rid(db_path)
    _routed(db_path, rid)
    texted = []
    monkeypatch.setattr(issues, "_notify", lambda *a, **k: False)          # held by quiet hours
    issues.create_issue(rid, "manual", "Held one", db_path=db_path)
    monkeypatch.setattr(issues, "_notify", lambda *a, **k: texted.append(a) or True)
    update_restaurant(rid, {"billing_status": "paused"}, db_path=db_path)
    monkeypatch.setattr(issues, "_sendable", lambda *a, **k: {"assignee_contact_id": 1})
    issues.tick(db_path=db_path)
    assert texted == []


def test_a_held_no_show_text_is_dropped_once_the_shift_is_over(db_path, monkeypatch):
    import issues
    rid = _rid(db_path)
    _routed(db_path, rid)
    texted = []
    monkeypatch.setattr(issues, "_notify", lambda *a, **k: False)          # held by quiet hours
    yesterday = (date.today() - timedelta(days=2)).isoformat()
    issue, _ = issues.create_issue(rid, "coverage", "Bob hasn't clocked in",
                                   source_key=f"coverage:{yesterday}:bob", db_path=db_path)
    monkeypatch.setattr(issues, "_notify", lambda *a, **k: texted.append(a) or True)
    monkeypatch.setattr(issues, "_sendable", lambda *a, **k: {"assignee_contact_id": 1})
    issues.tick(db_path=db_path)
    assert texted == []
    conn = get_conn(db_path)
    assert conn.execute("SELECT notify_suppressed FROM ops_issues WHERE id=?", (issue["id"],)).fetchone()[0] == 1
    conn.close()

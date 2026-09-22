"""Edge cases of time off, availability and the replacement pickers (SCHED audit).

Approving time off over a week already sent to staff, who may approve it,
the manager's replacement picker and the call-off suggestions offering
people on approved time off, deactivated, or in another role, and a
full-timer off for the whole week.

xfail(strict=True) marks a confirmed defect, asserting the correct
behaviour; the marker comes off with the fix.
"""
import datetime as dt
import json
import sys

import pytest

# Imported here, before any fixture patches models.get_conn, so no module is
# first imported mid-test and left holding a redirect to a deleted database.
import activity, covers, decisions, delayed, demand_signals, goals, issues, metrics, outcomes  # noqa: E401,F401
import push, schedule_economics, schedule_intel, schedule_rules, schedule_versions  # noqa: E401,F401
import shift_requests, shift_quality, staff_schedule, staff_settings, strategy_jobs, time_off  # noqa: E401,F401
import labor_replacements  # noqa: F401
from flask import Flask

import auth
import client_api
import labor
import mobile_api
import models
import permissions
import schedule_engine as se
import schedule_rules as sr
import staff_settings as ss
from models import create_restaurant, Restaurant

HEADER = "date,day,employee,role,shift_start,shift_end,scheduled_hours,notes"
W1 = ["2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09", "2026-10-10", "2026-10-11"]
DAYS = list(sr.DAYS)


@pytest.fixture
def db(db_path, monkeypatch):
    real = models.get_conn

    def conn(*a, **k):
        return real(db_path)
    for mod in list(sys.modules.values()):
        bound = getattr(mod, "get_conn", None) if mod is not None else None
        # The real one, or a redirect some earlier test left bound in a
        # module it imported for the first time mid-test.
        if bound is real or str(getattr(bound, "__module__", "")).startswith(("test_", "tests.", "conftest")):
            monkeypatch.setattr(mod, "get_conn", conn)
    monkeypatch.setattr(models, "get_conn", conn)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(models, "_cached_shifts", lambda r: [])
    monkeypatch.setattr(client_api, "log_account_event", lambda *a, **k: None)
    return db_path


def _restaurant(db_path, roster, **cols):
    rid = create_restaurant(Restaurant(name="Time Off Co", owner_email="t@x.com"), db_path=db_path)
    cols.setdefault("module_labor", 1)
    conn = models.get_conn(db_path)
    conn.execute("UPDATE restaurants SET " + ", ".join(f"{k}=?" for k in cols) + " WHERE id=?", (*cols.values(), rid))
    conn.commit()
    conn.close()
    for name, role in roster:
        models.add_manual_team_member(rid, name, role=role, db_path=db_path)
    return rid


def _day(d):
    return dt.date.fromisoformat(d).strftime("%A")


def _publish(db_path, rid, rows):
    text = HEADER + "\n" + "\n".join(f"{d},{_day(d)},{n},{role},{s},{e},{h}," for d, n, role, s, e, h in rows)
    hid = models.save_schedule_history(rid, W1[0], W1[-1], 0, 0, 30, text, [], db_path=db_path)
    conn = models.get_conn(db_path)
    conn.execute("UPDATE schedule_history SET published_at=datetime('now') WHERE id=?", (hid,))
    conn.commit()
    conn.close()
    return hid


def _time_off(db_path, rid, name, start, end=None, status="approved"):
    conn = models.get_conn(db_path)
    cur = conn.execute("INSERT INTO staff_time_off (restaurant_id, employee_name, start_date, end_date, status) "
                       "VALUES (?,?,?,?,?)", (rid, name, start, end or start, status))
    conn.commit()
    conn.close()
    return cur.lastrowid


# ── SCHED-22: approving time off over a published week ────────────────────

@pytest.fixture
def strategy_client(db):
    from strategy_routes import strategy_bp
    app = Flask(__name__, template_folder="../templates")
    app.register_blueprint(strategy_bp)
    return app.test_client()


def _as(monkeypatch, rid, role):
    monkeypatch.setattr(auth, "get_current_user",
                        lambda: {"id": 5, "restaurant_id": rid, "is_admin": 0, "role": role,
                                 "username": "u", "email": "u@x.com"})


def test_approving_time_off_over_a_published_shift_names_the_conflicting_shift(db, strategy_client, monkeypatch):
    rid = _restaurant(db, [("Ana", "Server"), ("Ben", "Server")])
    _publish(db, rid, [(W1[2], "Ana", "Server", "11:00am", "3:00pm", 4)])
    req = _time_off(db, rid, "Ana", W1[2], status="pending")
    _as(monkeypatch, rid, "client")
    r = strategy_client.post(f"/api/labor/time-off/{req}/decide", json={"decision": "approve"})
    assert r.status_code == 200
    assert "11:00am" in json.dumps(r.get_json()), r.get_json()


def test_a_view_only_labor_login_cannot_approve_time_off(db, strategy_client, monkeypatch):
    rid = _restaurant(db, [("Ana", "Server")])
    monkeypatch.setitem(permissions.ROLE_PERMISSIONS, "labor_viewer",
                        frozenset({permissions.DASHBOARD_ACCESS, permissions.LABOR_VIEW}))
    req = _time_off(db, rid, "Ana", W1[2], status="pending")
    _as(monkeypatch, rid, "labor_viewer")
    assert strategy_client.post(f"/api/labor/shift-requests/999/decide", json={"decision": "approve"}).status_code == 403
    r = strategy_client.post(f"/api/labor/time-off/{req}/decide", json={"decision": "approve"})
    assert r.status_code == 403
    assert time_off.pending(rid)[0]["status"] == "pending"


def test_a_time_off_request_is_decided_once(db, strategy_client, monkeypatch):
    rid = _restaurant(db, [("Ana", "Server")])
    req = _time_off(db, rid, "Ana", W1[2], status="pending")
    _as(monkeypatch, rid, "client")
    assert strategy_client.post(f"/api/labor/time-off/{req}/decide", json={"decision": "deny"}).status_code == 200
    assert strategy_client.post(f"/api/labor/time-off/{req}/decide", json={"decision": "approve"}).status_code == 404


# ── SCHED-14: the manager's replacement picker ────────────────────────────

@pytest.fixture
def picker(db, monkeypatch):
    auth.init_auth(db_path=db)
    app = Flask(__name__, template_folder="../templates")
    app.register_blueprint(mobile_api.mobile_bp)
    return app.test_client()


def _bearer(db, rid):
    uid = auth.create_user(rid, "owner", "owner@x.com", "pw", db_path=db)
    return {"Authorization": f"Bearer {auth.create_session(uid, db_path=db)}"}


def _rows(*rows):
    return [{"date": d, "day": _day(d), "employee": n, "role": "Server", "shift_start": s, "shift_end": e,
             "scheduled_hours": str(h), "notes": ""} for d, n, s, e, h in rows]


def test_the_replacement_picker_never_offers_someone_on_approved_time_off_or_deactivated(db, picker):
    rid = _restaurant(db, [("Ana", "Server"), ("Ben", "Server"), ("Cy", "Server")])
    _time_off(db, rid, "Ben", W1[0])
    ss.upsert(rid, "Cy", active=False)
    rows = _rows((W1[0], "Ana", "11:00am", "3:00pm", 4), (W1[1], "Ben", "11:00am", "3:00pm", 4),
                 (W1[2], "Cy", "11:00am", "3:00pm", 4))
    r = picker.post("/mobile/api/labor/schedule/replacements", headers=_bearer(db, rid), json={"rows": rows, "index": 0})
    assert r.status_code == 200
    offered = {m["name"] for m in r.get_json()["replacements"]}
    assert "Ben" not in offered and "Cy" not in offered, offered


def test_the_replacement_picker_offers_a_free_roster_member_with_no_shift_this_week(db, picker):
    rid = _restaurant(db, [("Ana", "Server"), ("Ben", "Server"), ("Dee", "Server")])
    rows = _rows((W1[0], "Ana", "11:00am", "3:00pm", 4), (W1[1], "Ben", "11:00am", "3:00pm", 4))
    r = picker.post("/mobile/api/labor/schedule/replacements", headers=_bearer(db, rid), json={"rows": rows, "index": 0})
    assert "Dee" in {m["name"] for m in r.get_json()["replacements"]}


def test_the_replacement_picker_refuses_somebody_already_working_that_start(db, picker):
    rid = _restaurant(db, [("Ana", "Server"), ("Ben", "Server")])
    rows = _rows((W1[0], "Ana", "11:00am", "3:00pm", 4), (W1[0], "Ben", "11:00am", "3:00pm", 4))
    r = picker.post("/mobile/api/labor/schedule/replacements", headers=_bearer(db, rid), json={"rows": rows, "index": 0})
    assert r.status_code == 200 and "Ben" not in {m["name"] for m in r.get_json()["replacements"]}


def test_the_shared_replacement_check_already_refuses_time_off(db):
    """The rule the picker should be using exists and is right."""
    rid = _restaurant(db, [("Ana", "Server"), ("Ben", "Server")])
    _time_off(db, rid, "Ben", W1[0])
    rows = _rows((W1[0], "Ana", "11:00am", "3:00pm", 4), (W1[1], "Ben", "11:00am", "3:00pm", 4))
    ok, why = se.replacement_is_legal(rid, rows, 0, "Ben")
    assert not ok and why == sr.LABELS["approved_time_off"]


# ── SCHED-15: call-off coverage suggestions ───────────────────────────────

def _contacts(db_path, rid, *names):
    conn = models.get_conn(db_path)
    for n in names:
        conn.execute("INSERT OR REPLACE INTO staff_contacts (restaurant_id, employee_name, email) VALUES (?,?,?)",
                     (rid, n, f"{n.lower()}@x.com"))
    conn.commit()
    conn.close()


def test_call_off_suggestions_for_a_bartender_gap_are_bartenders(db):
    rid = _restaurant(db, [("Ana", "Bartender"), ("Bea", "Bartender"), ("Dan", "Dishwasher")])
    _contacts(db, rid, "Ana", "Bea", "Dan")
    fits = labor_replacements.for_gap(rid, "Bartender", "Friday", exclude=["Ana"], limit=5)
    assert [f["name"] for f in fits] == ["Bea"]


def test_call_off_suggestions_never_name_someone_on_approved_time_off_today(db):
    today = dt.date.today()
    rid = _restaurant(db, [("Ana", "Bartender"), ("Bea", "Bartender"), ("Cal", "Bartender")])
    _contacts(db, rid, "Ana", "Bea", "Cal")
    _time_off(db, rid, "Bea", today.isoformat())
    fits = labor_replacements.for_gap(rid, "Bartender", today.strftime("%A"), exclude=["Ana"], limit=5)
    assert "Bea" not in {f["name"] for f in fits}


def test_call_off_suggestions_skip_deactivated_staff_and_the_missing_person(db):
    rid = _restaurant(db, [("Ana", "Bartender"), ("Bea", "Bartender"), ("Cal", "Bartender")])
    _contacts(db, rid, "Ana", "Bea", "Cal")
    ss.upsert(rid, "Cal", active=False)
    fits = labor_replacements.for_gap(rid, "Bartender", "Friday", exclude=["Ana"], limit=5)
    assert [f["name"] for f in fits] == ["Bea"]


# ── a full-timer on time off for the whole week (no finding) ─────────────

def test_a_full_timer_off_all_week_is_reported_unscheduled_not_flagged_for_days_off(db, monkeypatch):
    rid = _restaurant(db, [("Ana", "Server"), ("Ben", "Server")])
    ss.upsert(rid, "Ana", employment_type="full")
    _time_off(db, rid, "Ana", W1[0], W1[-1])
    text = HEADER + "\n" + "\n".join(f"{d},{_day(d)},Ben,Server,11:00am,3:00pm,4," for d in W1[:5])
    base = {"ok": True, "schedule_csv": text, "week_dates": W1, "week_days": DAYS, "summary": [], "hours_budget": 0,
            "daily_target_hours": {}, "labor_target": 30, "blended_rate": 20.0, "roster": ["Ana", "Ben"]}
    monkeypatch.setattr(se, "_build_schedule_result", lambda r, week_start=None: dict(base))
    done = {}
    monkeypatch.setattr(se._ops, "finish_async_job", lambda j, status, result: done.update(status=status, result=result))
    se._run_schedule_job("edge-off", rid)
    res = done["result"]
    assert done["status"] == "done" and res["not_scheduled"] == ["Ana"]
    assert any("1 of them full-time" in line for line in res["review"]["lines"])
    assert not [v for v in res["rule_violations"] if v["employee"] == "Ana"]
    c = sr.build_constraints(rid, W1, DAYS)
    assert all(not c.can_work("Ana", d)[0] for d in W1)

"""Schedule fix round 10/10/26, workstream D2.

MARKET-15  an employee's cut in availability waits for a manager and takes
           effect on a date; the draft uses the availability in force for
           each date; more availability is written at once.
BUGSGEN-23 two different people with one name are never folded into one.
"""
import json
from datetime import date

import pytest
from flask import Flask

import models
import schedule_rules as sr
import staff_settings as ss
import time_off
import availability_changes as avc
import request_autonomy
import action_queue
import people
from models import Restaurant, create_restaurant

TODAY = date(2026, 10, 2)                      # a Friday
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
HEAD = "date,day,employee,role,shift_start,shift_end,scheduled_hours,notes\n"
WEEK_OCT5 = ["2026-10-0%d" % d for d in range(5, 10)] + ["2026-10-10", "2026-10-11"]
WEEK_OCT12 = ["2026-10-%d" % d for d in range(12, 19)]


@pytest.fixture
def env(db_path, monkeypatch):
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)   # noqa: E731
    for mod in (models, ss, time_off, avc, request_autonomy, action_queue):
        monkeypatch.setattr(mod, "get_conn", redirect, raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    told = []
    import strategy_jobs
    monkeypatch.setattr(strategy_jobs, "_reach", lambda *a, **k: told.append((a, k)) or 1)
    import people as _pp
    monkeypatch.setattr(_pp, "tell", lambda *a, **k: told.append(("tell", a)) or "push")
    import time_utils
    monkeypatch.setattr(time_utils, "restaurant_now_by_id", lambda *a, **k: __import__("datetime").datetime(2026, 10, 2, 9))
    rid = create_restaurant(Restaurant(name="Avail Co", owner_email="o@a.test", module_labor=1), db_path=db_path)
    return {"rid": rid, "db": db_path, "told": told}


def _week(**days):
    return [dict({"day": d}, **days.get(d, {"status": "any"})) for d in DAYS]


def _save(env, name="Ben", **kw):
    kw.setdefault("today", TODAY)
    exp = ss.own_availability(env["rid"], name, db_path=env["db"], today=TODAY)["updated_at"]
    return ss.save_own_availability(env["rid"], name, exp, db_path=env["db"], **kw)


def _publish(env, rows, week_start="2026-10-05", week_end="2026-10-11"):
    conn = models.get_conn(env["db"])
    try:
        models._ensure_history_columns(conn)
        conn.execute("INSERT INTO schedule_history (restaurant_id, generated_at, week_start, week_end, schedule_csv, "
                     "published_at) VALUES (?, datetime('now'), ?, ?, ?, datetime('now'))",
                     (env["rid"], week_start, week_end, HEAD + "\n".join(rows)))
        conn.commit()
    finally:
        conn.close()


# ── MARKET-15 ───────────────────────────────────────────────────────────────

def test_a_cut_waits_for_a_manager_with_a_date_and_an_addition_is_written_at_once(env):
    models.save_staff_availability(env["rid"], "Ben", DAYS[:6], ["Sunday"], db_path=env["db"])   # Sundays off now
    _publish(env, ["2026-10-10,Saturday,Ben,Server,5:00pm,11:00pm,6,"])
    res = _save(env, week=_week(Saturday={"status": "off"}))            # Sundays back, Saturdays off
    assert res["ok"]
    assert res["availability"]["unavailable_days"] == []                 # the Sunday is back at once
    ch = res["pending_change"]
    assert ch["status"] == "pending" and set(ch["days"]) == {"Saturday"}
    assert ch["effective_from"] == "2026-10-12"                           # the first week nobody built
    assert res["message"] and res["conflicts"] == []                      # the published Saturday stands
    assert any(a[2] == "Availability change to approve" for a, k in env["told"] if isinstance(a, tuple) and len(a) > 2)
    # The draft reads Saturday as available until a manager says otherwise.
    c = sr.build_constraints(env["rid"], WEEK_OCT12, DAYS, db_path=env["db"])
    assert c.can_work("Ben", "2026-10-17")[0]
    # Waiting on you carries it, with Approve and Deny.
    q = {i["key"]: i for i in action_queue.items(env["rid"], viewer={"is_admin": True}, db_path=env["db"],
                                                 today=TODAY)["items"]}
    item = q[f"availability_change:{ch['id']}"]
    assert item["action"]["route"]["mobile"].endswith(f"/availability-changes/{ch['id']}/decide")
    assert item["action"]["alt"]["body"] == {"decision": "deny"}


def test_an_approved_cut_holds_from_its_date_for_the_draft_and_goes_in_force_on_it(env):
    res = _save(env, week=_week(Saturday={"status": "off"}, Tuesday={"status": "window", "earliest": "17:00"}))
    ch = res["pending_change"]
    out = avc.decide(env["rid"], ch["id"], True, decided_by="mgr", effective_from="2026-10-12", db_path=env["db"],
                     today=TODAY)
    assert out["status"] == "approved"
    # A week before the date keeps them; the week from it does not.
    before = sr.build_constraints(env["rid"], WEEK_OCT5, DAYS, db_path=env["db"])
    assert before.can_work("Ben", "2026-10-10")[0] and before.window_ok("Ben", "2026-10-06", "11:00am", "3:00pm")[0]
    after = sr.build_constraints(env["rid"], WEEK_OCT12, DAYS, db_path=env["db"])
    assert not after.can_work("Ben", "2026-10-17")[0]
    assert not after.window_ok("Ben", "2026-10-13", "11:00am", "3:00pm")[0]
    assert after.window_ok("Ben", "2026-10-13", "5:00pm", "10:00pm")[0]
    # On the date it is just their availability.
    assert avc.apply_due(env["rid"], today=date(2026, 10, 12), db_path=env["db"]) == 1
    row = models.staff_availability_for(env["rid"], "Ben", db_path=env["db"])
    assert "Saturday" in json.loads(row["unavailable_days"])
    assert ss.for_name(env["rid"], "Ben", db_path=env["db"])["time_windows"]["Tuesday"]["earliest"] == "5:00pm"
    assert avc.mine(env["rid"], "Ben", db_path=env["db"]) == []


def test_a_denied_cut_changes_nothing_and_the_person_is_told(env):
    res = _save(env, week=_week(Friday={"status": "off"}))
    out = avc.decide(env["rid"], res["pending_change"]["id"], False, decided_by="mgr", db_path=env["db"], today=TODAY)
    assert out["status"] == "denied"
    assert any(t[0] == "tell" and "wasn't approved" in t[1][2] for t in env["told"])
    c = sr.build_constraints(env["rid"], WEEK_OCT12, DAYS, db_path=env["db"])
    assert c.can_work("Ben", "2026-10-16")[0]


def test_a_manager_who_moves_the_date_into_a_built_week_is_told_the_shifts_it_rules_out(env):
    _publish(env, ["2026-10-09,Friday,Ben,Server,5:00pm,11:00pm,6,"])
    res = _save(env, week=_week(Friday={"status": "off"}))
    out = avc.decide(env["rid"], res["pending_change"]["id"], True, decided_by="mgr", effective_from="2026-10-05",
                     db_path=env["db"], today=TODAY)
    assert [(c["date"], c["shift_start"]) for c in out["conflicts"]] == [("2026-10-09", "5:00pm")]


def test_the_date_can_never_be_in_the_past(env):
    res = _save(env, week=_week(Friday={"status": "off"}))
    with pytest.raises(avc.ChangeError):
        avc.decide(env["rid"], res["pending_change"]["id"], True, effective_from="2026-09-01", db_path=env["db"],
                   today=TODAY)


def test_widening_a_window_is_an_addition_and_narrowing_one_is_a_cut():
    w = lambda lo, hi: ("window", (lo, hi, None, None))   # noqa: E731
    assert avc.is_cut(("any", None), w(600, None))
    assert avc.is_cut(w(600, 1380), w(900, 1380))
    assert not avc.is_cut(w(900, 1380), w(600, 1380))
    assert not avc.is_cut(("off", None), w(600, None))
    assert not avc.is_cut(("off", None), ("any", None))
    assert avc.is_cut(("off", {"from": None, "until": "2026-12-01"}), ("off", None))


def test_the_staff_route_says_what_waits(env, monkeypatch):
    import staff_routes
    monkeypatch.setattr(ss, "_today", lambda rid: TODAY)
    app = Flask(__name__)
    app.register_blueprint(staff_routes.staff_bp, url_prefix="/staff")
    user = {"id": 9, "restaurant_id": env["rid"], "employee_name": "Ben", "role": "employee"}
    with app.test_request_context("/staff/api/availability", method="POST",
                                  json={"updated_at": None, "week": _week(Sunday={"status": "off"})}):
        out = getattr(staff_routes.api_availability_save, "__wrapped__", staff_routes.api_availability_save)(user)
    body = (out[0] if isinstance(out, tuple) else out).get_json()
    assert body["ok"] and body["pending_change"]["summary"] == "Sundays off" and "manager" in body["message"]
    got = ss.own_availability(env["rid"], "Ben", db_path=env["db"], today=TODAY)
    assert got["pending_changes"][0]["summary"] == "Sundays off"


def test_the_manager_routes_list_and_decide_and_need_publish(env, monkeypatch):
    import strategy_routes
    import client_api
    monkeypatch.setattr(client_api, "log_account_event", lambda *a, **k: None)
    res = _save(env, week=_week(Monday={"status": "off"}))
    owner = {"is_admin": True, "restaurant_id": env["rid"], "id": 1}
    got, st = strategy_routes._do_availability_changes_list(owner)
    assert st == 200 and got["changes"][0]["id"] == res["pending_change"]["id"] and got["approval"] is True
    member = {"role": "member", "restaurant_id": env["rid"], "id": 9}
    monkeypatch.setattr(strategy_routes, "_body", lambda: {"decision": "approve"})
    assert strategy_routes._do_availability_change_decide(member, res["pending_change"]["id"])[1] == 403
    out, st = strategy_routes._do_availability_change_decide(owner, res["pending_change"]["id"])
    assert st == 200 and out["change"]["status"] == "approved"


def test_with_approval_off_a_cut_is_approved_for_its_date(env):
    request_autonomy.save_policy(env["rid"], {"availability_approval": False}, db_path=env["db"])
    _publish(env, ["2026-10-10,Saturday,Ben,Server,5:00pm,11:00pm,6,"])
    res = _save(env, week=_week(Saturday={"status": "off"}))
    assert res["pending_change"]["status"] == "approved" and res["pending_change"]["effective_from"] == "2026-10-12"
    c = sr.build_constraints(env["rid"], WEEK_OCT12, DAYS, db_path=env["db"])
    assert not c.can_work("Ben", "2026-10-17")[0]


# ── BUGSGEN-23 ──────────────────────────────────────────────────────────────

def _team_add(env, body):
    import mobile_api
    app = Flask(__name__)
    owner = {"id": 1, "restaurant_id": env["rid"], "is_admin": True, "username": "o", "role": "client"}
    with app.test_request_context("/mobile/api/labor/team/add", method="POST", json=body):
        out = getattr(mobile_api.mobile_add_team_member, "__wrapped__", mobile_api.mobile_add_team_member)(owner)
    resp, st = out if isinstance(out, tuple) else (out, 200)
    return resp.get_json(), st


def test_adding_a_name_a_former_employee_had_is_never_a_silent_fold(env):
    models.add_manual_team_member(env["rid"], "Maria", role="Server", db_path=env["db"])
    ss.upsert(env["rid"], "Maria", active=False, db_path=env["db"])
    body, st = _team_add(env, {"employee_name": "Maria", "role": "Bartender"})
    assert st == 409 and body["same_name"]["name"] == "Maria" and body["same_name"]["active"] is False
    assert "tells them apart" in body["error"]
    # A name that tells them apart is a new person.
    body, st = _team_add(env, {"employee_name": "Maria L.", "role": "Bartender"})
    assert st == 200 and body["ok"]
    # And "it is them" is a deliberate choice.
    body, st = _team_add(env, {"employee_name": "Maria", "role": "Server", "same_person": True})
    assert st == 200 and body["ok"]


def test_two_pos_people_with_one_name_and_different_roles_are_told_apart_by_role(env):
    from people import _Index, _same_name_display
    conn = people._conn(env["db"])
    try:
        idx = _Index(conn, env["rid"])
    finally:
        conn.close()
    assert _same_name_display(idx, "Maria L.", "998877", "Bartender", ["Server"]) == "Maria L. (Bartender)"
    # Same role: the role tells nobody apart, so the POS id does.
    assert _same_name_display(idx, "Maria L.", "998877", "Server", ["Server"]) == "Maria L. #8877"
    assert _same_name_display(idx, "Maria L.", "998877", "Server", [None]) == "Maria L. #8877"

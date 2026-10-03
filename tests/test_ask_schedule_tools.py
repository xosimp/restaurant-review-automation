"""Ask Cavnar's schedule tools keep what the owner meant (schedule audit
10/3/26 PR-19).

generate_schedule took no input at all: "redo Friday with one fewer server"
generated next week, whole, and the ask was lost on confirm. It now carries
the week, the days to rewrite (with the draft they belong to, which the
route requires) and the owner's own words, which reach every model call of
the generation ranked with the Studio notes.

set_staff_unavailable's own example mapped "Maria can't close Sundays" to a
whole Sunday blocked — a hard rule that took her lunch too. Part of a day is
now set_staff_hours: a time window or a daypart, merged into the person's
stored limits, through the staff-settings route the Team screen saves with.
"""
import dataclasses
import sys
import types
from datetime import date, timedelta

import pytest
from flask import Flask

import activity, covers, decisions, delayed, demand_signals, goals, issues, metrics, outcomes  # noqa: E401,F401
import push, schedule_economics, schedule_intel, schedule_rules, schedule_versions  # noqa: E401,F401
import shift_requests, shift_quality, staff_schedule, staff_settings, strategy_jobs, time_off  # noqa: E401,F401
import ai_utils
import ask_cavnar_tools as tools
import auth
import client_api
import labor
import mobile_api
import models
import schedule_engine as se
import strategy_routes
from models import create_restaurant, Restaurant


@pytest.fixture
def db(db_path, monkeypatch):
    real = models.get_conn

    def conn(*a, **k):
        return real(db_path)
    for mod in list(sys.modules.values()):
        bound = getattr(mod, "get_conn", None) if mod is not None else None
        if bound is real or str(getattr(bound, "__module__", "")).startswith(("test_", "tests.", "conftest")):
            monkeypatch.setattr(mod, "get_conn", conn)
    monkeypatch.setattr(models, "get_conn", conn)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(auth, "DB_PATH", db_path)
    auth.init_auth(db_path=db_path)
    return db_path


def _rid(db):
    return create_restaurant(Restaurant(name="Ask Grill", owner_email="a@x.test", module_labor=1), db_path=db)


def _monday(weeks_ahead=1):
    today = date.today()
    return today - timedelta(days=today.weekday()) + timedelta(weeks=weeks_ahead)


def _mdy(d):
    return f"{d.month}/{d.day}/{d.strftime('%y')}"


# ── generate_schedule ──────────────────────────────────────────────────────

def test_generate_schedule_takes_the_week_the_days_and_the_owners_words():
    props = tools._BY_NAME["generate_schedule"]["spec"]["input_schema"]["properties"]
    assert set(props) == {"week_start", "dates", "instruction"}
    assert "never drop one" in tools._BY_NAME["generate_schedule"]["spec"]["description"]


def test_a_whole_week_card_carries_the_week_and_what_the_owner_asked(db):
    rid = _rid(db)
    mon = _monday(2)
    card = tools.build_proposal("generate_schedule",
                                {"week_start": (mon + timedelta(days=3)).isoformat(),
                                 "instruction": "  Maria closes no more than twice  "}, restaurant_id=rid)
    assert card["body"] == {"week_start": mon.isoformat(), "instruction": "Maria closes no more than twice"}
    assert card["summary"] == f"Generate the schedule for the week of {_mdy(mon)}"
    shown = {f["label"]: f["value"] for f in card["fields_shown"]}
    assert shown == {"Week of": _mdy(mon), "What you asked for": "Maria closes no more than twice"}
    assert card["route"]["web"] == "/api/generate-schedule" and card["route"]["status"]


def test_a_redo_card_names_the_days_and_the_draft_they_belong_to(db):
    rid = _rid(db)
    mon = _monday(1)
    hid = models.save_schedule_history(rid, mon.isoformat(), (mon + timedelta(days=6)).isoformat(), 10, 0, 30,
                                       "date,day,employee,role,shift_start,shift_end,scheduled_hours,notes", [],
                                       db_path=db)
    fri, sat = mon + timedelta(days=4), mon + timedelta(days=5)
    card = tools.build_proposal("generate_schedule", {"dates": [sat.isoformat(), fri.isoformat()],
                                                      "instruction": "one fewer server at lunch"}, restaurant_id=rid)
    assert card["body"] == {"week_start": mon.isoformat(), "dates": [fri.isoformat(), sat.isoformat()],
                            "history_id": hid, "instruction": "one fewer server at lunch"}
    assert card["summary"] == (f"Rewrite Fri {_mdy(fri)} and Sat {_mdy(sat)} of the week of {_mdy(mon)} — the other "
                               f"days stay as drafted")


@pytest.mark.parametrize("args,why", [
    ({"dates": "NEXT_FRI"}, "no draft of the week"),
    ({"week_start": "NEXT_MON", "dates": ["2020-01-03"]}, "isn't in the week of"),
    ({"week_start": "2020-01-06"}, "already happened"),
    ({"week_start": "next week"}, "date in it"),
])
def test_a_card_that_could_not_run_is_refused_with_why(db, args, why):
    rid = _rid(db)
    mon = _monday(1)
    a = {k: ([(mon + timedelta(days=4)).isoformat()] if v == "NEXT_FRI" else mon.isoformat() if v == "NEXT_MON" else v)
         for k, v in args.items()}
    assert tools.build_proposal("generate_schedule", a, restaurant_id=rid) is None
    assert why in tools.proposal_refusal("generate_schedule", a, restaurant_id=rid)


def _app():
    app = Flask(__name__, template_folder="../templates")
    app.register_blueprint(mobile_api.mobile_bp)
    app.register_blueprint(client_api.client_bp)
    app.register_blueprint(strategy_routes.strategy_bp)
    app.register_blueprint(strategy_routes.strategy_mobile_bp)
    return app


def _bearer(db, rid, name="owner", role="client"):
    uid = auth.create_user(rid, name, f"{name}@x.test", "pw", db_path=db)
    conn = models.get_conn(db)
    conn.execute("UPDATE users SET role=? WHERE id=?", (role, uid))
    conn.commit()
    conn.close()
    return {"Authorization": f"Bearer {auth.create_session(uid, db_path=db)}"}


def test_the_route_hands_the_owners_words_to_the_job(db, monkeypatch):
    rid = _rid(db)
    monkeypatch.setattr(ai_utils, "ai_rate_limited", lambda *a, **k: False)
    started = []
    # The job goes on the bounded generation pool (schedule audit 10/3/26
    # P-39), never a thread of the route's own: what the pool is handed is
    # what the job runs with.
    monkeypatch.setattr(se, "submit_generation", lambda job_id, rid, **kw: started.append(dict(kw)))
    client = _app().test_client()
    r = client.post("/mobile/api/labor/generate-schedule", headers=_bearer(db, rid),
                    json={"instruction": "  give Maria  Sunday lunch "})
    assert r.status_code == 200 and started[-1]["instruction"] == "give Maria Sunday lunch"
    other = _rid(db)                                   # its own restaurant: no job to join
    r2 = client.post("/mobile/api/labor/generate-schedule", headers=_bearer(db, other, name="o2"), json={})
    assert r2.status_code == 200 and len(started) == 2 and "instruction" not in started[-1]


def test_the_job_and_the_build_carry_the_words_to_every_model_call(db, monkeypatch):
    seen = {}

    def fake_build(r, **kw):
        seen.update(kw)
        raise se.ScheduleGenerationError("stop here")
    monkeypatch.setattr(se, "_build_schedule_result", fake_build)
    monkeypatch.setattr(se._ops, "finish_async_job", lambda *a, **k: None)
    se._run_schedule_job("job-i", 1, instruction="Maria closes twice at most")
    assert seen["instruction"] == "Maria closes twice at most"


def test_the_prompt_ranks_the_owners_request_with_the_studio_notes_and_fences_nothing_open(monkeypatch):
    seen = []
    monkeypatch.setattr(labor, "create_with_retry", lambda client, **kw: seen.append(kw) or types.SimpleNamespace(
        content=[types.SimpleNamespace(type="text", text='{"days":[],"summary":[]}')], stop_reason="end_turn"))
    monkeypatch.setattr(labor, "get_client", lambda *a, **k: None)
    import ai_guard
    labor.generate_optimized_schedule(
        {"overall_labor_pct": 25, "overstaffed_days": [], "understaffed_days": [], "dow_summary": {}},
        [], roster=[("Maria", "Server")], instruction="Maria closes twice at most " + ai_guard.UNTRUSTED_CLOSE)
    prompt = seen[0]["messages"][0]["content"]
    at = prompt.index("THE OWNER'S REQUEST FOR THIS DRAFT")
    assert "priority 5" in prompt[at:at + 200] and "Maria closes twice at most" in prompt[at:]
    assert ai_guard.UNTRUSTED_CLOSE not in prompt[at:at + 400]


# ── part of a day ─────────────────────────────────────────────────────────

def test_cant_close_sundays_is_no_longer_a_whole_day_off():
    desc = tools._BY_NAME["set_staff_unavailable"]["spec"]["description"]
    assert "'Maria can't close Sundays' → Maria, Sunday" not in desc
    assert "set_staff_hours" in desc and "AT ALL" in desc
    hours = tools._BY_NAME["set_staff_hours"]
    assert "can't close" in hours["spec"]["description"] and hours["confirm"] is True
    assert hours["route"]["web"] == "/api/labor/staff-settings"
    assert "set_staff_hours" in tools._BY_NAME["remember"]["spec"]["description"]


def test_a_part_day_card_merges_into_the_persons_limits_and_the_route_holds_it(db, monkeypatch):
    rid = _rid(db)
    models.add_manual_team_member(rid, "Maria", role="Server", db_path=db)
    staff_settings.upsert(rid, "Maria", time_windows={"Monday": {"earliest": "4:00pm"}},
                          daypart_availability={"Tuesday": "night"}, db_path=db)
    card = tools.build_proposal("set_staff_hours", {"employee_name": "maria", "weekdays": ["Sunday"],
                                                    "latest": "8pm"}, restaurant_id=rid)
    assert card["route"]["mobile"] == "/mobile/api/labor/staff-settings"
    assert card["body"] == {"employee_name": "Maria",
                            "time_windows": {"Monday": {"earliest": "4:00pm", "latest": None},
                                             "Sunday": {"latest": "8:00pm"}}}
    assert card["summary"] == "Maria on Sunday: done by 8:00pm"
    shown = {f["label"]: f["value"] for f in card["fields_shown"]}
    assert "Sunday: until 8:00pm" in shown["Hours they can work"] and "Monday: from 4:00pm" in shown[
        "Hours they can work"]
    # confirmed through the route the Team screen saves with
    r = _app().test_client().post(card["route"]["mobile"], headers=_bearer(db, rid), json=card["body"])
    assert r.status_code == 200, r.get_json()
    sun = (_monday(1) + timedelta(days=6)).isoformat()
    week = [(_monday(1) + timedelta(days=i)).isoformat() for i in range(7)]
    c = schedule_rules.build_constraints(rid, week, list(schedule_rules.DAYS), models.get_restaurant(rid))
    assert c.can_work("Maria", sun)[0]                                     # Sunday is not a day off
    assert c.window_ok("Maria", sun, "11:00am", "3:00pm")[0]                # lunch is fine
    assert not c.window_ok("Maria", sun, "4:00pm", "10:00pm")[0]            # closing is not
    assert c.daypart_avail["maria"]["Tuesday"] == "night"                   # the other days' limits kept
    assert not c.window_ok("Maria", week[0], "11:00am", "3:00pm")[0]


def test_a_daypart_card_keeps_them_to_lunch_or_dinner(db):
    rid = _rid(db)
    models.add_manual_team_member(rid, "Ben", role="Server", db_path=db)
    card = tools.build_proposal("set_staff_hours", {"employee_name": "Ben", "weekdays": ["Friday"],
                                                    "daypart": "morning"}, restaurant_id=rid)
    assert card["body"] == {"employee_name": "Ben", "daypart_availability": {"Friday": "morning"}}
    assert card["summary"] == "Ben on Friday: lunch/day only"
    assert {f["label"]: f["value"] for f in card["fields_shown"]}["Shifts they can work"] == "Friday: lunch/day only"


@pytest.mark.parametrize("args,why", [
    ({"employee_name": "Ben", "weekdays": ["Friday"]}, "Say what part of the day"),
    ({"employee_name": "Ben", "weekdays": ["Friday"], "latest": "late"}, "isn't a time"),
    ({"employee_name": "Ben", "weekdays": ["Friday"], "earliest": "9pm", "latest": "4pm"}, "ends before it starts"),
    ({"employee_name": "Nobody", "weekdays": ["Friday"], "latest": "8pm"}, "no Nobody on the roster"),
    ({"employee_name": "Ben", "weekdays": ["Sunday"], "daypart": "night"}, "marked off"),
])
def test_a_part_day_card_that_could_not_hold_is_refused(db, args, why):
    rid = _rid(db)
    models.add_manual_team_member(rid, "Ben", role="Server", db_path=db)
    staff_settings.upsert(rid, "Ben", daypart_availability={"Sunday": "off"}, db_path=db)
    assert tools.build_proposal("set_staff_hours", args, restaurant_id=rid) is None
    assert why in tools.proposal_refusal("set_staff_hours", args, restaurant_id=rid)


def test_a_login_that_cannot_manage_the_team_is_not_offered_the_card(db):
    rid = _rid(db)
    r = models.get_restaurant(rid)
    view = dataclasses.replace(r)
    view._ask_dsr_user = {"role": "employee"}
    assert not tools.tool_allowed("set_staff_hours", view)
    view._ask_dsr_user = {"role": "client"}
    assert tools.tool_allowed("set_staff_hours", view)

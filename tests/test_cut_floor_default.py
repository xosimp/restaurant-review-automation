"""The floor a cut suggestion never goes below (schedule_rules.cut_floor).

With no staffing floor set for a role, every cut path assumed a floor of
one: the pre-dinner pulse (strategy_jobs.staffing_move) suggested sending
home one of two cooks, and the Response Validation Layer's A2 let "cut to
one cook" through. Will's decision: there is always a default minimum, and
each restaurant sets it (restaurants.cut_floor_default, 2 unless changed).

The order is the owner's role floor for the slot, then Will's admin
role_minimums_json, then the restaurant's default; never below one. It is a
floor for CUTS only: the coverage_floor violation and schedule building
still read the owner's floors alone. Each test here failed before the fix.
"""
import dataclasses
import datetime as dt
import inspect
import json

import pytest

import auth
import intraday
import labor
import models
import response_validation as rv
import schedule_engine
import schedule_rules as sr
import schedule_versions as sv
import staff_settings as ss
import strategy_jobs
import time_off
from models import Restaurant, create_restaurant, get_conn, get_restaurant, update_restaurant

HEADER = "date,day,employee,role,shift_start,shift_end,scheduled_hours,notes\n"
DAY = dt.date(2026, 9, 21)                 # a Monday, no holiday
LOCAL = dt.datetime(2026, 9, 21, 16, 30)
PULSE = {"direction": "behind", "pct": -30, "samples": 6}


@pytest.fixture
def db(db_path, monkeypatch):
    real = models.get_conn
    for mod in (models, auth, intraday, schedule_engine, sr, ss, time_off, strategy_jobs):
        monkeypatch.setattr(mod, "get_conn", lambda *a, **k: real(db_path), raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(sr, "DB_PATH", db_path, raising=False)
    return db_path


def _rest(db, name, **cols):
    rid = create_restaurant(Restaurant(name=name, owner_email=f"{name.lower()}@x.com"), db_path=db)
    update_restaurant(rid, dict({"hourly_rate": 15, "module_labor": 1}, **cols), db_path=db)
    return rid


def _cooks(db, rid, n):
    """n cooks on at the 8pm cut, each starting later than the last."""
    csv_text = HEADER
    for i in range(n):
        csv_text += f"{DAY.isoformat()},Monday,Cook{i + 1},Cook,{3 + i}:00pm,11:00pm,{8 - i}.0,\n"
    conn = get_conn(db)
    models._ensure_history_columns(conn)
    conn.execute("INSERT INTO schedule_history (restaurant_id, week_start, week_end, schedule_csv, summary_json, "
                 "published_at) VALUES (?,?,?,?,'[]',datetime('now'))",
                 (rid, DAY.isoformat(), DAY.isoformat(), csv_text))
    conn.commit()
    conn.close()


def _move(db, rid):
    return strategy_jobs.staffing_move(get_restaurant(rid, db), LOCAL, PULSE, db_path=db)


# ── the column: four touch points ──────────────────────────────────────

def test_the_column_is_in_every_touch_point(db):
    assert {f.name: f.default for f in dataclasses.fields(Restaurant)}["cut_floor_default"] == 2
    assert '"cut_floor_default"' in inspect.getsource(models.update_restaurant)
    rid = _rest(db, "Col")
    assert get_restaurant(rid, db).cut_floor_default == 2, "the migration's DEFAULT 2 reaches a new row"
    update_restaurant(rid, {"cut_floor_default": 4}, db_path=db)
    assert get_restaurant(rid, db).cut_floor_default == 4


def test_the_default_is_clamped_and_survives_junk():
    assert sr.cut_floor_default(Restaurant(name="x", owner_email="x@x.com", cut_floor_default=0)) == 1
    assert sr.cut_floor_default(Restaurant(name="x", owner_email="x@x.com", cut_floor_default=40)) == sr.CUT_FLOOR_MAX == 10
    assert sr.cut_floor_default(Restaurant(name="x", owner_email="x@x.com", cut_floor_default="lots")) == 2
    assert sr.cut_floor_default(Restaurant(name="x", owner_email="x@x.com", cut_floor_default=None)) == 2
    assert sr.cut_floor_default(None) == 2 == rv.CUT_FLOOR_DEFAULT


def test_the_fallback_order_is_owner_floor_then_admin_minimum_then_default():
    r = Restaurant(name="x", owner_email="x@x.com", cut_floor_default=3,
                   role_floors_json=json.dumps({"Line Cook": {"morning": 1, "night": 4, "days": {"Friday": {"night": 5}}}}),
                   role_minimums_json=json.dumps({"server": 2, "Host": 0}))
    assert sr.cut_floor(r, "line cook", "Monday", "night") == 4       # owner's floor
    assert sr.cut_floor(r, "Line Cook", "Friday", "night") == 5       # its per-day override
    assert sr.cut_floor(r, "Line Cook", "Monday", "morning") == 1
    assert sr.cut_floor(r, "Server", "Monday", "night") == 2          # admin minimum, any case
    assert sr.cut_floor(r, "Host", "Monday", "night") == 3            # a zero minimum is no minimum
    assert sr.cut_floor(r, "Bartender", "Monday", "night") == 3       # the restaurant's default
    r1 = dataclasses.replace(r, cut_floor_default=None, role_minimums_json="{not json")
    assert sr.cut_floor(r1, "Bartender", "Monday", "night") == 2      # unreadable → 2, never 1


# ── the pulse ──────────────────────────────────────────────────────────

def test_two_cooks_and_no_floor_set_means_no_cook_is_sent_home(db):
    rid = _rest(db, "Two")
    _cooks(db, rid, 2)
    assert _move(db, rid) is None, "one of two cooks sent home with no floor set"


def test_a_default_of_one_lets_the_pulse_cut_the_second_cook(db):
    rid = _rest(db, "One", cut_floor_default=1)
    assert get_restaurant(rid, db).cut_floor_default == 1
    _cooks(db, rid, 2)
    move = _move(db, rid)
    assert move and move["employee"] == "Cook2" and move["floor"] == 1


def test_three_cooks_against_the_default_of_two_allows_one_cut(db):
    rid = _rest(db, "Three")
    _cooks(db, rid, 3)
    move = _move(db, rid)
    assert move and move["employee"] == "Cook3" and move["floor"] == 2
    assert "against a floor of 2" in move["text"]


def test_the_owners_floor_wins_over_the_default(db):
    rid = _rest(db, "Owner", cut_floor_default=1,
                role_floors_json=json.dumps({"Cook": {"night": 3}}))
    _cooks(db, rid, 3)
    assert get_restaurant(rid, db).cut_floor_default == 1
    assert _move(db, rid) is None, "the owner's night floor of 3 keeps all three"
    # …and downward: an owner who set one cook at night is not overruled.
    rid2 = _rest(db, "OwnerLow", role_floors_json=json.dumps({"Cook": {"night": 1}}))
    _cooks(db, rid2, 2)
    move = _move(db, rid2)
    assert move and move["floor"] == 1


def test_the_admin_role_minimum_is_honoured_by_the_pulse(db):
    rid = _rest(db, "Min", cut_floor_default=1, role_minimums_json=json.dumps({"cook": 3}))
    _cooks(db, rid, 3)
    assert _move(db, rid) is None, "role_minimums_json {cook: 3} ignored by the pulse"
    update_restaurant(rid, {"role_minimums_json": json.dumps({"COOK": 2})}, db_path=db)
    move = _move(db, rid)
    assert move and move["floor"] == 2 and move["employee"] == "Cook3"


def test_the_pulse_docstring_no_longer_says_one_person_is_the_floor():
    doc = strategy_jobs.staffing_move.__doc__
    assert "one person is the floor" not in doc and "cut_floor" in doc


# ── A2 in the Response Validation Layer ────────────────────────────────

def _a2(text, policy):
    v = rv.validate(text, rv.ValidationContext(surface="ask", policy=policy))
    return [f for f in v.findings if f["rule"] == "A2"]


def test_a2_drops_cut_to_one_cook_when_no_floor_is_set_and_the_default_is_two():
    assert _a2("Cut to one cook after 8.", {"cut_floor_default": 2})
    assert _a2("Cut to one cook after 8.", {}), "no policy default: the engine's CUT_FLOOR_DEFAULT (2), not 1"
    assert not _a2("Cut to one cook after 8.", {"cut_floor_default": 1})
    assert not _a2("Cut to two cooks after 8.", {"cut_floor_default": 2})
    assert _a2("Cut to two cooks after 8.", {"cut_floor_default": 3})


def test_a2_reads_the_owners_floor_before_the_default():
    assert not _a2("Cut to one cook after 8.", {"role_floors": {"Cook": 1}, "cut_floor_default": 2})
    # "cook" in the text is the owner's "Line Cook"
    assert not _a2("Cut to one cook after 8.", {"role_floors": {"Line Cook": 1}, "cut_floor_default": 2})
    assert _a2("Cut to two cooks after 8.", {"role_floors": {"Line Cook": 3}, "cut_floor_default": 1})


def test_the_schedule_note_holds_a_role_with_no_floor_to_the_restaurants_default(db):
    prompt = ("CONTEXT:\n- Active staff: Ana (Server), Ben (Cook), Cal (Cook).\n"
              "Monday dinner needs 2 cooks. Hours ceiling 212. Labor target 28%.")
    rid = _rest(db, "Note")
    assert labor.schedule_note_problem("Monday dinner down to 1 cook.", prompt, restaurant_id=rid), \
        "a cut to one cook with no floor set is a cut below the default"
    update_restaurant(rid, {"cut_floor_default": 1}, db_path=db)
    assert labor.schedule_note_problem("Monday dinner down to 1 cook.", prompt, restaurant_id=rid) is None
    ctx = labor.schedule_note_context(prompt, rid)
    assert ctx.policy["cut_floor_default"] == 1


def test_note_floors_follows_the_cut_floor_order():
    """The owner's floor first; the admin minimum only for a role with none."""
    floors = {"Cook": {"morning": 1, "night": 1}}
    assert labor.note_floors("Monday night", floors, {"cook": 3, "Server": 2}) == {"Cook": 1, "Server": 2}


def test_the_ask_and_unattended_contexts_carry_the_restaurants_cut_policy(db):
    import ask_cavnar
    rid = _rest(db, "Ask", cut_floor_default=3, role_floors_json=json.dumps({"Server": {"night": 2}}))
    pol = ask_cavnar._validation_context(["corpus"], rid).policy
    assert pol["cut_floor_default"] == 3 and pol["role_floors"] == {"Server": 2}
    assert sr.cut_policy(rid) == {"role_floors": {"Server": 2}, "cut_floor_default": 3}
    assert sr.cut_policy(None) == {}


# ── cuts only: the coverage_floor violation is unchanged ────────────────

def test_the_default_is_not_a_staffing_requirement(db):
    rid = _rest(db, "Cov", cut_floor_default=5)
    assert get_restaurant(rid, db).cut_floor_default == 5
    week = ["2026-10-05"]
    csv_text = HEADER + "2026-10-05,Monday,Ana,Bartender,4:00pm,11:00pm,7.0,\n"
    c = sr.build_constraints(rid, week, ["Monday"])
    assert not [v for v in sr.violations(sv.rows_from_csv(csv_text), c) if v["kind"] == "coverage_floor"], \
        "one bartender on a published week is not a violation because of the cut floor"
    assert c.role_floors == {}


# ── the rules route (web and mobile share the body) ────────────────────

def test_the_rules_route_accepts_validates_and_returns_the_default(db, monkeypatch):
    import strategy_routes
    rid = _rest(db, "Route")
    owner = {"id": 1, "restaurant_id": rid, "role": "client", "is_admin": False, "username": "o"}
    payload, status = strategy_routes._do_compliance_get(owner)
    assert status == 200 and payload["cut_floor_default"] == 2 and payload["cut_floor_max"] == 10
    monkeypatch.setattr(strategy_routes, "_body", lambda: {"cut_floor_default": 3})
    payload, status = strategy_routes._do_compliance_set(owner)
    assert status == 200 and payload["cut_floor_default"] == 3
    assert strategy_routes._do_compliance_get(owner)[0]["cut_floor_default"] == 3
    for junk in ("lots", None, 0, 11, 2.5, -1, True, ""):
        monkeypatch.setattr(strategy_routes, "_body", lambda junk=junk: {"cut_floor_default": junk,
                                                                          "trim_to_budget": False})
        payload, status = strategy_routes._do_compliance_set(owner)
        assert status == 400 and "1 to 10" in payload["error"], junk
    r = get_restaurant(rid, db)
    assert r.cut_floor_default == 3 and r.trim_to_budget == 1, "a refused value changes nothing"


def test_the_admin_settings_save_accepts_and_refuses_the_default(db, monkeypatch):
    from flask import Flask
    import admin_routes
    monkeypatch.setattr(auth, "get_current_user", lambda: {"id": 999, "is_admin": 1})
    rid = _rest(db, "Admin")
    app = Flask(__name__)
    app.register_blueprint(admin_routes.admin_bp)

    def save(value):
        with app.test_request_context(f"/admin/client-settings/{rid}", method="POST",
                                      json={"name": "Admin", "owner_email": "admin@x.com",
                                            "cut_floor_default": value}):
            return admin_routes.save_client_settings(rid).get_json()

    assert save("4")["ok"] and get_restaurant(rid, db).cut_floor_default == 4
    for junk in ("", "0", "11", "two", "1.5"):
        out = save(junk)
        assert not out["ok"] and "1 to 10" in out["error"], junk
    assert get_restaurant(rid, db).cut_floor_default == 4
    html = open("templates/client_settings.html").read()
    assert 'id="cut_floor_default"' in html and "getElementById('cut_floor_default')" in html

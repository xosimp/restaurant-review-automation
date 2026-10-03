"""Schedule fix round 10/3/26, workstream F1 — the owner's scheduling setup,
said back and stored through both API twins:

  E-14   "Managers: …" on the rules screen for confirmation
  D-9    the closers' data-quality warning past 30% and a cleanup from the
         punches that changes nothing until the owner sends it
  D-10 / P-18  leader rules load without ratings; "inactive: nobody rated
         (N ratings entered through support)" on the generate screen
  D-11   a leader rule checked as it is saved; a new rule's bar is 4
  D-13   role families suggested from the job codes, stored by the owner
  D-14   a staffing rule said back as it will be checked
  D-42   floors suggested from the 25th percentile of history, never saved
  D-43   the rules screen's one stays-after-close setting writes both
  routes owner-only where it is the owner's data; web and mobile one body
"""
import json
import sys
from datetime import date, timedelta

import pytest
from flask import Flask

import auth
import models
import schedule_engine
import schedule_rules as sr
import schedule_setup as setup
import staff_settings as ss
import strategy_routes
from models import Restaurant, create_restaurant

WEEK = [(date(2026, 10, 12) + timedelta(days=i)).isoformat() for i in range(7)]
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
HISTORY = {}
OWNER = {"id": 1, "role": "client", "is_admin": 0, "username": "erik"}
MANAGER = {"id": 2, "role": "manager", "is_admin": 0, "username": "dana"}


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
    monkeypatch.setattr(auth, "DB_PATH", db_path)
    auth.init_auth(db_path=db_path)
    HISTORY.clear()
    monkeypatch.setattr(models, "_cached_shifts", lambda r: HISTORY.get(r, []))
    monkeypatch.setattr(setup, "coming_week", lambda rid: list(WEEK))
    yield


def _rid(**cols):
    rid = create_restaurant(Restaurant(name="F1 Grill", owner_email="f1@x.test", timezone="America/Chicago"))
    if cols:
        models.update_restaurant(rid, cols)
    return rid


def _u(base, rid):
    return dict(base, restaurant_id=rid)


def _call(fn, u, body=None, *args):
    with Flask(__name__).test_request_context(json=body or {}):
        return fn(u, *args)


def _punch(rid, name, role, d, start="4:00pm", end="10:00pm"):
    HISTORY.setdefault(rid, []).append({"date": d, "employee": name, "role": role, "shift_start": start,
                                        "shift_end": end, "scheduled_hours": "6"})


def _team(rid, people):
    for n, role in people:
        models.add_manual_team_member(rid, n, role=role)


# ── E-14: "Managers: …" for the owner to confirm ───────────────────────────

def test_the_rules_screen_says_who_runs_the_floor_and_who_was_left_out():
    rid = _rid()
    _team(rid, [("Erik", "Owner"), ("Andrew", "Manager FOH"), ("Kim", "Kitchen Manager"), ("Ana", "Server")])
    out, status = _call(strategy_routes._do_compliance_get, _u(OWNER, rid))
    assert status == 200
    assert out["managers_line"] == "Managers: Andrew (Manager FOH), Erik (Owner)"
    assert [m["name"] for m in out["managers"]["not_counted"]] == ["Kim"]
    assert "Which days and hours do Andrew and Erik work?" in out["managers"]["ask_standing"]
    assert out["salaried_cap"] == 55.0 and out["certification_labels"]["manager"].startswith("Floor manager")


def test_only_the_owner_sets_who_runs_the_floor_and_both_twins_serve_it():
    rid = _rid()
    _team(rid, [("Ana", "Server"), ("Andrew", "Manager FOH")])
    out, status = _call(strategy_routes._do_managers_set, _u(MANAGER, rid), {"name": "Ana", "floor_manager": True})
    assert status == 403
    out, status = _call(strategy_routes._do_staff_settings_set, _u(MANAGER, rid),
                        {"employee_name": "Ana", "floor_manager": True})
    assert status == 403, "a manager can't make themselves — or anyone — the floor manager"
    out, status = _call(strategy_routes._do_managers_set, _u(OWNER, rid), {"name": "Ana", "floor_manager": True})
    assert status == 200 and "Ana (Server)" in out["line"]
    out, status = _call(strategy_routes._do_managers_set, _u(OWNER, rid), {"name": "Ana", "floor_manager": "auto"})
    assert "Ana" not in out["line"]
    paths = {(r[0], tuple(r[1])) for r in strategy_routes._ROUTES}
    for p in ("/labor/managers", "/labor/closers", "/labor/role-families"):
        assert (p, ("GET",)) in paths and (p, ("POST",)) in paths
    assert ("/labor/floors/suggest", ("GET",)) in paths


def test_a_manager_may_still_set_standing_shifts_and_training():
    rid = _rid()
    _team(rid, [("Ana", "Server"), ("Marcus", "Bartender")])
    out, status = _call(strategy_routes._do_staff_settings_set, _u(MANAGER, rid), {
        "employee_name": "Ana", "standing_shifts": [{"day": "Monday", "start": "10:00am", "end": "4:00pm"}],
        "trainee": {"target_role": "Bartender", "trainer": "Marcus", "until": "12/1/26"}})
    assert status == 200 and out["settings"]["trainee"]["trainer"] == "Marcus"
    out, status = _call(strategy_routes._do_staff_settings_set, _u(MANAGER, rid), {
        "employee_name": "Ana", "trainee": {"target_role": "Bartender", "trainer": "Nobody", "until": "12/1/26"}})
    assert status == 400 and "trainer" in out["error"]


# ── D-9: the closers' cleanup ─────────────────────────────────────────────

def test_too_many_closers_is_a_warning_and_the_punches_suggest_who_closes():
    rid = _rid()
    _team(rid, [("Sam", "Bartender PM"), ("Lee", "Bartender PM"), ("Dee", "Dishwasher"), ("Ana", "Server PM")])
    for n in ("Sam", "Lee", "Dee"):
        models.set_capability(rid, n, attribute="can_close", flag=True)
    for i in range(1, 6):
        d = (date(2026, 10, 1) + timedelta(days=i)).isoformat()
        _punch(rid, "Sam", "Bartender PM", d, "5:00pm", "12:00am")
        _punch(rid, "Lee", "Bartender PM", d, "5:00pm", "11:00pm")
        _punch(rid, "Ana", "Server PM", d, "5:00pm", "11:30pm")
    review = setup.closer_review(rid, today=date(2026, 10, 10))
    assert review["flagged"] == 3 and review["roster"] == 4 and "3 of 4 people (75%)" in review["warning"]
    actions = {s["name"]: s["action"] for s in review["suggestions"]}
    assert actions == {"Sam": "keep", "Lee": "unmark", "Dee": "unmark", "Ana": "add"}
    assert models.get_leader_flags(rid).keys() == {"Sam", "Lee", "Dee"}, "a suggestion changes nothing"
    out, status = _call(strategy_routes._do_closers_set, _u(MANAGER, rid), {"changes": [{"name": "Lee", "can_close": False}]})
    assert status == 403
    out, status = _call(strategy_routes._do_closers_set, _u(OWNER, rid),
                        {"changes": [{"name": "Lee", "can_close": False}, {"name": "Dee", "can_close": False}],
                         "closer_roles": ["Bartender"]})
    assert status == 200 and out["applied"] == 2 and out["warning"] is None
    assert out["by_role"] == [{"role": "Bartender", "family": "bartender", "closers": ["Sam"]}]


# ── D-10 / P-18: leader rules load without ratings ────────────────────────

def test_leader_rules_are_loaded_whatever_the_ratings():
    import inspect
    src = inspect.getsource(schedule_engine._build_schedule_result)
    assert "_leader_rules = get_shift_leader_rules(restaurant_id)\n" in src
    src = inspect.getsource(schedule_engine.quality_inputs_from_db)
    assert "leader_rules = get_shift_leader_rules(restaurant_id)\n" in src


def test_the_generate_screen_says_leader_rules_judge_nobody_and_counts_supports_ratings():
    rid = _rid(shift_leader_rules_json=json.dumps([{"role": "Bartender AM", "min_score": 5, "count": 1},
                                                   {"role": "Bartender PM", "closing": True, "attribute": "can_close"}]))
    _team(rid, [("Sam", "Bartender PM"), ("Lee", "Bartender AM")])
    models.set_capability(rid, "Sam", score=5)
    conn = models.get_conn()
    conn.execute("UPDATE staff_capabilities SET authority='admin' WHERE restaurant_id=?", (rid,))
    conn.commit()
    conn.close()
    st = setup.leader_rules_status(rid, ["Sam", "Lee"])
    assert st["rules"] == 2 and st["admin_ratings"] == 1 and not st["active"] and st["can_adopt"]
    assert st["lines"][0] == ("1 leader rule that need a rating is inactive: nobody on the roster is rated "
                              "(1 rating entered through support is waiting for you to count them as yours).")
    assert "nobody on the roster is marked to close" in st["lines"][1]


# ── D-11: a leader rule checked as it is saved ────────────────────────────

def test_an_unmeetable_leader_rule_is_said_when_saved():
    rid = _rid()
    _team(rid, [("Sam", "Bartender AM"), ("Lee", "Bartender PM"), ("Bo", "Bartender PM")])
    models.set_capability(rid, "Lee", score=5)
    rules, errors = setup.clean_leader_rules([{"role": "Bartender AM", "min_score": 5, "count": 1}])
    assert not errors and rules == [{"role": "Bartender AM", "min_score": 5, "count": 1}]
    w = setup.leader_rule_warnings(rid, rules)
    assert w and w[0]["text"] == "Only 1 Bartender AM scores 5 or above, so this rule can't be met on 1 of its 7 shifts."
    assert w[0]["able"] == ["Lee"], "a Bartender PM covers a Bartender AM rule's lunch (one role)"
    two = setup.leader_rule_warnings(rid, [{"role": "Bartender AM", "min_score": 5, "count": 2}])
    assert "can't be met on 7 of its 7 shifts" in two[0]["text"]
    for bad in ([{"min_score": 5}], [{"role": "Host", "count": 0}], [{"role": "Host", "min_score": 6}],
                [{"role": "Host", "days": ["Funday"]}], [{"role": "Host", "attribute": "overall"}]):
        assert setup.clean_leader_rules(bad)[1], bad
    assert models.LEADER_RULE_DEFAULTS["min_score"] == 4


def test_the_thresholds_save_refuses_a_broken_rule_and_warns_on_an_unmeetable_one(monkeypatch):
    import mobile_api
    rid = _rid()
    _team(rid, [("Sam", "Bartender AM")])
    monkeypatch.setattr(mobile_api, "_may_manage_team", lambda u: True)
    app = Flask(__name__)
    with app.test_request_context(json={"thresholds": {}, "leader_rules": [{"role": "", "min_score": 5}]}):
        resp, status = mobile_api.mobile_set_thresholds.__wrapped__(_u(OWNER, rid))
    assert status == 400
    with app.test_request_context(json={"thresholds": {}, "leader_rules": [{"role": "Bartender AM", "min_score": 5}]}):
        resp, status = mobile_api.mobile_set_thresholds.__wrapped__(_u(OWNER, rid))
    body = resp.get_json()
    assert status == 200 and body["leader_rule_warnings"] and "Nobody in Bartender AM is rated yet" in \
        body["leader_rule_warnings"][0]
    assert json.loads(models.get_restaurant(rid).shift_leader_rules_json) == [
        {"role": "Bartender AM", "min_score": 5, "count": 1}]


# ── D-13: role families from the job codes ───────────────────────────────

def test_role_families_are_suggested_from_the_job_codes_and_stored_by_the_owner():
    rid = _rid()
    _team(rid, [("A", "Server AM"), ("B", "Server PM"), ("C", "Barback PM")])
    out, _ = _call(strategy_routes._do_role_families_get, _u(OWNER, rid))
    assert {"family": "server", "label": "Server", "roles": ["Server AM", "Server PM"], "source": "suggested"} \
        in out["families"]
    out, status = _call(strategy_routes._do_role_families_set, _u(MANAGER, rid), {"families": {"Barback PM": "Bartender"}})
    assert status == 403
    out, status = _call(strategy_routes._do_role_families_set, _u(OWNER, rid), {"families": {"Barback PM": "Bartender"}})
    assert status == 200 and out["stored"] == {"barback pm": "bartender"}
    out, status = _call(strategy_routes._do_role_families_set, _u(OWNER, rid), {"families": {"Barback PM": ""}})
    assert status == 400


# ── D-14: a staffing rule said back as it will be checked ─────────────────

def test_a_saved_staffing_rule_is_read_back():
    rid = _rid()
    _team(rid, [("A", "Server AM"), ("B", "Server PM")])
    p = setup.owner_rule_preview(rid, "Always two servers on Saturday night")
    assert p == {"checked": True, "reads_as": "at least 2 Server PM on Sat at dinner/night",
                 "text": "Checked on every draft as: at least 2 Server PM on Sat at dinner/night."}
    assert setup.owner_rule_preview(rid, "Keep it fair for the new hires")["checked"] is False


# ── D-42: floors from history ─────────────────────────────────────────────

def test_floors_are_suggested_from_the_25th_percentile_and_never_saved():
    rid = _rid()
    _team(rid, [("S1", "Server PM"), ("S2", "Server PM"), ("S3", "Server PM"), ("M", "Manager FOH")])
    out = setup.suggest_role_floors(rid, today=date(2026, 9, 20))
    assert out["floors"] == {} and sr.role_floors(models.get_restaurant(rid)) == {}, "no history, nothing suggested"
    for i in range(8):
        d = (date(2026, 9, 1) + timedelta(days=i)).isoformat()
        on = ["S1", "S2"] + (["S3"] if i % 2 else [])
        for n in on:
            _punch(rid, n, "Server PM", d)
        _punch(rid, "M", "Manager FOH", d)
        _punch(rid, "T", "Training", d)
    out = setup.suggest_role_floors(rid, today=date(2026, 9, 20))
    assert out["floors"] == {"Server": {"morning": 0, "night": 2, "days": {}}}, "per role, any of its job codes"
    models.update_restaurant(rid, {"role_floors_json": json.dumps(out["floors"])})
    c = sr.build_constraints(rid, WEEK, DAYS)
    assert sr.floor_for(c.role_floors, "Server PM", "Friday", "night") == 2, "held on the code that works dinner"
    body, status = _call(strategy_routes._do_floor_suggestions, _u(OWNER, rid))
    assert status == 200 and "floors" in body


# ── D-43: one stays-after-close setting, written to both ──────────────────

def test_the_rules_screens_stay_after_close_writes_both_settings():
    rid = _rid(role_close_buffer_json='{"Bartender": 60}', role_close_min_json='{"Bartender": 30}')
    out, _ = _call(strategy_routes._do_compliance_get, _u(OWNER, rid))
    assert out["role_close_mins"] == {"Bartender": 30}
    assert out["role_close_conflicts"] == [{"role": "Bartender", "stays": 30, "may_run": 60}]
    out, status = _call(strategy_routes._do_compliance_set, _u(OWNER, rid), {"role_close_mins": {"Bartender": 45}})
    assert status == 200
    r = models.get_restaurant(rid)
    assert json.loads(r.role_close_min_json) == {"Bartender": 45} == json.loads(r.role_close_buffer_json)
    out, status = _call(strategy_routes._do_compliance_set, _u(OWNER, rid), {"salaried_cap": 100})
    assert status == 400
    out, status = _call(strategy_routes._do_compliance_set, _u(OWNER, rid), {"salaried_cap": 50, "closer_roles": ["Bartender"]})
    assert status == 200 and out["salaried_cap"] == 50.0 and out["closer_roles"] == ["Bartender"]


# ── the generation's review ───────────────────────────────────────────────

def test_the_review_names_the_setup_a_week_was_drafted_against():
    rid = _rid(close_times_json='{"Friday": "12:00am", "Saturday": "12:00am"}')
    _team(rid, [("Erik", "Owner"), ("Kim", "Kitchen Manager"), ("Ana", "Server"), ("Sam", "Bartender")])
    for n in ("Ana", "Sam"):
        models.set_capability(rid, n, attribute="can_close", flag=True)
    ss.upsert(rid, "Ana", trainee={"target_role": "Bartender", "trainer": "Sam", "until": "12/1/26"})
    c = sr.build_constraints(rid, WEEK, DAYS)
    out = setup.setup_review(c, leader_status={"lines": ["1 leader rule that need a rating is inactive"],
                                               "can_adopt": True})
    kinds = [i["kind"] for i in out["items"]]
    assert kinds[0] == "managers" and out["items"][0]["text"] == "Managers: Erik (Owner)"
    for k in ("managers_unconfirmed", "standing_missing", "closers_share", "close_times_missing",
              "leader_rules_inactive", "trainee", "owner_salaried"):
        assert k in kinds, k
    assert "No close time for Monday, Tuesday, Wednesday, Thursday and Sunday" in " ".join(out["lines"])
    assert "Erik is scheduled as an owner — no overtime line, up to 55h a week" in " ".join(out["lines"])
    import inspect
    assert "setup_review(" in inspect.getsource(schedule_engine._run_schedule_job)

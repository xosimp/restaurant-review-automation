"""Erik's answers, 10/2/26: checks opened after 10pm are "Late night" in the
nightly report, and a login can be left off the nightly report (Danny, a
marketer with a Manager login, should not get the night's sales)."""
import sys

import pytest
from flask import Flask

import models
from models import Restaurant, create_restaurant


@pytest.fixture
def rid(db_path, monkeypatch):
    real = models.get_conn

    def conn(*a, **k):
        return real(db_path)
    for mod in list(sys.modules.values()):
        if mod is not None and getattr(mod, "get_conn", None) is real:
            monkeypatch.setattr(mod, "get_conn", conn)
    monkeypatch.setattr(models, "get_conn", conn)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    import auth
    monkeypatch.setattr(auth, "DB_PATH", db_path)
    auth.init_auth(db_path=db_path)
    return create_restaurant(Restaurant(name="EJ Co", owner_email="e@x.test"), db_path=db_path)


# ── late night ─────────────────────────────────────────────────────────────

def test_checks_opened_from_the_late_hour_or_after_midnight_are_late_night():
    from dsr.block_service import late_night_daypart, _split
    tickets = [
        {"mealtime": "Lunch", "opened_at": "2026-10-01T12:05:00", "net_sales": 40, "guest_count": 2},
        {"mealtime": "Dinner", "opened_at": "2026-10-01T21:59:00", "net_sales": 60, "guest_count": 2},
        {"mealtime": "Dinner", "opened_at": "2026-10-01T22:00:00", "net_sales": 30, "guest_count": 1},
        {"mealtime": "Dinner", "opened_at": "2026-10-02T00:40:00", "net_sales": 20, "guest_count": 1},
    ]
    by = {d["name"]: d for d in _split(tickets, late_night_daypart(22), 150)}
    assert by["Late night"]["net"] == 50 and by["Late night"]["checks"] == 2
    assert by["Dinner"]["net"] == 60 and by["Lunch"]["net"] == 40
    # Off: the POS's own tags, as before.
    assert {d["name"] for d in _split(tickets, late_night_daypart(None), 150)} == {"Lunch", "Dinner"}
    # A check with no readable open time keeps its POS tag.
    assert late_night_daypart(22)({"mealtime": "Dinner", "opened_at": None}) == "Dinner"


def test_the_late_hour_is_a_restaurant_setting_with_its_four_touch_points(rid, db_path):
    models.update_restaurant(rid, {"dsr_late_night_hour": 22}, db_path=db_path)
    assert models.get_restaurant(rid, db_path=db_path).dsr_late_night_hour == 22


def test_the_settings_route_takes_a_late_hour_between_6pm_and_11pm(rid, monkeypatch):
    import strategy_routes
    monkeypatch.setattr(strategy_routes, "_dsr_owner_only", lambda u: None)
    u = {"restaurant_id": rid, "role": "owner", "id": 1}
    app = Flask(__name__)
    for bad in (9, 24, "x", True):
        with app.test_request_context("/x", method="POST", json={"dsr_late_night_hour": bad}):
            assert strategy_routes._do_dsr_settings_set(u)[1] == 400, bad
    with app.test_request_context("/x", method="POST", json={"dsr_late_night_hour": 22}):
        out, code = strategy_routes._do_dsr_settings_set(u)
    assert code == 200 and out["settings"]["dsr_late_night_hour"] == 22
    with app.test_request_context("/x", method="POST", json={"dsr_late_night_hour": None}):
        assert strategy_routes._do_dsr_settings_set(u)[0]["settings"]["dsr_late_night_hour"] is None


# ── who gets the nightly report ────────────────────────────────────────────

def test_a_login_left_off_the_nightly_report_is_not_a_recipient(rid, db_path):
    import auth
    from dsr import deliver
    owner = auth.create_user(rid, "erik", "erik@x.test", "pw-1234567", db_path=db_path)
    auth.set_user_role(owner, "owner", db_path=db_path)
    jim = auth.create_user(rid, "jim", "jim@x.test", "pw-1234567", db_path=db_path)
    danny = auth.create_user(rid, "danny", "danny@x.test", "pw-1234567", db_path=db_path)
    auth.set_user_role(danny, "manager", db_path=db_path)
    assert {u["id"] for u in deliver.recipients(rid, db_path=db_path)} == {owner, jim, danny}
    auth.set_nightly_report_pref(rid, danny, False, db_path=db_path)
    assert {u["id"] for u in deliver.recipients(rid, db_path=db_path)} == {owner, jim}
    access = auth.get_team_access(rid, db_path=db_path)
    assert access[danny]["nightly_report"] is False and access[jim]["nightly_report"] is True
    # The morning brief is its own choice; the report switch never touches it.
    assert access[danny]["morning_brief"] is True
    auth.set_nightly_report_pref(rid, danny, True, db_path=db_path)
    assert danny in {u["id"] for u in deliver.recipients(rid, db_path=db_path)}


def test_the_team_route_and_screen_carry_the_switch():
    import inspect
    import mobile_api
    src = inspect.getsource(mobile_api.mobile_set_team_access)
    assert 'if "nightly_report" in data:' in src and "set_nightly_report_pref(rid, user_id" in src
    assert 'm["nightly_report"] = bool(a.get("nightly_report", True))' in inspect.getsource(mobile_api.mobile_get_team)
    html = open("templates/dashboard.html", encoding="utf-8").read()
    assert "bits.push(sw('Nightly report'," in html and "window.setTeamReport=function(id,box){" in html


def test_the_poss_own_late_night_meal_time_is_the_same_daypart():
    """RPOWER added a "Late Night" meal time at Simple EJ's (Justin, 10/6/26):
    with the late hour on or off, it is one "Late night" row, never two."""
    from dsr.block_service import late_night_daypart, _split
    tickets = [
        {"mealtime": "Late Night", "opened_at": "2026-10-06T22:30:00", "net_sales": 30, "guest_count": 1},
        {"mealtime": "Dinner", "opened_at": "2026-10-06T23:10:00", "net_sales": 20, "guest_count": 1},
    ]
    for hour in (22, None):
        names = [d["name"] for d in _split(tickets, late_night_daypart(hour), 50)]
        assert names.count("Late night") == 1 and "Late Night" not in names, (hour, names)

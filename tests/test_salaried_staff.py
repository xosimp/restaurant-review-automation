"""Salaried staff (owner, 9/28/26): Erik and Jim at $150K each.

Hourly labor stays the headline against the target; the salaries sit beside
it. A salaried person's punches leave hourly labor (their pay is the salary),
each trading day carries annual / 52 / trading days a week, and the figure is
the owner's alone.
"""
import json
import os
from datetime import date, timedelta

import pytest

import models

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = open(os.path.join(ROOT, "templates", "dashboard.html"), encoding="utf-8").read()
SAL = json.dumps([{"name": "Erik Baylis", "annual": 150000}, {"name": "Jim", "annual": 150000}])


@pytest.fixture(autouse=True)
def _redirect(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    yield


def _r(**kw):
    return models.Restaurant(name="Simple EJ's", owner_email="e@x.test", **kw)


def test_the_list_and_a_trading_days_share():
    r = _r(salaried_staff_json=SAL)
    assert models.salaried_staff(r) == [{"name": "Erik Baylis", "annual": 150000.0},
                                        {"name": "Jim", "annual": 150000.0}]
    assert round(models.salaried_day_share(r), 2) == round(300000 / 52 / 7, 2)          # $824.18
    five = _r(salaried_staff_json=SAL, open_times_json=json.dumps(
        {d: "11:00" for d in ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday")}))
    assert round(models.salaried_day_share(five), 2) == round(300000 / 52 / 5, 2)       # a week's pay over 5 days
    assert models.salaried_day_share(_r()) is None
    assert models.salaried_staff(_r(salaried_staff_json='[{"name": "", "annual": 5}, "x", {"name": "A"}]')) == []


def _world(db_path):
    rid = models.create_restaurant(_r(labor_target_pct=35.0, hourly_rate=26.0), db_path=db_path)
    models.update_restaurant(rid, {"salaried_staff_json": SAL}, db_path=db_path)
    today = date.today()
    rows = ["date,day,employee,role,shift_start,shift_end,scheduled_hours,actual_hours,sales,notes,pay_rate"]
    for n in range(1, 8):
        d = (today - timedelta(days=n)).isoformat()
        rows.append(f"{d},X,{'Amy' if n % 2 else 'Bob'} Cook,Kitchen,08:00,16:00,8,8,5000,,20")   # no overtime
        rows.append(f"{d},X,Erik Baylis,Manager FOH,10:00,14:00,4,4,5000,,")
    models.save_client_data(rid, "shifts", "\n".join(rows) + "\n", source="rpower", db_path=db_path)
    return rid


def test_a_salaried_persons_punches_leave_hourly_labor(db_path):
    import labor
    rid = _world(db_path)
    a = labor.analyse_shifts_for_restaurant(rid)
    assert a["costed_labor"] == 7 * 8 * 20.0                     # the cooks only: Erik's 28h are not $26 an hour
    assert a["salaried_hours_left_out"] == 28.0
    assert "Erik Baylis" not in a["employee_hours"]
    s = labor.salaried_summary(models.get_restaurant(rid), a)
    assert s["days"] == 7 and abs(s["cost"] - 300000 / 52) < 0.05     # seven trading days = one week's salary
    assert s["total_cost"] == round(1120.0 + s["cost"], 2)
    assert s["total_pct"] == round(s["total_cost"] / 35000 * 100, 1)
    assert a["overall_labor_pct"] == 3.2                           # the headline stays hourly


def test_the_nights_report_carries_the_share_for_the_owner_only():
    import dsr
    from dsr import access
    r = _r(salaried_staff_json=SAL)
    facts = {"blocks": {"sales": dsr.block(dsr.READY, source="rpower", metrics={"net": 10000.0}),
                        "labor": dsr.block(dsr.READY, source="rpower",
                                           metrics={"cost": 3000.0, "pct": 30.0, "target_pct": 35.0})}}
    out = access._live_salaries(facts, r)
    m = out["blocks"]["labor"]["metrics"]
    assert m["pct"] == 30.0                                        # hourly, untouched
    assert m["salaried_cost"] == round(300000 / 52 / 7, 2)
    assert m["salaried_total_pct"] == round((3000 + 300000 / 52 / 7) / 10000 * 100, 1)
    assert not access.line_allowed({}, access.MANAGER, "salaried_total_pct")
    assert access.line_allowed({}, access.OWNER, "salaried_total_pct")
    # hourly $ withheld (no wage entered): no salaries line either
    facts["blocks"]["labor"]["metrics"]["cost"] = None
    assert "salaried_cost" not in access._live_salaries(facts, r)["blocks"]["labor"]["metrics"]


def test_settings_adds_and_removes_one_person_at_a_time(db_path):
    import strategy_routes as sr
    rid = models.create_restaurant(_r(), db_path=db_path)
    u = {"id": 1, "restaurant_id": rid, "role": "owner"}
    orig = sr._body
    try:
        sr._body = lambda: {"salaried_add": {"name": "  Erik   Baylis ", "annual": 150000}}
        out, code = sr._do_targets_set(u)
        assert code == 200 and out["targets"]["salaried"][0]["name"] == "Erik Baylis"
        sr._body = lambda: {"salaried_add": {"name": "Jim", "annual": 150000}}
        sr._do_targets_set(u)
        sr._body = lambda: {"salaried_remove": "erik baylis"}
        out, _ = sr._do_targets_set(u)
        assert [x["name"] for x in out["targets"]["salaried"]] == ["Jim"]
        sr._body = lambda: {"salaried_add": {"name": "Jim", "annual": 12}}
        assert sr._do_targets_set(u)[1] == 400
    finally:
        sr._body = orig
    mgr = {"id": 2, "restaurant_id": rid, "role": "manager"}
    got, _ = sr._do_targets_get(mgr)
    if got.get("targets"):
        assert got["targets"]["salaried"] == []                   # a manager never reads salaries


def test_the_hero_and_the_report_show_it_beside_never_in_it():
    assert "{{ 'Hourly labor' if labor_salaried else 'Labor' }}" in SRC
    assert '<span class="l">With salaries</span>' in SRC
    assert "if(isNum(m.salaried_total_pct))t+=tile('With salaries'" in SRC
    assert 'data-tg-sal-rm="' in SRC and "salaried_add: {name: n.value, annual: +a.value}" in SRC


def test_rpowers_system_manager_login_is_a_station_not_a_manager():
    import rpower
    assert rpower.is_station_name("System Manager")
    assert not rpower.is_station_name("Andrew Marola")

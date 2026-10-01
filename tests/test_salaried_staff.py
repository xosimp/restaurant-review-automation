"""Salaried staff (owner, 9/28/26): Erik and Jim at $150K each.

Hourly labor stays the headline against the target; the salaries sit beside
it. A salaried person's punches leave hourly labor (their pay is the salary),
each trading day carries annual / 52 / trading days a week, and the figure is
the owner's alone.
"""
import inspect
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
    assert a["hourly_costed_labor"] == 7 * 8 * 20.0              # the cooks only: Erik's 28h are not $26 an hour
    assert a["salaried_hours_left_out"] == 28.0
    assert "Erik Baylis" not in a["employee_hours"]
    s = labor.salaried_summary(models.get_restaurant(rid), a)
    assert s["days"] == 7 and abs(s["cost"] - 300000 / 52) < 0.05     # seven trading days = one week's salary
    assert s["total_cost"] == round(1120.0 + s["cost"], 2)
    assert s["total_pct"] == round(s["total_cost"] / 35000 * 100, 1)
    # Labor % is all-in and the target judges it (owner, 9/30/26: "Erik only
    # cares about that labor %, it's the real %"); the shifts alone beside it.
    assert a["includes_salaries"] and a["overall_labor_pct"] == s["total_pct"]
    assert abs(a["costed_labor"] - s["total_cost"]) < 0.05 and a["hourly_labor_pct"] == 3.2
    # The schedule's hourly budget reads the shifts alone.
    assert labor.analyse_shifts_for_restaurant(rid, with_salaries=False)["overall_labor_pct"] == 3.2


def test_a_manager_never_reads_the_salaries_in_a_labor_figure(db_path):
    """Salaries are the owner's (dsr OWNER_ONLY_PREFIXES): an all-in % beside
    the hourly one would give them away, so a manager's read stays hourly."""
    import labor
    from flask import Flask, g
    rid = _world(db_path)
    mgr = {"id": 2, "restaurant_id": rid, "role": "manager", "is_admin": False}
    with Flask(__name__).test_request_context("/"):
        g.cavnar_current_user = mgr
        a = labor.analyse_shifts_for_restaurant(rid)
        assert not a["includes_salaries"] and a["overall_labor_pct"] == 3.2
        assert not models.viewer_sees_salaries()


def test_the_nights_report_carries_the_share_for_the_owner_only():
    import dsr
    from dsr import access
    r = _r(salaried_staff_json=SAL)
    facts = {"blocks": {"sales": dsr.block(dsr.READY, source="rpower", metrics={"net": 10000.0}),
                        "labor": dsr.block(dsr.READY, source="rpower",
                                           metrics={"cost": 3000.0, "pct": 30.0, "target_pct": 35.0})}}
    out = access._live_salaries(facts, r)
    m = out["blocks"]["labor"]["metrics"]
    assert m["salaried_cost"] == round(300000 / 52 / 7, 2)
    assert m["salaried_total_pct"] == round((3000 + 300000 / 52 / 7) / 10000 * 100, 1)
    # the owner's labor % is all-in and judged; the shifts alone beside it
    assert m["pct"] == m["salaried_total_pct"] and m["hourly_pct"] == 30.0 and m["includes_salaries"]
    assert m["vs_target_pts"] == round(m["pct"] - 35.0, 1)
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


def test_the_hero_and_the_report_lead_with_labor_with_salaries():
    assert "{{ 'Labor with salaries' if labor.includes_salaries else 'Labor' }}" in SRC
    assert '<span class="l">Hourly only</span>' in SRC
    assert "t+=tile(m.includes_salaries?'Labor with salaries':'Labor'" in SRC
    assert "if(m.includes_salaries&&isNum(m.hourly_pct))t+=tile('Hourly only'" in SRC
    assert 'data-tg-sal-rm="' in SRC and "salaried_add: {name: n.value, annual: +a.value}" in SRC


def test_rpowers_system_manager_login_is_a_station_not_a_manager():
    import rpower
    assert rpower.is_station_name("System Manager")
    assert not rpower.is_station_name("Andrew Marola")


def test_the_report_summary_states_labor_with_salaries_for_the_owner_only():
    """Owner, 9/30/26: "Erik is going to want to see labor with salaries
    included in the report - that's his actual labor." The night's labor
    block carries the salaries-in figures (owner-only by name), the writer is
    told to state labor with them, and a manager's copy drops every line that
    cites them or names salaries."""
    import dsr
    from dsr import access, narrative
    facts = {"blocks": {"sales": dsr.block(dsr.READY, source="rpower", metrics={"net": 10000.0}),
                        "labor": dsr.block(dsr.READY, source="rpower", metrics={
                            "cost": 3000.0, "pct": 30.0, "target_pct": 35.0, "salaried_cost": 824.18,
                            "salaried_total_cost": 3824.18, "salaried_total_pct": 38.2,
                            "salaried_vs_target_pts": 3.2})}}
    story = {"executive_summary": {"text": "Labor with salaries ran 38.2%, 3.2 points over target.",
                                   "cites": ["labor.salaried_total_pct", "labor.salaried_vs_target_pts"]},
             "operations_summary": {"text": "Labor ran 30% of sales.", "cites": ["labor.pct"]},
             "went_well": [{"text": "Salaries held steady.", "cites": ["sales.net"]}]}
    mgr = {"id": 2, "role": "manager", "is_admin": False}
    _, hidden = access.redact(facts, mgr)
    assert {"labor.salaried_total_pct", "labor.salaried_vs_target_pts"} <= hidden
    n = access.narrative_for(story, hidden)
    assert "38.2" not in str(n) and "alar" not in str(n)
    assert narrative.owner_only_cite("labor.salaried_total_pct")
    assert narrative._OWNER_TOPIC_RE.search("salaries held")
    assert "state labor with labor.salaried_total_pct" in inspect.getsource(narrative)
    src = inspect.getsource(__import__("dsr.block_labor", fromlist=["x"]))
    assert '"salaried_total_pct": all_in' in src


def test_a_role_whose_people_are_all_salaried_has_no_hourly_rate_to_set(db_path, monkeypatch):
    """Owner, 9/30/26: Owner and Manager FOH sat in Targets & pay rates with a
    $26 box nobody's pay uses. A role whose every person is salaried is left
    out of the rates and named once; a manager never sees which it is."""
    import staff_settings
    import strategy_routes as sr
    rid = models.create_restaurant(_r(), db_path=db_path)
    models.update_restaurant(rid, {"salaried_staff_json": SAL}, db_path=db_path)
    monkeypatch.setattr(staff_settings, "roster", lambda *a, **k: [
        {"name": "Erik Baylis", "role": "Owner"}, {"name": "Ana B.", "role": "Server AM"}])
    owner = {"id": 1, "restaurant_id": rid, "role": "owner"}
    got, _ = sr._do_targets_get(owner)
    assert got["targets"]["roles"] == ["Server AM"] and got["targets"]["salaried_roles"] == ["Owner"]
    mgr = {"id": 2, "restaurant_id": rid, "role": "manager"}
    got, code = sr._do_targets_get(mgr)
    if code == 200:
        assert got["targets"]["salaried_roles"] == []
    assert "t.salaried_roles" in SRC


def test_one_person_can_have_their_own_rate_under_their_roles(db_path, monkeypatch):
    """Owner, 9/30/26: most hosts and cooks make the same, a few make more.
    A shift costs the POS's own pay first, then the person's own rate, then
    the role's; the rate saves one person at a time and blank clears it."""
    import labor
    import staff_settings
    import strategy_routes as sr
    rates = {"Host AM": 15.0, "_default": 26.0}
    own = {"kailey gordon": 17.5}
    assert labor._shift_rate({"employee": "Kailey  Gordon", "role": "Host AM"}, rates, 26.0, own) == 17.5
    assert labor._shift_rate({"employee": "Ana B.", "role": "Host AM"}, rates, 26.0, own) == 15.0
    assert labor._shift_rate({"employee": "Kailey Gordon", "role": "Host AM", "pay_rate": "16"}, rates, 26.0, own) == 16.0

    rid = models.create_restaurant(_r(), db_path=db_path)
    monkeypatch.setattr(staff_settings, "roster", lambda *a, **k: [
        {"name": "Kailey Gordon", "role": "Host AM"}, {"name": "Ana B.", "role": "Host AM"}])
    owner = {"id": 1, "restaurant_id": rid, "role": "owner"}
    orig = sr._body
    try:
        sr._body = lambda: {"person_rate": {"name": "Kailey Gordon", "rate": 17.5}}
        out, code = sr._do_targets_set(owner)
        assert code == 200 and models.person_rates(models.get_restaurant(rid)) == {"kailey gordon": 17.5}
        ppl = out["targets"]["people_by_role"]["Host AM"]
        assert [(p["name"], p["rate"]) for p in ppl] == [("Ana B.", None), ("Kailey Gordon", 17.5)]
        sr._body = lambda: {"person_rate": {"name": "Kailey Gordon", "rate": 400}}
        assert sr._do_targets_set(owner)[1] == 400
        sr._body = lambda: {"person_rate": {"name": "kailey gordon", "rate": ""}}
        sr._do_targets_set(owner)
        assert models.person_rates(models.get_restaurant(rid)) == {}
    finally:
        sr._body = orig
    assert "person_rates_json" in models.PAY_FIELDS
    assert "data-tg-person" in SRC and "person_rate: {name: person" in SRC

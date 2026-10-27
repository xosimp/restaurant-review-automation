"""One labor figure per viewer, on every surface (audit #25 F2 / R7, A04-08).

Labor % was computed in fifteen places, each adding the salaries its own
way: the stored labor_daily_history.labor_pct is HOURLY, the owner's labor %
is ALL-IN (salaries included, owner 9/30/26) and a manager's stays hourly.
Three salary-view drifts shipped in October. This seeds ONE restaurant —
seven days of shifts with one salaried manager, the daily archive written
by the real writer, and the nightly report's nights for the same days —
and asserts that the Labor analysis, metrics, the nightly report (each
night and the week), the month-to-date prime cost's labor and Ask's
recent-days line all show the same all-in figure to the owner and the same
hourly figure to a manager.

The days (Thu 10/1/26 - Wed 10/7/26, America/Chicago): Amy 8h and Bob 4-8h
a day at $20 (no overtime on either workweek), Mia salaried at $36,400 a
year = $100 a trading day (7 trading days). Sales $20,000; hourly $1,900
(9.5%); all-in $2,600 (13.0%).
"""
import json
import os
import sys
from datetime import date, timedelta

import pytest
from flask import Flask, g

import cogs
import food_cost_intelligence as fci
import models
from models import Restaurant, create_restaurant, get_restaurant, update_restaurant

# The deploy gate's contract slice (TESTING.md, Tiers; blind re-audit 10/27/26).
pytestmark = pytest.mark.contract

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIRST = date(2026, 10, 1)                 # a Thursday
DAYS = [FIRST + timedelta(days=i) for i in range(7)]
SALES = [2500.0, 3000.0, 4000.0, 3500.0, 2000.0, 2500.0, 2500.0]
BOB_HOURS = [5, 6, 8, 7, 4, 5, 4]
HOURLY = [8 * 20.0 + h * 20.0 for h in BOB_HOURS]          # 260, 280, 320, 300, 240, 260, 240
SHARE = 100.0                                              # 36,400 / 52 / 7
TODAY = date(2026, 10, 8)                                  # the profitability card's "today"

OWNER_PCT, MANAGER_PCT = 13.0, 9.5


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
    yield


def _world(db_path):
    rid = create_restaurant(Restaurant(name="One Figure Co", owner_email="one@x.test", timezone="America/Chicago",
                                       labor_target_pct=30.0, hourly_rate=20.0), db_path=db_path)
    update_restaurant(rid, {"salaried_staff_json": json.dumps([{"name": "Mia Manager", "annual": 36400}]),
                            "hourly_rate_source": "set", "fiscal_week_start_dow": 3}, db_path=db_path)
    rows = ["date,day,employee,role,shift_start,shift_end,scheduled_hours,actual_hours,sales,notes,pay_rate"]
    for d, s, bob in zip(DAYS, SALES, BOB_HOURS):
        wd = d.strftime("%A")
        rows.append(f"{d.isoformat()},{wd},Amy Cook,Kitchen,08:00,16:00,8,8,{s:g},,20")
        rows.append(f"{d.isoformat()},{wd},Bob Cook,Kitchen,16:00,{16 + bob}:00,{bob},{bob},{s:g},,20")
        rows.append(f"{d.isoformat()},{wd},Mia Manager,Manager FOH,10:00,14:00,4,4,{s:g},,")
    models.save_client_data(rid, "shifts", "\n".join(rows) + "\n", db_path=db_path)
    # The daily archive by the real writer, from the hourly per-day analysis.
    import labor
    models.save_labor_daily_history(rid, labor.full_history_by_day(rid), db_path=db_path)
    _nights(db_path, rid)
    return rid


def _nights(db_path, rid):
    """The nightly report for each day, as dsr.block_labor stores it: labor
    cost HOURLY, the salaries apart."""
    import dsr
    from dsr import store
    r = get_restaurant(rid, db_path=db_path)
    share = models.salaried_day_share(r)
    for d, s, h in zip(DAYS, SALES, HOURLY):
        rep = store.create_report(rid, d, trigger="sweep", db_path=db_path)
        store.save_block(rep["id"], "sales", dsr.block(dsr.READY, source="fakepos", block_name="sales",
                                                       metrics={"net": s, "gross": s + 100}), db_path=db_path)
        store.save_block(rep["id"], "labor", dsr.block(dsr.READY, source="fakepos", block_name="labor", metrics={
            "cost": h, "pct": round(h / s * 100, 1), "target_pct": 30.0, "hours": 16.0,
            "salaried_cost": round(share, 2), "salaried_total_cost": round(h + share, 2),
            "salaried_total_pct": round((h + share) / s * 100, 1)}), db_path=db_path)
        store.set_stage(rep["id"], "final", db_path=db_path)


def _as(user):
    app = Flask(__name__)
    ctx = app.test_request_context("/")
    ctx.push()
    g.cavnar_current_user = user
    return ctx


def _owner(rid):
    return {"id": 1, "restaurant_id": rid, "role": "client", "is_admin": 0}


def _manager(rid):
    return {"id": 2, "restaurant_id": rid, "role": "manager", "is_admin": 0}


def _profitability(monkeypatch, rid):
    monkeypatch.setattr(fci, "_local_today", lambda r: TODAY)
    monkeypatch.setattr(fci, "forecast_accuracy", lambda *a, **k: None)
    monkeypatch.setattr(cogs, "net_sales_in_window", lambda r, s, e: (sum(SALES), None))
    monkeypatch.setattr(cogs, "build_food_cost_pct", lambda r, days=28, db_path=None, today=None: {
        "ok": True, "cogs": 6000.0, "pct": 30.0, "target": 28.0, "window_days": days,
        "start": (today - timedelta(days=days - 1)).isoformat(), "end": today.isoformat()})
    monkeypatch.setattr(fci, "_labor_stale_why", lambda *a, **k: None)
    return fci.profitability_projection(rid)


def _figures_for(user, rid, monkeypatch):
    """Every surface's labor % for the week and for the newest night, read
    as `user`."""
    import ask_cavnar
    import labor
    import metrics
    from dsr import access, rollup, store
    r = get_restaurant(rid)
    ctx = _as(user)
    try:
        week = {
            "analysis": labor.analyse_shifts_for_restaurant(rid)["overall_labor_pct"],
            "metrics": metrics.measure(rid, "labor_pct", DAYS[0].isoformat(), DAYS[-1].isoformat())[0],
            "labor_figures": metrics.labor_figures(rid, DAYS[0], DAYS[-1])["pct"],
            "prime_labor_pct_mtd": _profitability(monkeypatch, rid)["labor_pct_mtd"],
        }
        totals = access.grid_for(rollup.week(r, DAYS[0]), user)["totals"]
        week["dsr_week"] = totals.get("salaried_total_pct") if access.view_for(user) == access.OWNER \
            else totals.get("labor_pct")
        nights = {}
        for d in DAYS:
            rep = store.get_report(rid, d)
            shown = access.render(rep, user, r)
            kpi = next((k for k in shown["kpis"] if k["key"] == "labor_pct"), None)
            nights[d.isoformat()] = {
                "dsr_block": shown["facts"]["blocks"]["labor"]["metrics"]["pct"],
                "dsr_kpi": kpi["value"] if kpi else None,
                "metrics": metrics.measure(rid, "labor_pct", d.isoformat(), d.isoformat())[0],
            }
        ask = ask_cavnar._recent_days_lines(rid)
        daily = {x["date"]: x for x in metrics.daily_labor(rid, days=14)}
    finally:
        ctx.pop()
    return week, nights, ask, daily


def test_every_surface_shows_the_owner_one_all_in_figure(db_path, monkeypatch):
    rid = _world(db_path)
    week, nights, ask, daily = _figures_for(_owner(rid), rid, monkeypatch)
    assert week == {k: OWNER_PCT for k in week}, week
    for d, s, h in zip(DAYS, SALES, HOURLY):
        want = round((h + SHARE) / s * 100, 1)
        assert nights[d.isoformat()] == {"dsr_block": want, "dsr_kpi": want, "metrics": want}, d
        assert daily[d.isoformat()]["labor_pct"] == want and daily[d.isoformat()]["includes_salaries"]
        assert daily[d.isoformat()]["hourly_labor_pct"] == round(h / s * 100, 1)
    # Ask's last three days: the same all-in figures, said as such, M/D/YY.
    text = "\n".join(ask)
    assert "salaries included" in text
    for d, s, h in list(zip(DAYS, SALES, HOURLY))[-3:]:
        assert f"{round((h + SHARE) / s * 100, 1):.1f}% labor with salaries" in text
        assert f"{d.month}/{d.day}/{d.strftime('%y')}" in text
    assert "2026-10-0" not in text, "an ISO date in owner-facing text is a bug"


def test_every_surface_shows_a_manager_one_hourly_figure_and_no_salary(db_path, monkeypatch):
    rid = _world(db_path)
    week, nights, ask, daily = _figures_for(_manager(rid), rid, monkeypatch)
    assert week == {k: MANAGER_PCT for k in week}, week
    for d, s, h in zip(DAYS, SALES, HOURLY):
        want = round(h / s * 100, 1)
        assert nights[d.isoformat()] == {"dsr_block": want, "dsr_kpi": want, "metrics": want}, d
        row = daily[d.isoformat()]
        assert row["labor_pct"] == want and not row["includes_salaries"] and "salaried_cost" not in row
    text = "\n".join(ask)
    assert "salaries" not in text.replace("salaries are not in it", "")
    assert "hourly staff only" in text
    for d, s, h in list(zip(DAYS, SALES, HOURLY))[-3:]:
        assert f"{round(h / s * 100, 1):.1f}% labor," in text


def test_labor_figures_says_its_basis_and_never_hands_a_manager_a_salary(db_path):
    import metrics
    rid = _world(db_path)
    owner = metrics.labor_figures(rid, DAYS[0], DAYS[-1], view="owner")
    assert (owner["hourly_cost"], owner["salaried_cost"], owner["net"]) == (1900.0, 700.0, 20000.0)
    assert (owner["hourly_pct"], owner["all_in_pct"], owner["pct"]) == (9.5, 13.0, 13.0)
    assert owner["includes_salaries"] and owner["basis"] == "all_in" and owner["days"] == 7
    mgr = metrics.labor_figures(rid, DAYS[0], DAYS[-1], view="manager")
    assert mgr["salaried_cost"] is None and mgr["all_in_pct"] is None
    assert (mgr["pct"], mgr["cost"], mgr["basis"]) == (9.5, 1900.0, "hourly")
    assert all(day["salaried_cost"] is None for day in mgr["by_day"].values())
    # Nobody salaried: the owner's figure is every wage, and says so.
    update_restaurant(rid, {"salaried_staff_json": "[]"})
    none = metrics.labor_figures(rid, DAYS[0], DAYS[-1], view="owner")
    assert none["pct"] == 9.5 and none["basis"] == "no_salaries" and not none["includes_salaries"]
    # Nothing to divide: unknown, never 0%.
    empty = metrics.labor_figures(rid, date(2025, 1, 1), date(2025, 1, 7), view="owner")
    assert empty["pct"] is None and empty["days"] == 0


# ── the ratchet: labor ÷ sales lives in metrics, labor and dsr ──────────────

def _ratchet():
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    import check_labor_math
    return check_labor_math


def test_no_new_labor_over_sales_arithmetic_outside_the_owners():
    c = _ratchet()
    over = c.compare(c.scan(), c.load_baseline())
    assert not over, ("new labor-cost/sales arithmetic outside metrics.py, labor.py and dsr/ — read it through "
                      f"metrics.labor_figures, or raise scripts/labor_math_baseline.json with the reason: {over}")
    assert all((v or {}).get("why") and not str(v["why"]).startswith("TODO")
               for v in c.load_baseline().values()), "every grandfathered file says why"


def test_the_ratchet_sees_labor_over_sales_code_and_skips_prose():
    c = _ratchet()
    src = ('def f(r):\n'
           '    """labor / sales in a docstring is prose."""\n'
           '    # labor_cost / sales in a comment too\n'
           '    pct = r["labor_cost"] / r["sales"] * 100\n'
           '    sql = "SELECT SUM(labor_pct * sales) FROM labor_daily_history"\n'
           '    x = "UPDATE h SET p=ROUND(labor_cost * 100.0 / ?, 1)"\n'
           '    y = total / count\n')
    assert c.count_text(src) == 3
    assert c.compare({"a.py": 3}, {"a.py": {"n": 2, "why": "w"}}) == [("a.py", 3, 2)]
    assert c.compare({"a.py": 2}, {"a.py": {"n": 3, "why": "w"}}) == []

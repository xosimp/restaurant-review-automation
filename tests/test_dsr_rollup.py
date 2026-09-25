"""dsr.rollup — Erik's week and period, from the nightly reports.

Simple EJ's calendar: weeks Wed → Tue, Period 1 starting Wed 1/14/26, so
Wed 9/16/26 – Tue 9/22/26 is Period 9 · Week 4.
"""
import json
import sys
from datetime import date

import pytest
from flask import Flask

import auth
import models
import pos
import dsr
from dsr import access, rollup, store
from models import Restaurant, create_restaurant, update_restaurant, get_restaurant

WED = date(2026, 9, 16)
TUE = date(2026, 9, 22)


@pytest.fixture
def db(db_path, monkeypatch):
    real = models.get_conn

    def conn(*a, **k):
        return real(db_path)
    for mod in list(sys.modules.values()):
        if mod is not None and getattr(mod, "get_conn", None) is real:
            monkeypatch.setattr(mod, "get_conn", conn)
    monkeypatch.setattr(models, "get_conn", conn)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(auth, "DB_PATH", db_path)
    monkeypatch.setattr(pos, "PROVIDERS", None)
    return db_path


def _ejs(db):
    rid = create_restaurant(Restaurant(name="Simple EJ's", owner_email="e@x.com", timezone="America/Chicago"),
                            db_path=db)
    update_restaurant(rid, {"fiscal_week_start_dow": 2, "fiscal_year_start": "2026-01-14",
                            "fiscal_period_scheme": "4x13"}, db_path=db)
    return get_restaurant(rid, db_path=db)


def _night(db, rid, day, net, gross, cats, labor_cost=None, influence=None, event=None, weather=None):
    r = store.create_report(rid, day, trigger="sweep", db_path=db)
    store.save_block(r["id"], "sales", dsr.block(dsr.READY, source="rpower", metrics=dict(
        {"net": net, "gross": gross}, **{f"cat:{k}": v for k, v in cats.items()})), db_path=db)
    if labor_cost is not None:
        store.save_block(r["id"], "labor", dsr.block(dsr.READY, source="rpower",
                                                     metrics={"cost": labor_cost, "pct": round(labor_cost / net * 100, 1)}),
                         db_path=db)
    store.save_block(r["id"], "intel", dsr.block(dsr.READY, source="cavnar", metrics={},
                                                 detail={"weather": {"summary": weather},
                                                         "events": {"summary": event or "Nothing listed"}}), db_path=db)
    store.save_block(r["id"], "closeout", dsr.block(dsr.READY, source="manager", metrics={"filed": 1},
                                                    detail={"fields": {"influence": influence}}), db_path=db)
    store.set_stage(r["id"], "collecting", db_path=db)
    store.set_stage(r["id"], "final", db_path=db)
    return r


def test_a_week_is_erik_s_grid(db):
    r = _ejs(db)
    _night(db, r.id, WED, 5000.0, 5300.0, {"Food": 3500.0, "Liquor": 900.0, "Beer": 600.0}, labor_cost=1400.0,
           weather="Sunny · high 74°", event="Cubs home game", influence="Game night pulled a late rush")
    _night(db, r.id, date(2026, 9, 17), 6000.0, 6400.0, {"Food": 4200.0, "Wine": 800.0, "Kiosk": 1000.0},
           labor_cost=1500.0)
    store.set_budget(r.id, WED, gross=5500, net=5200, db_path=db)
    store.set_budget(r.id, date(2026, 9, 17), gross=6000, net=5800, db_path=db)
    store.set_budget(r.id, TUE, gross=7800, net=7300, db_path=db)          # a budget for a night not yet run

    w = rollup.week(r, date(2026, 9, 19))
    assert (w["start"], w["end"], w["label"]) == ("2026-09-16", "2026-09-22", "Period 9 · Week 4")
    assert [d["label"] for d in w["days"]] == ["Wed 9/16/26", "Thu 9/17/26", "Fri 9/18/26", "Sat 9/19/26",
                                              "Sun 9/20/26", "Mon 9/21/26", "Tue 9/22/26"]
    # Erik's six first, anything else he has not placed after them.
    assert w["categories"] == ["Food", "Liquor", "Beer", "Wine", "Retail", "NA Beverage", "Kiosk"]
    wed, thu, tue = w["days"][0], w["days"][1], w["days"][6]
    assert (wed["net"], wed["gross"], wed["cats"]["Food"], wed["labor_pct"]) == (5000.0, 5300.0, 3500.0, 28.0)
    assert (wed["weather"], wed["event"], wed["influence"]) == ("Sunny · high 74°", "Cubs home game",
                                                                "Game night pulled a late rush")
    assert thu["event"] is None                              # "Nothing listed" is no event
    assert (wed["vs_budget_net"], wed["vs_budget_net_pct"]) == (-200.0, -3.8)
    # A night with no report: its budget, and nothing measured — never a zero.
    assert tue["status"] is None and tue["net"] is None and tue["budget_net"] == 7300.0 and tue["vs_budget_net"] is None
    t = w["totals"]
    assert (t["net"], t["gross"], t["days_measured"]) == (11000.0, 11700.0, 2)
    assert t["cats"]["Food"] == 7700.0 and t["cats"]["Retail"] is None
    # vs budget only over the nights that have both: 11,000 against 11,000, not against 18,300.
    assert (t["vs_budget_net"], t["vs_budget_net_pct"]) == (0.0, 0.0)
    # ...and the budget total beside it covers the same nights, so the row
    # reads net - budget == vs budget (NS3 H6); the week's whole budget is
    # still there, named for what it is.
    assert t["budget_net"] == 11000.0 and t["net"] - t["budget_net"] == t["vs_budget_net"]
    assert t["budget_net_full_range"] == 18300.0
    assert t["labor_pct"] == round(2900 / 11000 * 100, 1)
    ptd = w["period_to_date"]
    assert (ptd["start"], ptd["end"], ptd["net"]) == ("2026-08-26", "2026-09-22", 11000.0)


def test_last_year_is_the_dsr_then_the_import_then_the_pos_sync(db, monkeypatch):
    import pos
    # RPOWER's daily total is built as the DSR's net (store.POS_SYNC_SAME_BASIS).
    monkeypatch.setattr(pos, "connected_provider", lambda rid: ("rpower", object()))
    r = _ejs(db)
    _night(db, r.id, WED, 5000.0, 5300.0, {"Food": 5000.0})
    _night(db, r.id, date(2025, 9, 17), 4600.0, 4800.0, {"Food": 4600.0})      # a DSR from last year
    store.import_history(r.id, [{"date": "2025-09-17", "net": 9999.0},             # the DSR wins over the import
                                {"date": "2025-09-18", "gross": 5100.0, "net": 4800.0},
                                {"date": "2025-09-19", "gross": None, "net": None}],  # empty cells: skipped
                         source_file="EJ DSR 2025.xlsx", db_path=db)
    conn = models.get_conn(db)
    conn.execute("INSERT INTO labor_daily_history (restaurant_id, date, day_of_week, labor_pct, labor_cost, sales, "
                 "total_hours) VALUES (?,?,?,?,?,?,?)", (r.id, "2025-09-19", "Friday", 25.0, 1000.0, 4000.0, 60.0))
    conn.execute("INSERT INTO labor_daily_history (restaurant_id, date, day_of_week, labor_pct, labor_cost, sales, "
                 "total_hours) VALUES (?,?,?,?,?,?,?)", (r.id, "2025-09-20", "Saturday", None, 0.0, 0.0, 0.0))
    conn.commit()
    conn.close()
    w = rollup.week(r, WED)
    ly = [(d["last_year_net"], d["last_year_source"]) for d in w["days"][:5]]
    assert ly == [(4600.0, "dsr"), (4800.0, "import"), (4000.0, "pos_sync"), (None, None), (None, None)]
    assert w["days"][0]["vs_last_year_net_pct"] == round(400 / 4600 * 100, 1)
    # A POS whose daily total is another figure (Toast's businessDay
    # netSales) is never set beside the DSR's net as last year (D1-13).
    monkeypatch.setattr(pos, "connected_provider", lambda rid: ("toast", object()))
    ly = [(d["last_year_net"], d["last_year_source"]) for d in rollup.week(r, WED)["days"][:3]]
    assert ly == [(4600.0, "dsr"), (4800.0, "import"), (None, None)]


def test_the_nightly_report_and_the_grid_read_last_year_the_same_way(db, monkeypatch):
    # D1-10: the imported workbook fills the nightly report's "vs last year"
    # exactly as it fills the grid's Last Year column.
    import pos
    monkeypatch.setattr(pos, "connected_provider", lambda rid: ("rpower", object()))
    r = _ejs(db)
    store.import_history(r.id, [{"date": "2025-09-17", "gross": 5100.0, "net": 4800.0}], db_path=db)
    from dsr import block_sales
    ctx = dsr.Context(r, WED, db_path=db)
    assert block_sales._baseline_net(ctx, store.last_year_day(r, WED), "rpower") == (4800.0, "import")
    _night(db, r.id, WED, 5000.0, 5300.0, {"Food": 5000.0})
    assert rollup.week(r, WED)["days"][0]["last_year_net"] == 4800.0


def test_last_year_follows_the_fiscal_calendar_after_a_53_week_year():
    # D1-16: FY2026 (from Wed 12/31/25) is listed with 53 weeks, so FY2027
    # starts Wed 1/6/27. Its week 1 Wednesday compares with FY2026's week 1
    # Wednesday — 371 days back, not 364.
    from datetime import timedelta
    import json
    from dsr import fiscal
    r = Restaurant(name="x", owner_email="x@x.com")
    r.fiscal_week_start_dow = 2
    r.fiscal_year_start = "2025-12-31"
    r.fiscal_period_scheme = "445"
    r.fiscal_years_json = json.dumps([{"start": "2025-12-31", "lengths": [4, 4, 5, 4, 4, 5, 4, 4, 5, 4, 5, 5]}])
    day = date(2027, 1, 6)
    assert fiscal.position(r, day)["week"] == 1 and fiscal.position(r, day)["period"] == 1
    assert store.last_year_day(r, day) == date(2025, 12, 31) == day - timedelta(days=371)
    # Week 53 of the long year has no twin last year: no last year, never
    # this year's own first week.
    assert store.last_year_day(r, date(2026, 12, 30)) is None
    # No fiscal calendar: the same weekday 364 days back.
    plain = Restaurant(name="y", owner_email="y@x.com")
    assert store.last_year_day(plain, day) == day - timedelta(days=364)


def test_a_period_is_its_weeks(db):
    r = _ejs(db)
    _night(db, r.id, date(2026, 8, 26), 4000.0, 4200.0, {"Food": 4000.0})
    _night(db, r.id, WED, 5000.0, 5300.0, {"Food": 5000.0})
    p = rollup.period(r, WED)
    assert (p["label"], p["start"], p["end"]) == ("Period 9", "2026-08-26", "2026-09-22")
    assert [w["label"] for w in p["weeks"]] == ["Period 9 · Week 1", "Period 9 · Week 2", "Period 9 · Week 3",
                                               "Period 9 · Week 4"]
    assert [w["totals"]["net"] for w in p["weeks"]] == [4000.0, None, None, 5000.0]
    assert p["totals"]["net"] == 9000.0
    update_restaurant(r.id, {"fiscal_year_start": ""}, db_path=db)
    assert rollup.period(get_restaurant(r.id, db_path=db), WED) is None


def test_a_manager_s_grid_has_no_budget(db):
    r = _ejs(db)
    _night(db, r.id, WED, 5000.0, 5300.0, {"Food": 5000.0})
    store.set_budget(r.id, WED, gross=5500, net=5200, db_path=db)
    w = rollup.week(r, WED)
    mgr = access.redact_grid(w, {"role": "manager"})
    assert not [k for d in mgr["days"] for k in d if k.startswith(("budget", "vs_budget"))]
    assert not [k for k in mgr["totals"] if k.startswith(("budget", "vs_budget"))]
    # Gross goes too without LOSS_VIEW (D2-2): beside net it is the comps.
    assert mgr["withheld"] == ["budget", "gross"] and mgr["days"][0]["net"] == 5000.0
    assert not [k for d in mgr["days"] for k in d if k.startswith("gross")]
    assert access.redact_grid(w, {"role": "client"}) is w
    assert w["days"][0]["budget_net"] == 5200.0                       # the owner's copy is untouched


@pytest.fixture
def client(db):
    from strategy_routes import strategy_bp, strategy_mobile_bp
    app = Flask(__name__, template_folder="../templates")
    app.register_blueprint(strategy_bp)
    app.register_blueprint(strategy_mobile_bp)
    return app.test_client()


def _as(monkeypatch, rid, role):
    monkeypatch.setattr(auth, "get_current_user", lambda: {
        "id": 7, "restaurant_id": rid, "is_admin": 0, "role": role, "username": "u", "email": "u@x.com"})


def test_the_week_and_period_routes(client, db, monkeypatch):
    r = _ejs(db)
    _night(db, r.id, WED, 5000.0, 5300.0, {"Food": 5000.0})
    store.set_budget(r.id, WED, gross=5500, net=5200, db_path=db)
    _as(monkeypatch, r.id, "client")
    body = client.get("/api/dsr/week").get_json()                     # defaults to the latest report's week
    assert body["ok"] and body["view"] == "owner" and body["week"]["label"] == "Period 9 · Week 4"
    assert body["week"]["days"][0]["budget_net"] == 5200.0
    assert client.get("/api/dsr/period?date=2026-09-16").get_json()["period"]["label"] == "Period 9"
    assert client.get("/api/dsr/week?date=9-16-26").status_code == 400
    _as(monkeypatch, r.id, "manager")
    body = client.get("/api/dsr/week?date=2026-09-16").get_json()
    assert body["view"] == "manager" and "budget_net" not in body["week"]["days"][0]
    _as(monkeypatch, r.id, "employee")
    assert client.get("/api/dsr/week").status_code == 403
    other = create_restaurant(Restaurant(name="Elsewhere", owner_email="o@x.com"), db_path=db)
    _as(monkeypatch, other, "client")
    assert client.get("/api/dsr/week?date=2026-09-16").get_json()["week"]["totals"]["net"] is None

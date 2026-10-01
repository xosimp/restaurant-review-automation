"""DSR KPI round 1 (owner, 9/30/26): the night's checks read when the report
runs, the Service block (meal periods, rooms, servers, the loss ledger, punch
edits), labor by department and by hour, the week's pace, tomorrow's labor %
and the week's overtime, a 7th day in a row, the owner's all-in history, and
the six KPIs the report leads with.

The rule this round is held to: archive-fed figures are in the FIRST version
the owner reads — the 4am archive job is never waited for, and a failed read
never holds the report open.
"""
import json
import re
import sys
import types
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

import models
import pos
import dsr
from dsr import access, block_labor, block_service, common, kpis, narrative, pipeline, store, tomorrow
from models import Restaurant, create_restaurant, update_restaurant, get_restaurant

DAY = date(2026, 9, 22)                     # a Tuesday; closes 11pm CDT = 04:00 UTC 9/23
DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
OTHERS = ("food", "reviews", "marketing", "intel", "closeout")
SRC = (Path(__file__).resolve().parent.parent / "templates" / "dashboard.html").read_text()
OWNER = {"id": 1, "role": "client"}
MANAGER = {"id": 2, "role": "manager"}


def U(hour, minute=0, day=23):
    return datetime(2026, 9, day, hour, minute)


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
    monkeypatch.setattr(pos, "PROVIDERS", None)
    return db_path


def _tickets(day):
    iso = day.isoformat()
    t = []
    for i in range(12):
        t.append({"ticket_id": f"a{i}", "business_date": iso, "server_id": "e1", "server_name": "Maria Lopez",
                  "guest_count": 2, "bev_count": 3, "net_sales": 60.0, "tip": 12.0, "mealtime": "Dinner",
                  "profit_center": "Dining", "cancelled": 0})
    for i in range(9):
        t.append({"ticket_id": f"b{i}", "business_date": iso, "server_id": "e2", "server_name": "Tom Reed",
                  "guest_count": 1, "bev_count": 1, "net_sales": 30.0, "tip": 0, "mealtime": "Lunch",
                  "profit_center": "Bar", "cancelled": 0})
    for i in range(3):          # below the 8-check floor
        t.append({"ticket_id": f"c{i}", "business_date": iso, "server_id": "e3", "server_name": "Jo Park",
                  "guest_count": 2, "bev_count": 2, "net_sales": 50.0, "tip": 0, "mealtime": "Dinner",
                  "profit_center": "Dining", "cancelled": 0})
    for i in range(30):         # a shared terminal, never a person
        t.append({"ticket_id": f"s{i}", "business_date": iso, "server_id": "st", "server_name": "To Go AM",
                  "guest_count": 1, "bev_count": 0, "net_sales": 15.0, "tip": 0, "mealtime": "Lunch",
                  "profit_center": "Pick-Up", "cancelled": 0})
    return t


def _rows(day):
    iso = day.isoformat()
    lines = [
        {"line_id": "l1", "ticket_id": "a1", "business_date": iso, "kind": "comp", "sales": -20.0, "loss_amount": 20.0,
         "reason": "Error Made", "approver_id": "m1"},
        {"line_id": "l2", "ticket_id": "a2", "business_date": iso, "kind": "discount", "sales": -10.0,
         "loss_amount": None, "reason": None, "approver_id": "m2"},
        {"line_id": "l3", "ticket_id": "a3", "business_date": iso, "kind": "void", "sales": 0.0, "loss_amount": 14.0,
         "reason": "*Manager Test*", "approver_id": "m2"},
    ]
    punches = [
        {"punch_id": "p1", "business_date": iso, "employee_id": "e1", "employee_name": "Maria Lopez",
         "role": "Server PM", "clock_in": f"{iso} 16:00:00", "clock_out": f"{iso} 22:00:00", "reg_hours": 6.0,
         "is_station": 0, "edited_by": "m1", "edited_at": f"{iso} 23:10:00", "edit_what": "MC"},
        {"punch_id": "p2", "business_date": iso, "employee_id": "e2", "employee_name": "Tom Reed",
         "role": "Bartender PM", "clock_in": f"{iso} 11:00:00", "clock_out": f"{iso} 16:00:00", "reg_hours": 5.0,
         "is_station": 0, "edited_by": None},
        {"punch_id": "p3", "business_date": iso, "employee_id": "st", "employee_name": "To Go AM", "role": "Host AM",
         "clock_in": f"{iso} 10:00:00", "clock_out": f"{iso} 18:00:00", "reg_hours": 8.0, "is_station": 1,
         "edited_by": None},
    ]
    return {"tickets": _tickets(day), "lines": lines, "punches": punches, "payments": [], "payouts": [],
            "max_stamp": f"{iso}T23:59:00"}


def _archiving_pos(state):
    mod = types.SimpleNamespace()

    def archive_rows(rid, day):
        state["archive_calls"].append(day.isoformat() if hasattr(day, "isoformat") else str(day))
        if state.get("archive_fails"):
            raise RuntimeError("POS read failed")
        return _rows(day)
    mod.archive_rows = archive_rows
    mod.employee_names = lambda rid: {"m1": "Andrew Marola", "m2": "Gabe Huerta"}
    mod.is_station_name = lambda name: str(name).startswith("To Go")
    mod.job_categories = lambda rid: {"server pm": "Front of House", "bartender pm": "Front of House"}
    return mod


@pytest.fixture
def world(db, monkeypatch):
    state = {"archive_calls": [], "archiving": True}
    fake = _archiving_pos(state)
    monkeypatch.setattr(pos, "connected_provider",
                        lambda rid: ("fakepos", fake if state["archiving"] else object()))
    monkeypatch.setattr(pos, "fetch_day_closed", lambda rid, day: (True, "fakepos"))
    monkeypatch.setattr(pos, "fetch_day_sales", lambda rid, day: ({
        "gross": 2100.0, "net": 2000.0, "transactions": 80, "guests": 120, "discounts": 10.0, "comps": 20.0,
        "voids": 0.0, "refunds": 0.0, "tax": 160.0, "by_department": {"Food": 2000.0},
        "by_hour": {"12": 800.0, "19": 1200.0}, "items": [], "net_deductions": [], "source_checks": {}}, "fakepos"))
    for name in OTHERS:
        mod = types.ModuleType(f"dsr.block_{name}")
        mod.collect = lambda ctx, _n=name: dsr.block(dsr.READY, source="fake", metrics={"n": 1})
        monkeypatch.setitem(sys.modules, f"dsr.block_{name}", mod)
    fake_narr = types.ModuleType("dsr.narrative")
    fake_narr.write = lambda ctx, facts: {"ok": True, "narrative": {"executive_summary": {
        "text": "A good night.", "facts": ["sales.net"]}}, "reason": None}
    monkeypatch.setitem(sys.modules, "dsr.narrative", fake_narr)
    return state


def _restaurant(db, name="Round Co"):
    rid = create_restaurant(Restaurant(name=name, owner_email="r@x.com", timezone="America/Chicago"), db_path=db)
    update_restaurant(rid, {"open_times_json": json.dumps({d: "11:00am" for d in DAYS}),
                            "close_times_json": json.dumps({d: "11:00pm" for d in DAYS})}, db_path=db)
    conn = models.get_conn(db)
    conn.execute("INSERT INTO labor_daily_history (restaurant_id, date, day_of_week, labor_pct, labor_cost, sales, "
                 "total_hours) VALUES (?,?,?,?,?,?,?)", (rid, DAY.isoformat(), "Tuesday", 22.0, 440.0, 2000.0, 32.0))
    conn.commit()
    conn.close()
    return get_restaurant(rid, db_path=db)


# ── the caveat: read at report time, in version one, never holding ─────────

def test_the_nights_checks_are_read_when_the_report_runs_and_are_in_version_one(world, db):
    r = _restaurant(db)
    out = pipeline.run_night(r, DAY, pipeline.TRIGGER_SWEEP, now_utc=U(4, 10), db_path=db)
    assert out["action"] == "final"
    rep = store.get_report(r.id, DAY, db_path=db)
    assert rep["version"] == 1
    svc = rep["facts"]["blocks"]["service"]
    assert svc["status"] == dsr.READY and svc["detail"]["read_at_report_time"] is True
    # Read once in the pass, for the night itself — 10 minutes after close,
    # hours before the 4am archive job.
    assert world["archive_calls"] == [DAY.isoformat()]
    conn = models.get_conn(db)
    try:
        n = conn.execute("SELECT COUNT(*) FROM pos_tickets WHERE restaurant_id=? AND business_date=?",
                         (r.id, DAY.isoformat())).fetchone()[0]
    finally:
        conn.close()
    assert n == len(_tickets(DAY))
    assert {d["name"] for d in svc["detail"]["dayparts"]} == {"Dinner", "Lunch"}


def test_a_failed_check_read_goes_out_labelled_and_never_holds_the_report(world, db):
    world["archive_fails"] = True
    r = _restaurant(db)
    out = pipeline.run_night(r, DAY, pipeline.TRIGGER_SWEEP, now_utc=U(4, 10), db_path=db)
    assert out["action"] == "final"                       # not "retry": service never holds
    rep = store.get_report(r.id, DAY, db_path=db)
    svc = rep["facts"]["blocks"]["service"]
    assert svc["status"] == dsr.UNAVAILABLE and svc["reason"] == block_service.REASON_FAILED
    assert block_service.REASON_FAILED in rep["facts"]["missing"]
    assert "service" in pipeline.NEVER_HOLDS


def test_a_pos_without_check_detail_is_quiet_not_missing(world, db):
    world["archiving"] = False
    r = _restaurant(db)
    pipeline.run_night(r, DAY, pipeline.TRIGGER_SWEEP, now_utc=U(4, 10), db_path=db)
    rep = store.get_report(r.id, DAY, db_path=db)
    assert rep["facts"]["blocks"]["service"]["status"] == dsr.NOT_CONNECTED
    assert block_service.REASON_NOT_CONNECTED not in rep["facts"]["missing"]
    assert world["archive_calls"] == []


def test_nothing_is_read_before_the_nights_sales_are_in(world, db):
    r = _restaurant(db)
    ctx = dsr.Context(r, DAY, db_path=db)
    ctx.blocks = {"sales": dsr.block(dsr.AWAITING, block_name="sales")}
    blk = block_service.collect(ctx)
    assert blk["status"] == dsr.AWAITING and world["archive_calls"] == []


def test_the_archive_is_read_once_per_pass(world, db):
    r = _restaurant(db)
    ctx = dsr.Context(r, DAY, db_path=db)
    ctx.blocks = {"sales": dsr.block(dsr.READY, block_name="sales", metrics={"net": 2000.0, "gross_items": 2050.0})}
    common.night_archive(ctx)
    common.night_archive(ctx)
    assert world["archive_calls"] == [DAY.isoformat()]


# ── the Service block's figures ────────────────────────────────────────────

def _service(world, db):
    r = _restaurant(db)
    ctx = dsr.Context(r, DAY, db_path=db)
    ctx.blocks = {"sales": dsr.block(dsr.READY, block_name="sales", metrics={"net": 2000.0, "gross_items": 2000.0})}
    return block_service.collect(ctx)


def test_servers_are_people_past_the_floor_never_a_station(world, db):
    blk = _service(world, db)
    names = [s["name"] for s in blk["detail"]["servers"]]
    assert names == ["Maria Lopez", "Tom Reed"]          # the To Go terminal and Jo (3 checks) are not shown
    assert blk["detail"]["servers_below_floor"] == 1
    maria = blk["detail"]["servers"][0]
    assert maria["per_guest"] == 30.0 and maria["drinks_per_guest"] == 1.5 and maria["tip_pct"] == 20.0
    assert maria["hours"] == 6.0 and maria["net_per_hour"] == 120.0


def test_the_loss_ledger_names_the_approver_and_keeps_voids_apart(world, db):
    blk = _service(world, db)
    loss = blk["detail"]["loss"]
    assert loss["given"]["total"] == 30.0 and loss["given"]["pct_of_gross"] == 1.5
    assert {a["approver"] for a in loss["given"]["by_approver"]} == {"Andrew Marola", "Gabe Huerta"}
    assert loss["voids"]["lines"] == 1 and loss["voids"]["by_reason"][0]["reason"] == "*Manager Test*"
    edits = blk["detail"]["timeclock_edits"]
    assert len(edits) == 1 and edits[0]["edited_by"] == "Andrew Marola" and edits[0]["code"] == "MC"


def test_who_sees_what_in_the_service_block(world, db):
    blk = _service(world, db)
    facts = {"blocks": {"service": blk}}
    owner, _ = access.redact(facts, OWNER)
    mgr, hidden = access.redact(facts, MANAGER)
    od, md = owner["blocks"]["service"]["detail"], mgr["blocks"]["service"]["detail"]
    assert "timeclock_edits" in od and "loss" in od
    assert "timeclock_edits" not in md and "service.timeclock_edits" in hidden      # owner only, always
    assert "loss" not in md                                                          # needs the loss grant
    assert md["servers"]                                                             # managers see servers (Will)
    granted, _ = access.redact(facts, dict(MANAGER, grants=["loss.view"]))
    assert "loss" in granted["blocks"]["service"]["detail"]


def test_no_staff_names_reach_the_models_prompt(world, db):
    blk = _service(world, db)
    facts = {"blocks": {"service": blk, "sales": dsr.block(dsr.READY, block_name="sales", metrics={"net": 2000.0})}}
    for key in ("servers", "loss", "timeclock_edits"):
        assert narrative._private("service", key)
    f = narrative.Facts(facts)
    assert not ({"service.servers", "service.loss", "service.timeclock_edits"} & set(f.details))
    assert "maria lopez" not in f.block_strings["service"] and "andrew marola" not in f.block_strings["service"]
    src = Path(narrative.__file__).read_text()
    assert "and not _private(bname, k)" in src


# ── labor by department and by hour ────────────────────────────────────────

@pytest.mark.parametrize("role,cat,dept", [
    ("Server PM", "Front of House", "Front of house"), ("Bartender AM", "Front of House", "Bar"),
    ("Barback PM", "Front of House", "Bar"), ("Kitchen", "Back of House", "Kitchen"),
    ("Dishwasher", "Back of House", "Kitchen"), ("Utility PM", None, "Kitchen"), ("Training", None, "Other"),
    ("Manager BOH", "Back of House", "Management"), ("Mascot", "Front of House", "Front of house"),
])
def test_departments_follow_the_pos_categories_then_the_job_name(role, cat, dept):
    assert block_labor.department_of(role, cat) == dept


def test_department_dollars_add_up_to_the_nights_labor(db, monkeypatch):
    r = get_restaurant(create_restaurant(Restaurant(name="Dept Co", owner_email="d@x.com"), db_path=db), db_path=db)
    ctx = dsr.Context(r, DAY, db_path=db)
    monkeypatch.setattr(block_labor, "_job_categories", lambda ctx, p: {})
    rows = [{"date": DAY.isoformat(), "employee": "A", "role": "Server PM", "shift_start": "16:00",
             "shift_end": "22:00", "actual_hours": "6", "pay_rate": "10"},
            {"date": DAY.isoformat(), "employee": "B", "role": "Kitchen", "shift_start": "15:00",
             "shift_end": "23:00", "actual_hours": "8", "pay_rate": "20"}]
    out = block_labor.departments(ctx, None, rows, rows, 440.0, 2000.0)
    assert [d["department"] for d in out] == ["Front of house", "Kitchen"]
    assert round(sum(d["cost"] for d in out), 2) == 440.0
    assert out[1]["cost"] > out[0]["cost"]


def test_labor_hours_are_shared_over_each_clock_hour():
    rows = [{"date": "2026-09-22", "shift_start": "17:30", "shift_end": "19:30", "actual_hours": "2"}]
    hh = block_labor.hourly_hours(rows)
    assert hh == {17: 0.5, 18: 1.0, 19: 0.5}


# ── the owner's all-in history, the week's pace, the six tiles ─────────────

def test_the_owners_all_in_labor_is_compared_with_all_in_nights(db, monkeypatch):
    series = {"labor.cost": {"2026-09-15": 400.0}, "sales.net": {"2026-09-15": 2000.0}}
    monkeypatch.setattr(kpis, "_series", lambda rid, fact, s, e, d: dict(series.get(fact, {})))
    hourly = kpis._history("labor_pct", 1, DAY, db)
    allin = kpis._history("labor_pct", 1, DAY, db, salaried_share=100.0)
    assert hourly == {"2026-09-15": 0} or hourly.get("2026-09-15") != allin["2026-09-15"]
    assert allin["2026-09-15"] == 25.0                       # (400 + 100) / 2000


def test_the_pace_tile_says_what_the_week_needs_and_never_a_negative():
    p = {"week": {"net": 38420, "nights_measured": 6, "nights_total": 7, "nights_left": 1, "budget_vs": -1580,
                  "budget_total": 70000, "budget_left": 31580, "budget_to_date": 40000, "budget_label": "Budget",
                  "budget_per_night_needed": 31580, "typical_per_night": 9800, "last_year_pct": 4.2}}
    t = kpis._pace_tile(p, access.OWNER)
    assert t["tone"] == "bad" and t["trend"] == {"dir": "down", "text": "$1,580", "vs": "vs budget", "tone": "bad"}
    assert "from the night left" in t["sub"][0] and t["sub"][1] == "↑ 4.2% vs last year" and t["bar"]["max"] == 70000
    made = dict(p["week"], budget_left=-200, budget_vs=300)
    t2 = kpis._pace_tile({"week": made}, access.OWNER)
    assert any("already made" in s for s in t2["sub"]) and not any("Needs" in s for s in t2["sub"])
    mgr = kpis._pace_tile({"week": {"net": 38420, "nights_measured": 6, "nights_total": 7, "last_year_pct": 4.2}},
                          access.MANAGER)
    assert mgr["sub"] == [] and mgr["bar"] is None and mgr["trend"]["vs"] == "vs last year"


def test_a_managers_pace_never_carries_the_budget(monkeypatch):
    monkeypatch.setattr(kpis, "_span_pace", lambda *a, **k: {"net": 1, "budget_total": 5, "budget_vs": 2,
                                                             "last_year": 1, "nights_measured": 1, "nights_total": 7})
    import dsr.fiscal as fiscal
    monkeypatch.setattr(fiscal, "week_bounds", lambda r, d: (d, d))
    monkeypatch.setattr(fiscal, "period_span", lambda r, d: None)
    r = types.SimpleNamespace(id=1)
    p = kpis.pace({"business_date": DAY.isoformat()}, r, access.MANAGER)
    assert p["week"] == {"net": 1, "last_year": 1, "nights_measured": 1, "nights_total": 7}


def test_the_six_lead_tiles_skip_what_isnt_measured():
    def k(key, v):
        return {"key": key, "label": key, "value": v, "value_text": str(v), "unit": "count", "spark": [1, 2, 3],
                "change": {"text": "↑ 1% vs last Tuesday", "tone": "good"}}
    top = [k("guests", 312), k("per_guest", 27.8), k("splh", 66.0), k("avg_ticket", 50.0), k("overtime", 3.0)]
    big = kpis.big(top, [], None, None, access.OWNER)            # no pace, no prime, no tomorrow
    assert [t["key"] for t in big] == ["guests", "per_guest", "splh", "avg_ticket", "overtime"]
    mgr = kpis.big([k("labor_pct", 28.0)] + top, [], None, None, access.MANAGER)
    assert mgr[0]["key"] == "labor_pct" and len(mgr) <= kpis.BIG_MAX


# ── tomorrow: labor %, the week's overtime, a 7th day in a row ─────────────

def _week(monkeypatch, published, worked):
    monkeypatch.setattr(tomorrow, "_published", lambda rid, d, db: [dict(r, date=d.isoformat())
                                                                     for r in published.get(d.isoformat(), [])])
    monkeypatch.setattr(tomorrow, "_worked_shifts", lambda rid, db: worked)
    monkeypatch.setattr(tomorrow, "_rates", lambda r, rid, db: ({"server": 10.0}, 10.0, {}, {}))
    monkeypatch.setattr(tomorrow, "_salaried", lambda r: set())


def test_tomorrows_labor_is_the_schedule_priced_against_the_forecast(monkeypatch):
    nxt = (DAY + timedelta(days=1)).isoformat()
    _week(monkeypatch, {nxt: [{"employee": "A", "role": "Server", "shift_start": "16:00", "shift_end": "22:00",
                               "scheduled_hours": "6"}]}, [])
    import models as m
    monkeypatch.setattr(m, "salaried_day_share", lambda r: 40.0)
    r = types.SimpleNamespace(id=1, week_start_day=2, hourly_rate=10.0)
    plan = tomorrow.labor_plan(r, DAY, 600.0)
    assert plan["hours"] == 6.0 and plan["hourly_cost"] == 60 and plan["hourly_pct"] == 10.0
    assert plan["salaried_total_pct"] == round(100 / 600 * 100, 1)


def test_the_overtime_outlook_names_who_crosses_40_and_who_has_room(monkeypatch):
    first = DAY - timedelta(days=DAY.weekday())                # a Monday payroll week: Mon 9/21
    worked = [{"date": (first + timedelta(days=i)).isoformat(), "employee": "A", "role": "Server",
               "actual_hours": "8", "shift_start": "10:00", "shift_end": "18:00"} for i in range((DAY - first).days + 1)]
    ahead = {}
    d = DAY + timedelta(days=1)
    while d <= first + timedelta(days=6):
        ahead[d.isoformat()] = [{"employee": "A", "role": "Server", "scheduled_hours": "8"},
                                {"employee": "B", "role": "Server", "scheduled_hours": "4"}]
        d += timedelta(days=1)
    _week(monkeypatch, ahead, worked)
    r = types.SimpleNamespace(id=1, week_start_day=0, hourly_rate=10.0)
    ot = tomorrow.overtime_outlook(r, DAY)
    a = ot["people"][0]
    assert a["employee"] == "A" and a["overtime_hours"] == 16.0 and a["extra_cost"] == 80
    assert a["room"] == ["B"] and ot["over_count"] == 1


def test_a_seventh_day_in_a_row_is_flagged(monkeypatch):
    nxt = (DAY + timedelta(days=1)).isoformat()
    worked = [{"date": (DAY - timedelta(days=i)).isoformat(), "employee": "Ava Chen", "hours": "6"} for i in range(6)]
    _week(monkeypatch, {nxt: [{"employee": "Ava Chen", "role": "Server", "scheduled_hours": "6"}]}, worked)
    items = tomorrow.rest_day_items(types.SimpleNamespace(id=1, week_start_day=2), DAY)
    assert items == [{"kind": "rest_day", "tone": "warn", "text": "Ava Chen would work a 7th day in a row"}]


def test_tomorrows_labor_follows_the_labor_view_and_salaries_stay_the_owners():
    t = {"labor": {"hours": 6, "hourly_pct": 10.0, "salaried_total_pct": 16.7, "salaried_cost": 40},
         "overtime": {"people": []}, "items": [{"kind": "rest_day", "text": "x"}], "predictions": []}
    mgr = access.tomorrow_for({"tomorrow": t}, MANAGER, access.MANAGER, withheld=[])
    assert "salaried_total_pct" not in mgr["labor"] and mgr["labor"]["hourly_pct"] == 10.0
    no_labor = access.tomorrow_for({"tomorrow": t}, MANAGER, access.MANAGER, withheld=["labor"])
    assert "labor" not in no_labor and "overtime" not in no_labor and not no_labor["items"]
    own = access.tomorrow_for({"tomorrow": t}, OWNER, access.OWNER, withheld=[])
    assert own["labor"]["salaried_total_pct"] == 16.7


# ── the web report ─────────────────────────────────────────────────────────

def test_the_report_leads_with_six_large_animated_tiles():
    assert "function bigHtml(list,p)" in SRC and "p.kpis_big" in SRC
    assert "cavCount(el,n," in SRC                     # the one count-up engine, never a private tween
    # One hero at twice the width (owner, 10/1/26), the only card that keeps moving.
    assert ".kx{display:grid;grid-template-columns:repeat(4,minmax(0,1fr))" in SRC
    assert ".kx-hero{grid-column:span 2;grid-row:span 2;" in SRC
    for loop in (".kx.on .kx-hero .kx-glow{animation", ".kx.on .kx-hero .kx-spark .halo{animation",
                 ".kx.on .kx-hero .kx-bar .tr i s{animation"):
        assert loop in SRC
    assert "@media (prefers-reduced-motion:reduce){.kx:not(.on)>.kx-hero" in SRC
    assert "b.innerHTML=loading()+kxSkeleton()+hxSkeleton();" in SRC
    for fn in ("hourHtml", "moneyHtml", "laborHtml", "serversHtml", "lossHtml", "tmrLaborHtml"):
        assert f"function {fn}(" in SRC


def test_the_new_report_styles_dont_collide_with_existing_classes():
    # .sw, .dr-hours and .dr-tbl were already taken (a page wrapper, the
    # Sales block's bars, the items table) and broke the first draft.
    js = SRC[SRC.index("  function bigNum(t){"):SRC.index("  // Manager: Today's shift and Operations.")]
    assert 'class="sw ' not in js and "dr-hours" not in js and 'class="dr-tbl' not in js
    assert len(re.findall(r"@keyframes drGrow\b", SRC)) == 1


def test_over_a_starting_target_is_amber_never_red():
    t = {"weekday": "Wednesday", "labor": {"hours": 100, "hourly_cost": 2400, "hourly_pct": 32.0,
                                            "target_pct": 30.0, "target_source": "default"}}
    tile = kpis._tomorrow_tile(t, access.MANAGER)
    assert tile["tone"] == "warn" and tile["trend"]["vs"] == "vs Cavnar AI's starting 30%" and tile["gauge"]["soft"]
    owned = kpis._tomorrow_tile({"weekday": "Wednesday", "labor": dict(t["labor"], target_source="set")},
                                access.MANAGER)
    assert owned["tone"] == "bad" and owned["trend"]["vs"] == "vs the 30% target" and owned["trend"]["dir"] == "up"
    assert ".kx-gauge.soft .gv{stroke:var(--hb-warn)}" in SRC


def test_the_weeks_pace_reads_real_nights(db, monkeypatch):
    """The first live read (9/30/26) raised on the last-year step: the
    measured nights are ISO strings, the calendar days are dates."""
    from dsr import store as _store
    r = types.SimpleNamespace(id=1)
    first = DAY - timedelta(days=2)
    monkeypatch.setattr(_store, "baselines_net", lambda rid, days, **k: {
        (d.isoformat() if hasattr(d, "isoformat") else str(d)): (1000.0, "dsr") for d in days})
    monkeypatch.setattr(_store, "night_budget", lambda rid, d, **k: {"net": 1100.0, "source": "budget"})
    monkeypatch.setattr(_store, "last_year_day", lambda rest, d: d - timedelta(days=364))
    import demand
    monkeypatch.setattr(demand, "forecast_net", lambda rid, d, **k: {"available": True, "typical_sales": 1050.0})
    p = kpis._span_pace(r, first, first + timedelta(days=6), DAY, db)
    assert p["net"] == 3000.0 and p["nights_measured"] == 3 and p["budget_vs"] == -300.0
    assert p["budget_per_night_needed"] == round((7700 - 3000) / 4, 2) and p["typical_per_night"] == 1050.0
    assert p["last_year"] == 3000.0 and p["last_year_pct"] == 0.0


def test_one_hero_four_secondary_and_the_rest_chips():
    def k(key, spark=True):
        return {"key": key, "label": key, "value": 1, "value_text": "1", "unit": "count",
                "spark": [1, 2, 3] if spark else [], "change": None}
    top = [k("prime_pct", spark=False), k("guests"), k("per_guest"), k("splh"), k("avg_ticket"), k("overtime")]
    big = kpis.big(top, [], None, None, access.OWNER)
    # prime cost has no picture tonight (no trend, no target), so the first
    # tile that has one leads; the order is otherwise kept.
    assert [t["rank"] for t in big] == ["hero", "secondary", "secondary", "secondary", "secondary", "tertiary"]
    assert big[0]["key"] == "guests" and [t["key"] for t in big[1:]] == ["prime_pct", "per_guest", "splh",
                                                                         "avg_ticket", "overtime"]


def test_key_numbers_sit_above_the_day_after():
    n = SRC[SRC.index("  function narrativeHtml(n,ck,p){"):SRC.index("  function drEffects(list){")]
    assert n.index("h+=big?bigHtml(big,p):kpisHtml(p&&p.kpis,p);") < n.index("h+=tomorrowHtml(p&&p.tomorrow,n,p&&p.view);")


# ── the hour-by-hour story (owner, 10/1/26) ────────────────────────────────

def _hx_node(rows_js):
    import shutil
    import subprocess
    if not shutil.which("node"):
        pytest.skip("node is not installed")
    a, b = SRC.index("  function hxK(v){"), SRC.index("  function hourHtml(p){")
    helpers = ("function isNum(v){return typeof v==='number'&&isFinite(v);}function esc(v){return String(v);}"
               "function hn(s){return s;}function money(v){return '$'+Math.round(v);}"
               "function hourLabel(h,f){h=(+h)%24;return ((h%12)||12)+(f?(h<12?'am':'pm'):(h<12?'a':'p'));}")
    js = helpers + SRC[a:b] + ("var rows=" + rows_js + ";var peak=rows[0];rows.forEach(function(r){if(r.net>peak.net)peak=r;});"
                               "var cs=hxCallouts(rows,peak,'Tuesday');"
                               "console.log(JSON.stringify({c:cs.list.map(function(x){return [x.kind,rows[x.i].hour];}),"
                               "s:hxStory(rows,peak,cs,'Tuesday')}));")
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_the_rush_is_the_climb_into_the_peak_not_a_lunch_bump():
    hours = [(11, 310, 290, 6), (12, 820, 760, 9), (13, 690, 720, 9), (14, 380, 400, 8), (15, 260, 300, 8),
             (16, 420, 450, 9), (17, 980, 900, 12), (18, 1420, 1280, 14), (19, 1350, 1290, 14), (20, 1010, 1080, 13),
             (21, 640, 700, 10), (22, 280, 330, 6), (23, 120, 160, 3)]
    rows = "[" + ",".join(f"{{hour:{h},net:{n},usual:{u},hours:{l},splh:{n}/{l}}}" for h, n, u, l in hours) + "]"
    out = _hx_node(rows)
    kinds = dict((k, h) for k, h in out["c"])
    assert kinds.get("rush") == 17                     # 5pm, never the noon bump
    assert kinds.get("labor") == 15                    # 3pm: $33 a labor hour against $72
    assert out["s"].startswith("The rush began at <b>5pm</b> and peaked at <b>6pm</b> with $1420, 11% above a usual Tuesday.")
    assert len(out["c"]) <= 4


def test_a_quiet_flat_night_says_nothing_it_cannot_prove():
    rows = "[" + ",".join(f"{{hour:{h},net:500,usual:null,hours:null,splh:null}}" for h in range(11, 22)) + "]"
    out = _hx_node(rows)
    assert out["c"] == [] and out["s"].startswith("Sales peaked at")


def test_the_hour_chart_is_one_hybrid_story():
    js = SRC[SRC.index("  function hourHtml(p){"):SRC.index("  function hxSkeleton(){")]
    for part in ('class="hm"', "'hxp':'hxb'", 'class="ll"', 'class="lg"', 'class="ul"', "hx-peak", "hx-tip",
                 "What stood out", "hx-story", 'class="hs"'):
        assert part in js, part
    assert "The hour-by-hour story appears once the POS sends" in js      # the empty state
    assert "b.innerHTML=loading()+kxSkeleton()+hxSkeleton();" in SRC      # the loading state
    assert "@media (prefers-reduced-motion:reduce){.hx *,.hx-svg .hb.pk{animation:none!important}" in SRC
    assert 'class="sw ' not in js


def test_todays_score_carries_the_brand_gradient():
    assert "#panel-dsr .dr-score{border-color:var(--hb-tint2);" in SRC
    assert "#panel-dsr .dr-score::before{" in SRC

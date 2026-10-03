"""Schedule audit 10/3/26, workstream E — demand moves the numbers.

D-23/P-19/PR-7  the usual crew is scaled by each date's measured demand
                (bounded, held to the sales-per-labor-hour target), with the
                reason in the row; the score judges coverage against the
                same numbers; asks fold in, never as a firm shortfall.
D-24            each day's hours target follows its date's demand; a
                closed date takes no hours and no sales.
D-26            the hourly curve and the daypart split come from the ticket
                archive by business date, the captures as a supplement.
L-25            an unmeasured split is no daypart target — never 40/60.
D-30            measured rain on a fresh forecast moves a near-term date,
                only once the rain effect is measured.
D-32            a late segment (10pm to a close past 11pm) has its own usual
                crew, row, coverage check and sales per labor hour.
D-33            freshness on the restaurant's own date; 14+ trading days of
                no sales stop a generation.
"""
import json
import types
from datetime import date, timedelta

import pytest

import labor
import models
import schedule_economics as econ
import schedule_engine
import schedule_requirements as req
import shift_quality as sq

FRI = "2026-10-09"
WEEK = ["2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09", "2026-10-10", "2026-10-11"]


@pytest.fixture(autouse=True)
def _redirect(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    yield


def _exec(sql, args=()):
    c = models.get_conn()
    try:
        c.execute(sql, args)
        c.commit()
    finally:
        c.close()


def _rid(name="Late Bar", **kw):
    return models.create_restaurant(models.Restaurant(name=name, owner_email=f"{name.split()[0].lower()}@x.test",
                                                      timezone="America/Chicago", **kw))


def _ticket(rid, tid, bd, opened, net, cancelled=0):
    _exec("INSERT INTO pos_tickets (restaurant_id, provider, ticket_id, business_date, opened_at, net_sales, "
          "cancelled) VALUES (?,?,?,?,?,?,?)", (rid, "rpower", tid, bd, opened, net, cancelled))


# ── D-23 / P-19 / PR-7: the numbers move, with the reason ────────────────

def test_a_measured_busy_date_raises_the_usual_crew_and_says_why():
    typical = {("Friday", "night"): {"Server": 4, "Cook": 2}}
    dd = {FRI: {"ratio": 1.3, "pct": 30, "reasons": ["Homecoming (+30% here last year): +30%"]}}
    rows = req.shift_requirements([FRI], typical_headcount=typical, date_demand=dd)
    night = next(r for r in rows if r["daypart"] == "night")
    by = {x["role"]: x for x in night["roles"]}
    assert by["Server"]["required"] == 5 and by["Server"]["typical"] == 4 and "usual 4" in by["Server"]["reason"]
    assert by["Cook"]["required"] == 3
    assert night["factor"] == 1.3
    assert any("+30%" in r and "Homecoming" in r for r in night["reasons"])
    block = req.requirements_block(rows)
    assert "Server 5 (usual 4)" in block and "why: usual crew +30%" in block
    # A typical date is the usual crew, with nothing to explain.
    plain = req.shift_requirements(["2026-10-16"], typical_headcount=typical)
    assert next(r for r in plain if r["daypart"] == "night")["reasons"] == []


def test_the_scaling_is_bounded_and_the_owners_floor_and_the_cap_still_hold():
    typical = {("Friday", "night"): {"Server": 4, "Host": 1}}
    huge = {FRI: {"ratio": 2.0, "pct": 100, "reasons": ["New Year's Eve"]}}
    night = next(r for r in req.shift_requirements([FRI], typical_headcount=typical, date_demand=huge)
                 if r["daypart"] == "night")
    assert {x["role"]: x["required"] for x in night["roles"]} == {"Server": 6, "Host": 1}     # 4 × 1.4, 1 × 1.4
    assert any("held to +40%" in r for r in night["reasons"])
    dead = {FRI: {"ratio": 0.3, "pct": -70, "reasons": ["storm"]}}
    floors = {"Server": {"night": 4}}
    night = next(r for r in req.shift_requirements([FRI], typical_headcount=typical, date_demand=dead,
                                                    role_floors=floors) if r["daypart"] == "night")
    by = {x["role"]: x for x in night["roles"]}
    assert by["Server"]["required"] == 4 and by["Server"]["floor"] == 4          # the floor holds
    assert by["Host"]["required"] == 1                                           # a role that runs keeps one
    capped = next(r for r in req.shift_requirements([FRI], typical_headcount=typical, date_demand=huge,
                                                     section_cap=5, cap_roles=["server"]) if r["daypart"] == "night")
    assert {x["role"]: x["required"] for x in capped["roles"]}["Server"] == 5


def test_the_sales_per_labor_hour_hold_brings_an_overstaffed_crew_in():
    typical = {("Tuesday", "night"): {"Server": 4}}
    rows = req.shift_requirements(["2026-10-06"], typical_headcount=typical,
                                  splh_hold={"Tuesday": {"night": 0.8}})
    night = next(r for r in rows if r["daypart"] == "night")
    assert night["roles"][0]["required"] == 3 and night["factor"] == 0.8
    assert any("sales per labor hour held to its target" in r for r in night["reasons"])


def test_an_ask_folds_in_with_its_reason_and_is_never_a_firm_shortfall():
    typical = {("Friday", "night"): {"Server AM": 1, "Server PM": 4}}
    asks = [{"date": FRI, "daypart": "night", "role": "server", "delta": 1,
             "reason": "asked by the 10/2/26 nightly report", "firm": False}]
    rows = req.shift_requirements([FRI], typical_headcount=typical, adjustments=asks)
    night = next(r for r in rows if r["daypart"] == "night")
    pm = next(x for x in night["roles"] if x["role"] == "Server PM")
    assert pm["required"] == 5 and pm["firm"] == 4 and pm["asked"] == 1
    assert "+1 asked by the 10/2/26 nightly report" in pm["reason"]
    assert req.requirements_map(rows)[(FRI, "night")]["Server PM"] == 4
    assert req.requirements_map(rows, firm=False)[(FRI, "night")]["Server PM"] == 5


def test_the_score_judges_coverage_against_the_scaled_numbers():
    rows = [{"date": FRI, "day": "Friday", "employee": f"S{i}", "role": "Server", "shift_start": "5:00pm",
             "shift_end": "11:00pm", "scheduled_hours": "6"} for i in range(4)]
    typical = {("Friday", "night"): {"Server": 4}}
    usual = sq.build_contexts(rows, typical_headcount=typical)
    cov = sq.dim_coverage(next(c for c in usual if c.daypart == "night"))
    assert cov.facts["missing"] == 0
    scaled = req.requirements_map(req.shift_requirements(
        [FRI], typical_headcount=typical, date_demand={FRI: {"ratio": 1.3, "reasons": ["Homecoming"]}}))
    ctxs = sq.build_contexts(rows, typical_headcount=typical, requirements_by_date=scaled)
    cov = sq.dim_coverage(next(c for c in ctxs if c.daypart == "night"))
    assert cov.facts["missing"] == 1 and cov.facts["short"] == {"Server": 1}


def test_the_generator_hands_the_scaled_week_to_the_score(monkeypatch):
    captured = {}

    def fake(client, **kwargs):
        captured.update(kwargs)
        return types.SimpleNamespace(content=[types.SimpleNamespace(
            text="date,day,employee,role,shift_start,shift_end,scheduled_hours,notes\n---SUMMARY---\n- ok")],
            stop_reason="end_turn")
    monkeypatch.setattr(labor, "create_with_retry", fake)
    monkeypatch.setattr(labor, "get_client", lambda *a, **k: None)
    monkeypatch.setattr(labor, "model_for", lambda k: "m")
    analysis = {"overall_labor_pct": 25.0, "overstaffed_days": [], "understaffed_days": [], "dow_summary": {},
                "total_sales": 0, "period_days": 0, "by_day": {}}
    patterns = {"typical_headcount": {("Friday", "night"): {"Server": 4}}, "cross_trained": {}}
    out = labor.generate_optimized_schedule(
        analysis, [], roster=[("Ana", "Server")], week_start="2026-10-05", hourly_rate=20.0, labor_target=30.0,
        staffing_patterns=patterns, date_demand={FRI: {"ratio": 1.3, "pct": 30, "reasons": ["Homecoming: +30%"]}})
    prompt = captured["messages"][0]["content"]
    assert "Fri 2026-10-09 night | Server 5 (usual 4)" in prompt and "Homecoming: +30%" in prompt
    assert out["requirements_by_date"]["2026-10-09|night"] == {"Server": 5}
    assert any(r["date"] == FRI and r["daypart"] == "night" for r in out["requirements"])


# ── D-24: day targets follow each date's demand ──────────────────────────

def _by_day(hours=10.0):
    return {(date(2026, 9, 14) + timedelta(days=i)).isoformat(): {"actual": hours} for i in range(14)}


def test_a_busy_dates_extra_hours_land_on_that_date():
    flat = labor.week_hours_plan({"by_day": _by_day()}, WEEK, labor_target=30.0, hourly_rate=20.0,
                                 projected_revenue_override=70000)
    busy = labor.week_hours_plan({"by_day": _by_day()}, WEEK, labor_target=30.0, hourly_rate=20.0,
                                 projected_revenue_override=70000,
                                 date_demand={FRI: {"ratio": 1.4, "pct": 40, "reasons": ["Halloween: +40%"]}})
    assert flat["daily_target_hours"][FRI] == flat["daily_target_hours"]["2026-10-06"]
    assert busy["daily_target_hours"][FRI] == pytest.approx(busy["daily_target_hours"]["2026-10-06"] * 1.4, rel=0.01)
    assert abs(sum(busy["daily_target_hours"].values()) - busy["hours_budget"]) < 1.0
    assert busy["daily_target_reasons"][FRI] == ["Halloween: +40%"]
    assert "Halloween: +40%" in busy["daily_targets_text"]


def test_a_closed_date_takes_no_hours_and_none_of_the_weeks_sales(db_path):
    # The shape schedule_economics.date_demand gives a closed date: no sales
    # of its own, its weekday's typical night kept.
    rid = models.create_restaurant(models.Restaurant(name="Closed Co", owner_email="c@x.test"), db_path=db_path)
    for k in range(1, 29):
        d = date(2026, 10, 3) - timedelta(days=k)
        _exec("INSERT INTO labor_daily_history (restaurant_id, date, day_of_week, sales, total_hours, labor_pct) "
              "VALUES (?,?,?,?,?,?)", (rid, d.isoformat(), d.strftime("%A"), 10000.0, 60.0, 30.0))
    dd = econ.date_demand(rid, WEEK, closed_dates={"2026-10-08"}, today=date(2026, 10, 3), db_path=db_path)
    assert dd["2026-10-08"]["closed"] and dd["2026-10-08"]["projected_sales"] == 0.0
    assert dd["2026-10-08"]["typical_sales"] == 10000.0
    plan = labor.week_hours_plan({"by_day": _by_day()}, WEEK, labor_target=30.0, hourly_rate=20.0,
                                 projected_revenue_override=70000, closed_dates={"2026-10-08"}, date_demand=dd)
    assert "2026-10-08" not in plan["daily_target_hours"] and len(plan["daily_target_hours"]) == 6
    assert plan["projected_revenue"] == 60000.0 and plan["budget_basis"]["closed_share"] == round(1 / 7, 3)
    assert "Closed: 2026-10-08" in plan["daily_targets_text"]


# ── D-26: the curve comes from the ticket archive ────────────────────────

def test_the_hourly_curve_is_built_from_the_ticket_archive_by_business_date(db_path):
    rid = _rid()
    today = date(2026, 10, 3)
    fridays = [today - timedelta(days=d) for d in (1, 8, 15)]          # 10/2, 9/25, 9/18
    for k, f in enumerate(fridays):
        bd = f.isoformat()
        nxt = (f + timedelta(days=1)).isoformat()
        _ticket(rid, f"{k}a", bd, f"{bd}T12:10:00", 200.0)
        _ticket(rid, f"{k}b", bd, f"{bd}T19:05:00", 500.0)
        _ticket(rid, f"{k}c", bd, f"{bd}T22:40:00", 200.0)
        _ticket(rid, f"{k}d", bd, f"{nxt}T00:30:00", 100.0)            # after midnight: the night before's
        _ticket(rid, f"{k}x", bd, f"{bd}T20:00:00", 900.0, cancelled=1)
    curve = econ.measured_sales_curve(rid, today=today, db_path=db_path)
    fri = curve["Friday"]
    assert fri["days"] == 3 and fri["sources"] == {"tickets": 3, "intraday": 0, "dsr": 0}
    assert fri["hours"] == {12: 0.2, 19: 0.5, 22: 0.2, 24: 0.1}
    assert fri["morning_share"] == 0.2 and fri["late_share"] == pytest.approx(0.3)
    assert econ.hourly_profile(rid, today=today, db_path=db_path)["Friday"][24] == 0.1


def test_the_captures_stand_in_only_for_a_date_the_archive_does_not_hold(db_path):
    rid = _rid()
    today = date(2026, 10, 3)
    for k, f in enumerate([date(2026, 10, 2), date(2026, 9, 25)]):
        _ticket(rid, f"t{k}", f.isoformat(), f"{f.isoformat()}T19:00:00", 1000.0)
    # 9/18 has captures only; 9/25 has both — its tickets win, never added.
    for bd in ("2026-09-18", "2026-09-25"):
        for hour, cum in ((12, 300.0), (19, 1000.0)):
            _exec("INSERT INTO pos_intraday (restaurant_id, business_date, captured_hour, weekday, net_sales) "
                  "VALUES (?,?,?,?,?)", (rid, bd, hour, "Friday", cum))
    fri = econ.measured_sales_curve(rid, today=today, db_path=db_path)["Friday"]
    assert fri["sources"] == {"tickets": 2, "intraday": 1, "dsr": 0}
    assert fri["days"] == 3


def test_the_engine_reads_the_archive_curve(db_path, monkeypatch):
    called = {}
    monkeypatch.setattr(econ, "hourly_profile", lambda rid, *a, **k: called.setdefault("rid", rid) and {"Friday": {19: 0.5}})
    assert schedule_engine._hourly_profile(7) == {"Friday": {19: 0.5}} and called["rid"] == 7


# ── L-25: an unmeasured split is no target, never 40/60 ──────────────────

def _history(rid, days, sales=4000.0, hours=60.0):
    for d in days:
        _exec("INSERT INTO labor_daily_history (restaurant_id, date, day_of_week, sales, total_hours, labor_pct) "
              "VALUES (?,?,?,?,?,?)", (rid, d.isoformat(), d.strftime("%A"), sales, hours, 30.0))


def test_a_weekday_with_no_measured_split_has_no_daypart_figures(db_path):
    rid = _rid()
    today = date(2026, 10, 3)
    fridays = [today - timedelta(days=d) for d in (1, 8, 15)]
    _history(rid, fridays)
    rows = ["date,day,employee,role,shift_start,shift_end,scheduled_hours,actual_hours,sales,notes"]
    for f in fridays:
        rows.append(f"{f.isoformat()},Friday,Ana,Server,11:00,15:00,4,4,4000,")
        rows.append(f"{f.isoformat()},Friday,Bo,Server,17:00,23:00,6,6,4000,")
    models.save_client_data(rid, "shifts", "\n".join(rows) + "\n", source="upload", db_path=db_path)
    splh = econ.splh_by_daypart(rid, today=today, db_path=db_path)
    assert splh["Friday"] == {}, "no 40/60 assumption"
    obj = econ.splh_objective(rid, splh=splh, labor_target_pct=30, today=today, db_path=db_path)
    assert obj["available"] is False and "Friday" not in json.dumps(obj.get("by_day") or {})
    # With the archive measuring the split, the dayparts are real.
    for k, f in enumerate(fridays):
        _ticket(rid, f"m{k}", f.isoformat(), f"{f.isoformat()}T12:00:00", 1000.0)
        _ticket(rid, f"n{k}", f.isoformat(), f"{f.isoformat()}T19:00:00", 3000.0)
    splh = econ.splh_by_daypart(rid, today=today, db_path=db_path)
    fri = splh["Friday"]
    assert fri["morning"]["sales"] == 1000.0 and fri["night"]["sales"] == 3000.0
    assert fri["morning"]["hours"] == 24.0 and fri["night"]["hours"] == 36.0      # 4h and 6h of 60h a day
    assert fri["night"]["sales_split"] == "measured"


def test_the_hours_split_reads_the_window_and_leaves_the_salaried_out(db_path):
    rid = _rid()
    models.update_restaurant(rid, {"salaried_staff_json": json.dumps([{"name": "Erik", "annual": 150000}])},
                             db_path=db_path)
    today = date(2026, 10, 3)
    fridays = [today - timedelta(days=d) for d in (1, 8, 15)]
    _history(rid, fridays)
    for k, f in enumerate(fridays):
        _ticket(rid, f"m{k}", f.isoformat(), f"{f.isoformat()}T12:00:00", 1000.0)
        _ticket(rid, f"n{k}", f.isoformat(), f"{f.isoformat()}T19:00:00", 3000.0)
    rows = ["date,day,employee,role,shift_start,shift_end,scheduled_hours,actual_hours,sales,notes"]
    for f in fridays:
        rows.append(f"{f.isoformat()},Friday,Ana,Server,11:00,15:00,4,4,4000,")
        rows.append(f"{f.isoformat()},Friday,Bo,Server,17:00,23:00,4,4,4000,")
        rows.append(f"{f.isoformat()},Friday,Erik,Manager,09:00,21:00,12,12,4000,")     # salaried: out
    old = (today - timedelta(weeks=20)).isoformat()                                     # outside the window
    rows.append(f"{old},Friday,Cy,Server,11:00,23:00,12,12,4000,")
    models.save_client_data(rid, "shifts", "\n".join(rows) + "\n", source="upload", db_path=db_path)
    fri = econ.splh_by_daypart(rid, today=today, db_path=db_path)["Friday"]
    assert fri["morning"]["hours"] == fri["night"]["hours"] == 30.0


# ── D-30: measured rain on a fresh forecast ──────────────────────────────

def _rain_record(rid, n=4, lift=-15.0):
    for k in range(n):
        d = date(2026, 8, 7) + timedelta(weeks=k)
        _exec("INSERT INTO event_outcomes (restaurant_id, business_date, weekday, kind, label, raw_label, lift_pct) "
              "VALUES (?,?,?,?,?,?,?)", (rid, d.isoformat(), d.strftime("%A"), "rain", "rain", "Rain", lift))


def test_measured_rain_on_a_fresh_forecast_moves_the_date(db_path):
    rid = _rid()
    _history(rid, [date(2026, 10, 2) - timedelta(weeks=k) for k in range(4)], sales=5000.0)
    wet = [{"date": FRI, "precip_pct": 80, "stale": False}]
    dd = econ.date_demand(rid, WEEK, weather=wet, today=date(2026, 10, 3), db_path=db_path)
    assert dd[FRI]["ratio"] == 1.0, "no measured rain effect yet: the forecast moves nothing"
    _rain_record(rid)
    dd = econ.date_demand(rid, WEEK, weather=wet, today=date(2026, 10, 3), db_path=db_path)
    assert dd[FRI]["ratio"] == 0.85 and dd[FRI]["pct"] == -15 and "weather" in dd[FRI]["sources"]
    assert "rain forecast (80%)" in dd[FRI]["reasons"][0] and "measured 4 times" in dd[FRI]["reasons"][0]
    assert dd[FRI]["projected_sales"] == 4250.0
    stale = econ.date_demand(rid, WEEK, weather=[{"date": FRI, "precip_pct": 80, "stale": True}],
                             today=date(2026, 10, 3), db_path=db_path)
    assert stale[FRI]["ratio"] == 1.0, "a stale copy of the forecast never moves a date"
    dry = econ.date_demand(rid, WEEK, weather=[{"date": FRI, "precip_pct": 30, "stale": False}],
                           today=date(2026, 10, 3), db_path=db_path)
    assert dry[FRI]["ratio"] == 1.0


def test_the_owners_budget_for_the_night_is_the_dates_demand(db_path):
    rid = _rid()
    _history(rid, [date(2026, 10, 2) - timedelta(weeks=k) for k in range(4)], sales=5000.0)
    _exec("INSERT INTO dsr_budgets (restaurant_id, business_date, gross, net) VALUES (?,?,?,?)",
          (rid, FRI, 7000.0, 6500.0))
    dd = econ.date_demand(rid, WEEK, today=date(2026, 10, 3), db_path=db_path)
    assert dd[FRI]["ratio"] == 1.3 and dd[FRI]["sources"] == ["budget"]
    assert "your budget for the night, $6,500 against a typical Friday's $5,000" in dd[FRI]["reasons"]


def test_one_number_per_date_reaches_every_reader(db_path):
    rid = _rid()
    _history(rid, [date(2026, 10, 2) - timedelta(weeks=k) for k in range(4)], sales=5000.0)
    _rain_record(rid)
    signals = {FRI: {"lift_pct": 30, "covers": None, "labels": ["Homecoming"]}}
    hol, dd = schedule_engine._merge_date_demand(rid, WEEK, signals,
                                                 weather=[{"date": FRI, "precip_pct": 70, "stale": False}])
    # +30% the owner's, -15% the rain: one figure, multiplied, never added twice.
    assert dd[FRI]["ratio"] == round(1.3 * 0.85, 3) and signals[FRI]["lift_pct"] == 10
    assert signals[FRI]["signal_lift_pct"] == 30 and "Rain forecast (70%)" in signals[FRI]["labels"]
    import demand_signals
    block = demand_signals.prompt_block(signals, WEEK)
    assert "expect about 10% more than a typical Friday" in block and "rain forecast (70%)" in block


# ── D-32: the late segment ───────────────────────────────────────────────

def _late_history():
    rows = []
    for f in ("2026-09-18", "2026-09-25", "2026-10-02"):
        rows += [{"date": f, "employee": "Bea", "role": "Bartender", "shift_start": "6:00pm", "shift_end": "2:00am"},
                 {"date": f, "employee": "Cal", "role": "Bartender", "shift_start": "5:00pm", "shift_end": "11:00pm"},
                 {"date": f, "employee": "Dee", "role": "Barback", "shift_start": "9:00pm", "shift_end": "2:00am"},
                 {"date": f, "employee": "Eve", "role": "Server", "shift_start": "5:00pm", "shift_end": "10:30pm"}]
    return rows


def test_a_late_closing_night_has_its_own_usual_crew_and_row():
    pats = labor.historical_patterns(_late_history(), close_times={"Friday": "2:00am"})
    assert pats["late_headcount"]["Friday"] == {"Bartender": 1, "Barback": 1}
    assert pats["typical_headcount"][("Friday", "night")] == {"Bartender": 2, "Barback": 1, "Server": 1}
    rows = req.shift_requirements([FRI], typical_headcount=pats["typical_headcount"],
                                  late_headcount=pats["late_headcount"], close_times={"Friday": "2:00am"},
                                  date_demand={FRI: {"ratio": 1.4, "reasons": ["Homecoming"]}})
    late = next(r for r in rows if r["daypart"] == "late")
    assert late["window"] == [22 * 60, 26 * 60]
    assert {x["role"]: x["required"] for x in late["roles"]} == {"Bartender": 1, "Barback": 1}
    assert "late night 10:00pm-2:00am | Barback 1, Bartender 1" in req.requirements_block(rows)
    assert req.requirements_map(rows)[(FRI, "late")] == {"Bartender": 1, "Barback": 1}
    # An 11pm close has no late segment.
    assert not [r for r in req.shift_requirements([FRI], typical_headcount=pats["typical_headcount"],
                                                  late_headcount=pats["late_headcount"],
                                                  close_times={"Friday": "11:00pm"}) if r["daypart"] == "late"]


def test_the_score_holds_the_late_crew_from_ten_to_close():
    draft = [{"date": FRI, "day": "Friday", "employee": "Bea", "role": "Bartender", "shift_start": "6:00pm",
              "shift_end": "11:00pm", "scheduled_hours": "5"},
             {"date": FRI, "day": "Friday", "employee": "Cal", "role": "Bartender", "shift_start": "5:00pm",
              "shift_end": "11:00pm", "scheduled_hours": "6"}]
    rmap = {(FRI, "night"): {"Bartender": 2}, (FRI, "late"): {"Bartender": 1}}
    ctxs = sq.build_contexts(draft, requirements_by_date=rmap, close_times={"Friday": "2:00am"})
    night = next(c for c in ctxs if c.daypart == "night")
    assert night.late_window == (22 * 60, 26 * 60) and night.late_required == {"Bartender": 1}
    res = sq.dim_coverage_curve(night)
    assert any("usually on late at night" in w for w in res.weaknesses)
    assert res.facts["gaps"]["Bartender"]["minutes_short"] == 180          # 11pm to 2am
    draft[0]["shift_end"] = "2:00am"
    draft[0]["scheduled_hours"] = "8"
    ok = sq.dim_coverage_curve(next(c for c in sq.build_contexts(draft, requirements_by_date=rmap,
                                                                close_times={"Friday": "2:00am"})
                                    if c.daypart == "night"))
    assert not ok.facts["gaps"]


def test_the_late_segment_has_its_own_sales_per_labor_hour():
    draft = [{"date": FRI, "day": "Friday", "employee": f"B{i}", "role": "Bartender", "shift_start": "6:00pm",
              "shift_end": "2:00am", "scheduled_hours": "8"} for i in range(3)]
    ctxs = sq.build_contexts(draft, close_times={"Friday": "2:00am"},
                             splh_targets={"Friday": {"night": 100.0, "late": 100.0}},
                             daypart_sales={"Friday": {"night": 3000.0, "late": 400.0}})
    night = next(c for c in ctxs if c.daypart == "night")
    assert night.late_hours == 12.0 and night.late_expected_sales == 400.0
    res = sq.dim_splh(night)
    assert res.facts["late"]["splh"] == 33.0 and any("Late night" in w for w in res.weaknesses)


def test_late_sales_and_hours_reach_the_objective(db_path):
    rid = _rid()
    today = date(2026, 10, 3)
    fridays = [today - timedelta(days=d) for d in (1, 8, 15)]
    _history(rid, fridays, sales=4000.0, hours=40.0)
    for k, f in enumerate(fridays):
        _ticket(rid, f"n{k}", f.isoformat(), f"{f.isoformat()}T19:00:00", 3000.0)
        _ticket(rid, f"l{k}", f.isoformat(), f"{f.isoformat()}T23:00:00", 1000.0)
    rows = ["date,day,employee,role,shift_start,shift_end,scheduled_hours,actual_hours,sales,notes"]
    for f in fridays:
        rows.append(f"{f.isoformat()},Friday,Bea,Bartender,18:00,02:00,8,8,4000,")
    models.save_client_data(rid, "shifts", "\n".join(rows) + "\n", source="upload", db_path=db_path)
    fri = econ.splh_by_daypart(rid, today=today, db_path=db_path)["Friday"]
    assert fri["late"]["sales"] == 1000.0 and fri["late"]["hours"] == 20.0     # 4 of 8 hours from 10pm


# ── D-33: freshness gates the generation ─────────────────────────────────

def test_stale_sales_stop_a_generation_but_closed_days_are_not_stale(db_path, monkeypatch):
    rid = _rid()
    today = date(2026, 10, 3)
    last = today - timedelta(days=20)
    _history(rid, [last])
    out = schedule_engine._demand_data_through(rid, today=today)
    assert out["blocked"] and out["trading_days_ago"] == 19 and "9/13/26" in out["message"]
    # Closed for a refit: the closed dates are not missing sales.
    import schedule_rules as sr
    sr.save_closures(rid, closed_dates=[(last + timedelta(days=k)).isoformat() for k in range(1, 18)],
                     db_path=db_path)
    out = schedule_engine._demand_data_through(rid, today=today)
    assert not out["blocked"] and out["trading_days_ago"] == 2


def test_the_generation_refuses_on_stale_sales_with_the_owners_words(db_path, monkeypatch):
    rid = _rid()
    rows = ["date,day,employee,role,shift_start,shift_end,scheduled_hours,actual_hours,sales,notes"]
    old = date.today() - timedelta(days=40)
    rows.append(f"{old.isoformat()},X,Ana,Server,11:00,15:00,4,4,4000,")
    models.save_client_data(rid, "shifts", "\n".join(rows) + "\n", source="upload", db_path=db_path)
    _history(rid, [old])
    with pytest.raises(schedule_engine.ScheduleGenerationError) as e:
        schedule_engine._build_schedule_result(rid)
    assert "Reconnect the POS or upload recent sales" in str(e.value)


def test_the_demand_forecast_window_is_the_restaurants_own_date(db_path):
    rid = _rid()
    today = date(2026, 10, 3)
    days = [today - timedelta(days=k) for k in range(1, 22)]
    _history(rid, days)
    _history(rid, [today - timedelta(days=70)], sales=99999.0)            # outside the 8 weeks
    fc = labor.build_demand_forecast(rid, today=today)
    assert fc["ok"] and fc["data_through"] == days[0].isoformat()
    assert all(d["median_sales"] == 4000.0 for d in fc["days"])

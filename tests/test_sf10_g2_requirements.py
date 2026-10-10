"""Scheduler audit 10/10/26, workstream G2 — requirements from what works,
not from habit.

RM-17 / LEARNING-4 / LOGIC-10 / LOGIC-12  the requirement is the meal's work
          at each half hour ÷ the labor standard at the RUSH (shrunk toward a
          prior; the owner's own wins) plus a fixed crew — never the usual
          crew x one clamped ratio; history is the fallback.
LOGIC-11  each role on its own lead or lag; the half-hour peak is the usual
          people ON AT ONCE, not 0.75 x different people.
LOGIC-9   demand per meal and per role, wider bounds where measured.
LEARNING-19 / RM-19 / MARKET-11  a backtested forecast with trend and season,
          calibrated intervals, a busy shift staffed to the upper forecast; a
          half-hour curve measured from the tickets.
LEARNING-20  an event's lift per role.     LEARNING-21  weather per meal.
LEARNING-5   Cavnar's own trims never the usual crew without evidence.
LEARNING-11  each date's own daypart split; the weekday share is "estimated".
RM-18 / LEARNING-14  expected attendance, a standby requirement, graded cost.
RM-20 / LEARNING-6 / MARKET-23  what one more person is worth, with a CI.
LEARNING-26  partial-pooled productivity, disagreements, owner-only.
LEARNING-27  cohort priors behind MIN_COHORT.
MARKET-33    earned hours and the floors the work never needed.
"""
import json
import math
import random
from datetime import date, timedelta

import pytest

import models


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


def _rid(name="Driver Bar", **kw):
    return models.create_restaurant(models.Restaurant(name=name, owner_email=f"{name.split()[0].lower()}@x.test",
                                                      timezone="America/Chicago", **kw))


# ── RM-19 / LEARNING-19 / MARKET-11: the backtested forecast ──────────────

def _series(today, weeks=60, growth=0.0, seasonal=None, noise=0.04, seed=3):
    rnd = random.Random(seed)
    out = {}
    for i in range(1, weeks * 7 + 1):
        d = today - timedelta(days=i)
        wk = [1.0, 0.9, 1.0, 1.1, 1.5, 1.7, 1.2][d.weekday()]
        s = (seasonal(d) if seasonal else 1.0)
        out[d] = 1000 * wk * s * math.exp(growth * (-i / 7.0)) * (1 + rnd.gauss(0, noise))
    return out


def test_a_growing_restaurant_is_forecast_from_its_trend_not_the_lagging_median():
    import demand_model as dm
    today = date(2026, 10, 10)
    got = dm.forecast_from_series(_series(today, weeks=20, growth=0.02), [today + timedelta(days=6)], today)
    assert got["choice"]["estimator"] == "trend" and got["choice"]["skill"] >= dm.MIN_SKILL
    e = got["dates"][(today + timedelta(days=6)).isoformat()]
    # The median lags ~5 weeks of 2%/week growth: the forecast moves it ~10%+.
    assert e["ratio"] > 1.07 and e["quantiles"] and e["quantiles"][0.1] < e["p50"] < e["quantiles"][0.9]
    assert e["busy_ratio"] >= e["ratio"]


def test_the_weekday_median_stands_when_nothing_beats_it():
    import demand_model as dm
    today = date(2026, 10, 10)
    got = dm.forecast_from_series(_series(today, weeks=20, growth=0.0, noise=0.08), [today + timedelta(days=3)],
                                  today)
    assert got["choice"]["estimator"] == "naive" and got["choice"]["reason"]
    assert abs(got["dates"][(today + timedelta(days=3)).isoformat()]["ratio"] - 1.0) < 0.06


def test_last_years_season_turns_the_forecast_at_the_season_turn():
    import demand_model as dm
    today = date(2026, 4, 20)
    # A patio spring: every year from mid-April sales jump 30%.
    spring = lambda d: 1.3 if (d.month, d.day) >= (4, 15) and d.month <= 9 else 1.0  # noqa: E731
    s = _series(today, weeks=110, seasonal=spring, noise=0.03)
    got = dm.forecast_from_series(s, [today + timedelta(days=7)], today)
    assert got["choice"]["estimator"] in ("seasonal", "trend")
    assert got["dates"][(today + timedelta(days=7)).isoformat()]["ratio"] > 1.08


def test_too_few_nights_state_no_interval():
    import demand_model as dm
    today = date(2026, 10, 10)
    s = {d: v for d, v in _series(today, weeks=4).items()}
    e = dm.forecast_from_series(s, [today + timedelta(days=2)], today)["dates"]
    assert all(x["quantiles"] is None for x in e.values())


def test_date_demand_carries_the_forecasts_trend_and_says_so(db_path):
    import schedule_economics as econ
    rid = _rid()
    today = date(2026, 10, 10)
    for d, v in _series(today, weeks=20, growth=0.02).items():
        _exec("INSERT INTO labor_daily_history (restaurant_id, date, day_of_week, sales, total_hours, labor_pct) "
              "VALUES (?,?,?,?,?,?)", (rid, d.isoformat(), d.strftime("%A"), v, 50, 30))
    dd = econ.date_demand(rid, ["2026-10-16"], today=today, db_path=db_path)["2026-10-16"]
    assert "forecast" in dd["sources"] and dd["ratio"] > 1.05
    assert any("the forecast" in r for r in dd["reasons"]) and dd["forecast"]["estimator"] == "trend"
    assert dd["parts"]["night"]["busy_ratio"] >= dd["parts"]["night"]["ratio"]


def test_a_half_hour_curve_is_measured_from_the_tickets_not_interpolated(db_path):
    import schedule_economics as econ
    rid = _rid()
    today = date(2026, 10, 10)
    for k in range(1, 4):
        d = (today - timedelta(weeks=k)).isoformat()
        # All of 6pm's sales in its second half: an hourly curve cannot say so.
        for i, (at, net) in enumerate((("18:40", 900.0), ("18:50", 100.0), ("19:10", 500.0))):
            _exec("INSERT INTO pos_tickets (restaurant_id, provider, ticket_id, business_date, opened_at, net_sales) "
                  "VALUES (?,?,?,?,?,?)", (rid, "rpower", f"{d}-{i}", d, f"{d}T{at}:00", net))
    hc = econ.measured_half_hours(rid, today=today, db_path=db_path)["Saturday"]
    assert hc.get(18 * 60 + 30) == pytest.approx(1000 / 1500, rel=1e-3) and 18 * 60 not in hc


# ── LOGIC-9 / LEARNING-19 / LEARNING-20: per meal, per role ───────────────

def test_a_dinner_occasion_moves_dinner_and_each_role_by_its_own_record():
    import schedule_requirements as req
    typical = {("Friday", "morning"): {"Server": 3, "Cook": 2}, ("Friday", "night"): {"Server": 4, "Cook": 3,
                                                                                    "Bartender": 2}}
    dd = {"2026-10-16": {"ratio": 1.2, "pct": 20, "reasons": ["Cubs game: +35%"], "sources": ["signals"],
                         "parts": {"morning": {"ratio": 1.0, "busy_ratio": 1.0, "roles": {}, "measured": False,
                                               "reasons": []},
                                   "night": {"ratio": 1.35, "busy_ratio": 1.35, "measured": True, "reasons": [],
                                             "roles": {"bartender": 1.9, "cook": 1.0}}}}}
    rows = req.shift_requirements(["2026-10-16"], typical_headcount=typical, date_demand=dd)
    lunch = {x["role"]: x["required"] for x in next(r for r in rows if r["daypart"] == "morning")["roles"]}
    dinner = {x["role"]: x for x in next(r for r in rows if r["daypart"] == "night")["roles"]}
    assert lunch == {"Server": 3, "Cook": 2}, "a dinner game added lunch staff"
    assert dinner["Server"]["required"] == 5 and dinner["Cook"]["required"] == 3
    # Measured: the wider bounds (an estimate stops at x1.4).
    assert dinner["Bartender"]["required"] == 4 and "own record" in dinner["Bartender"]["reason"]


def test_measured_demand_is_not_clipped_like_an_estimate():
    import schedule_requirements as req
    assert req.demand_factor(1.8) == req.DEMAND_SCALE_BOUNDS[1]
    assert req.demand_factor(1.8, measured=True) == 1.8
    assert req.demand_factor(0.5, measured=True) == 0.5 and req.demand_factor(0.5) == 0.75


def test_a_busy_shift_is_staffed_to_the_upper_forecast():
    import schedule_requirements as req
    dd = {"parts": {"night": {"ratio": 1.0, "busy_ratio": 1.2, "roles": {}, "measured": True, "reasons": []}}}
    r, _roles, measured, why = req.part_demand(dd, "night", busy=True)
    assert r == 1.2 and measured and any("upper forecast" in w for w in why)
    assert req.part_demand(dd, "night", busy=False)[0] == 1.0


def test_a_games_lift_falls_on_the_meal_its_kickoff_is_in(monkeypatch):
    import schedule_economics as econ
    import demand_drivers as ddr
    ctx = econ._DemandContext.__new__(econ._DemandContext)
    ctx.rid, ctx.db, ctx.today, ctx._memo, ctx._weather_rows = 1, models.DB_PATH, None, {}, []
    monkeypatch.setattr(ddr, "event_parts", lambda *a, **k: {"night"})
    monkeypatch.setattr(ctx, "label_split", lambda *a, **k: None)
    monkeypatch.setattr(ctx, "event_labels", lambda d: [])
    monkeypatch.setattr(ctx, "morning_share", lambda wd: 0.4)
    split = ctx.event_split("2026-10-16", 1.3)
    assert split["morning"] == 1.0 and split["night"] == pytest.approx(1.5)


def test_an_events_lift_per_role_is_its_own_record_shrunk_toward_the_night(monkeypatch):
    import demand_drivers as ddr
    import event_memory as em
    nights = ["2026-09-04", "2026-09-11", "2026-09-18", "2026-09-25"]
    monkeypatch.setattr(em, "measured_effect", lambda *a, **k: {"dates": nights, "median_lift_pct": 22})
    rows = []
    for d in nights:
        day = date.fromisoformat(d)
        for k in range(1, 6):
            o = (day - timedelta(weeks=k)).isoformat()
            rows += [{"date": o, "employee": f"bar{i}", "role": "Bartender"} for i in range(2)]
            rows += [{"date": o, "employee": f"cook{i}", "role": "Cook"} for i in range(3)]
        rows += [{"date": d, "employee": f"bar{i}", "role": "Bartender"} for i in range(4)]
        rows += [{"date": d, "employee": f"cook{i}", "role": "Cook"} for i in range(3)]
    got = ddr.role_event_lifts(1, "cubs", 22, rows=rows, family_prior={})
    assert got["bartender"]["raw"] == 2.0 and got["cook"]["raw"] == 1.0
    # Shrunk toward the night's +22% by ROLE_SHRINK nights.
    assert 1.22 < got["bartender"]["ratio"] < 2.0 and 1.0 < got["cook"]["ratio"] < 1.22


# ── LEARNING-21: weather per meal ─────────────────────────────────────────

def test_the_nws_day_and_night_periods_are_lunch_and_dinner():
    import demand_drivers as ddr
    fp = ddr.forecast_parts([{"date": "2026-10-16", "precip_pct": 70, "short_forecast": "Rain Showers",
                              "high_f": 62, "night_precip_pct": 10, "low_f": 38, "night_short_forecast": "Clear",
                              "stale": False}])["2026-10-16"]
    assert ddr.conditions_of(fp["morning"], "morning") == {"rain"}
    assert ddr.conditions_of(fp["night"], "night") == {"cold"}


def test_weather_is_learned_per_meal_and_band(db_path):
    import demand_drivers as ddr
    today = date(2026, 10, 10)
    series, observed = {}, {}
    for i in range(1, 300):
        d = (today - timedelta(days=i)).isoformat()
        cold = i % 11 == 3
        series[d] = {"morning": 1000.0, "night": 1400.0 if cold else 2000.0}
        observed[d] = {"rain": 0, "lunch_wet": 0, "dinner_wet": 0, "lunch_temp_f": 60 if not cold else 50,
                       "dinner_temp_f": 35 if cold else 65, "snow": 0}
    eff = ddr.weather_effects(1, today=today, series=series, observed=observed)
    assert eff["night"]["cold"]["applies"] and eff["night"]["cold"]["lift_pct"] == pytest.approx(-30.0)
    assert "cold" not in (eff.get("morning") or {}), "a cold dinner taught lunch"
    f, items, used = ddr.weather_factor({"night": {"rain": 0, "high_f": 30}}, eff, "night")
    assert used == ["cold"] and f == pytest.approx(0.7)
    assert ddr.weather_factor({"morning": {"rain": 0, "high_f": 30}}, eff, "morning")[2] == []


def test_the_observed_day_is_kept_by_meal():
    import weather
    from datetime import datetime as dt, timezone
    from zoneinfo import ZoneInfo
    tz = ZoneInfo("America/Chicago")
    day = date(2026, 10, 9)
    obs = []
    for h in range(11, 23):
        at = dt(2026, 10, 9, h, 0, tzinfo=tz).astimezone(timezone.utc)
        obs.append({"at": at, "temp_f": 70.0 if h < 15 else 45.0, "precip_mm": None, "wet": h < 14,
                    "text": "Rain" if h < 14 else "Clear"})
    s = weather.summarise_day(obs, tz, day)
    assert s["lunch_wet"] == 1 and s["dinner_wet"] == 0 and s["dinner_temp_f"] == 45.0 and s["snow"] == 0


# ── RM-17 / LEARNING-4 / LOGIC-10 / LOGIC-12: the work at the rush ────────

def _model_inputs(weeks=6, mon_servers=5, busy_servers=4):
    """Fridays: 40 guests in the 7pm half hour carried by 4 servers (10 a
    server-hour... 20/half hour at rush); Mondays: 10 guests at the rush
    with 5 servers on — a restaurant that always overstaffs Mondays."""
    import staffing_model as sm
    work, people = {}, {}
    base = date(2026, 8, 3)                                  # a Monday
    for k in range(weeks):
        for off, servers, peak in ((0, mon_servers, 10), (4, busy_servers, 40)):
            d = (base + timedelta(weeks=k, days=off)).isoformat()
            g = {17 * 60 + 30: peak // 4, 18 * 60: peak // 2, 18 * 60 + 30: peak, 19 * 60: peak // 2,
                 19 * 60 + 30: peak // 4}
            work[d] = {"guests": dict(g), "tickets": {m: v // 2 for m, v in g.items()}, "bar tickets": {}}
            people[d] = {"server": {m: servers for m in range(17 * 60, 21 * 60, 30)}}
    return sm, work, people


def test_the_standard_is_what_a_server_carries_at_the_rush_and_sizes_the_peak_half_hour():
    sm, work, people = _model_inputs()
    model = sm._build(work, people, sorted(work), None, set(), {}, {})
    std = model["families"]["server"]["parts"]["night"]
    # Median over the dates: Fridays 40/half hour x 2 / 4 = 20 a server-hour,
    # Mondays 10 x 2 / 5 = 4 — the median of 6 and 6 is 12.
    assert std["source"] == "measured" and std["std"] == pytest.approx(12.0)
    need = sm.needs(model, "Friday", "night", {"Server": 1.0}, {"Server": 4}, {"Server": {"peak": 4}})["Server"]
    # The 6:30pm half hour's 40 guests at 12 a server-hour: ceil(40 / 6) = 7 on
    # at once — the night's AVERAGE would ask for far fewer (LOGIC-12).
    assert need["half"][18 * 60 + 30] == 7 and need["peak"] == 7
    assert "busiest half hour" in need["reason"]


def test_a_monday_the_restaurant_always_overstaffed_is_asked_for_what_its_work_needs():
    sm, work, people = _model_inputs()
    model = sm._build(work, people, sorted(work), None, set(), {}, {})
    mon = sm.needs(model, "Monday", "night", {"Server": 1.0}, {"Server": 5}, {"Server": {"peak": 5}})["Server"]
    # 10 guests at the rush ÷ (12 x 0.5) = 2 on at once; the fixed crew is
    # the fewest the restaurant runs ANY meal on (its Fridays: 4) — never
    # Monday's own habit of 5.
    assert mon["peak"] == 4 and mon["distinct"] == 4
    # Once a few quiet nights show the meal running on 2, Monday is its work.
    for k in range(2):
        d = (date(2026, 8, 4) + timedelta(weeks=k)).isoformat()            # Tuesdays
        work[d] = {"guests": {18 * 60 + 30: 8}, "tickets": {18 * 60 + 30: 4}, "bar tickets": {}}
        people[d] = {"server": {m: 2 for m in range(17 * 60, 21 * 60, 30)}}
    model = sm._build(work, people, sorted(work), None, set(), {}, {})
    assert model["families"]["server"]["parts"]["night"]["fixed"] == 2
    mon = sm.needs(model, "Monday", "night", {"Server": 1.0}, {"Server": 5}, {"Server": {"peak": 5}})["Server"]
    # The standard is now the median of 4, 8 and 20 a server-hour (8): 10
    # guests ÷ 4 = 3 on at once, above the fixed crew of 2.
    assert mon["peak"] == 3


def test_the_owners_own_standard_wins_and_a_role_with_none_keeps_its_history():
    import schedule_requirements as req
    sm, work, people = _model_inputs()
    model = sm._build(work, people, sorted(work), None, set(), {"server": {"night": 20.0}}, {})
    assert model["families"]["server"]["parts"]["night"]["source"] == "yours"
    rows = req.shift_requirements(["2026-10-16"], typical_headcount={("Friday", "night"): {"Server": 4, "Dish": 2}},
                                  staffing_model=model, concurrent={("Friday", "night"): {"Server": {"peak": 4}}})
    night = {x["role"]: x for x in next(r for r in rows if r["daypart"] == "night")["roles"]}
    # 40 guests ÷ (20 x 0.5) = 4; the dish pit has no standard — its usual 2.
    assert night["Server"]["required"] == 4 and "your standard" in night["Server"]["reason"]
    assert night["Dish"]["required"] == 2
    row = next(r for r in rows if r["daypart"] == "night")
    assert row["half_need"][18 * 60 + 30]["Server"] == 4 and row.get("driven") == ["Server"]


def test_a_prior_pulls_a_thin_record_toward_the_cohort():
    sm, work, people = _model_inputs(weeks=2)
    model = sm._build(work, people, sorted(work), None, set(), {}, {"server": {"night": 6.0}})
    e = model["families"]["server"]["parts"]["night"]
    assert e["prior"] == 6.0 and 6.0 < e["std"] < e["own"]


def test_a_cavnar_era_night_cannot_raise_the_standard_without_outcome_evidence():
    import requirement_provenance as prov
    vals = {"2026-08-01": 10.0, "2026-08-08": 10.0, "2026-09-05": 18.0, "2026-09-12": 18.0}
    capped = prov.cap_unvalidated(vals, "2026-09-01")
    assert capped["2026-09-05"] == 10.0 and capped["2026-08-01"] == 10.0
    assert prov.cap_unvalidated(vals, "2026-09-01", {("2026-09-05", "night")}, part="night")["2026-09-05"] == 18.0


# ── LOGIC-11: lead and lag, people on at once ─────────────────────────────

def test_cooks_lead_the_rush_and_dish_follows_it():
    import staffing_model as sm
    work, people = {}, {}
    for k in range(8):
        d = (date(2026, 8, 7) + timedelta(weeks=k)).isoformat()
        curve = {17 * 60: 2, 17 * 60 + 30: 6, 18 * 60: 12, 18 * 60 + 30: 20, 19 * 60: 10, 19 * 60 + 30: 4, 20 * 60: 2}
        work[d] = {"tickets": curve, "guests": curve, "bar tickets": {}}
        people[d] = {"line_cook": {m - 30: max(1, v // 4) for m, v in curve.items()},
                     "dish": {m + 30: max(1, v // 5) for m, v in curve.items()}}
    dates = sorted(work)
    assert sm.measure_lag(work, people, "line_cook", "tickets", dates)["lag"] == 30
    assert sm.measure_lag(work, people, "dish", "tickets", dates)["lag"] == -30


def test_the_half_hour_peak_is_the_usual_people_on_at_once():
    import staffing_curve as sc
    curve = {17: 0.1, 18: 0.4, 19: 0.3, 20: 0.2}
    # Six different servers a night, never more than four on at once.
    old = sc.half_hour_needs(curve, {"Server": 6}, 17 * 60, 21 * 60)
    new = sc.half_hour_needs(curve, {"Server": 6}, 17 * 60, 21 * 60, peaks={"Server": 4})
    assert max(n["Server"] for n in old.values()) == 5          # 0.75 x 6, rounded up
    assert max(n["Server"] for n in new.values()) == 4
    # A cook half an hour ahead: needed at its peak before the sales peak.
    led = sc.half_hour_needs(curve, {"Cook": 4}, 17 * 60, 21 * 60, peaks={"Cook": 4}, offsets={"Cook": 30})
    top = max(led, key=lambda m: led[m]["Cook"])
    flat = sc.half_hour_needs(curve, {"Cook": 4}, 17 * 60, 21 * 60, peaks={"Cook": 4})
    assert top < max(flat, key=lambda m: flat[m]["Cook"])


def test_the_score_judges_the_half_hours_the_requirements_carry():
    import shift_quality as sq
    ctx = sq.ShiftContext.__new__(sq.ShiftContext)
    ctx.half_hour_needs = {18 * 60: {"Server": 3}, 21 * 60: {"Server": 1}}
    ctx.demand_curve, ctx.typical_headcount, ctx.concurrent_peaks, ctx.daypart = {}, {}, {}, "night"
    assert sq._half_hour_requirement(ctx, 17 * 60, 20 * 60) == {18 * 60: {"Server": 3}}


def test_the_passes_fill_to_the_table_the_model_was_given():
    import schedule_engine as se
    stored = [{"date": "2026-10-16", "daypart": "night", "roles": [{"role": "Server", "required": 6}]},
              {"date": "2026-10-17", "daypart": "night", "roles": [{"role": "Server", "required": 4}]}]
    got = se.week_requirements(1, {"requirements": stored, "closed_dates": ["2026-10-17"], "week_dates": []},
                               constraints=None)
    assert got == stored[:1]


# ── LEARNING-5: the baseline never learns Cavnar's own trims ─────────────

def test_a_cavnar_trim_is_held_until_its_nights_ran_no_worse():
    import requirement_provenance as prov
    t = {"direction": "down", "now": 2, "was": 3, "since": "2026-09-08", "part": "morning",
         "recent_dates": ["2026-09-08", "2026-09-15", "2026-09-22", "2026-09-29"]}
    held = prov.hold_down_trend(t, "2026-09-01", restaurant_id=1, check=lambda *a, **k: {"ok": False, "read": 1})
    assert held["hold"] and held["provenance"] == "trim" and "held at 3" in held["reason"]
    ok = prov.hold_down_trend(t, "2026-09-01", restaurant_id=1,
                              check=lambda *a, **k: {"ok": True, "read": 4, "well": 3})
    assert not ok["hold"] and ok["provenance"] == "validated"
    assert prov.hold_down_trend(dict(t, since="2026-08-01"), "2026-09-01")["provenance"] == "pre_cavnar"
    assert not prov.hold_down_trend(dict(t, direction="up"), "2026-09-01")["hold"]


def test_the_usual_crew_keeps_its_level_through_an_unvalidated_trim():
    import labor
    rows = []
    start = date(2026, 7, 7)                                      # Tuesdays
    for k in range(8):
        d = (start + timedelta(weeks=k)).isoformat()
        n = 3 if k < 4 else 2
        rows += [{"date": d, "employee": f"S{i}", "role": "Server", "shift_start": "11:00am",
                  "shift_end": "3:00pm"} for i in range(n)]
    free = labor.historical_patterns(rows)
    assert free["typical_headcount"][("Tuesday", "morning")]["Server"] == 2
    guarded = labor.historical_patterns(rows, trend_guard=lambda t: {"hold": True, "provenance": "trim",
                                                                     "reason": "held at 3"})
    assert guarded["typical_headcount"][("Tuesday", "morning")]["Server"] == 3
    assert guarded["headcount_provenance"][("Tuesday", "morning")]["Server"] == "trim_held"


# ── LEARNING-11: each date's own split ────────────────────────────────────

def test_each_date_is_split_by_its_own_tickets_first():
    import demand_drivers as ddr
    m, n = ddr.split_hours({11: 300.0, 12: 400.0, 19: 300.0, 25: 100.0})
    assert (m, n) == (700.0, 400.0)


# ── RM-18 / LEARNING-14: expected attendance ─────────────────────────────

def test_the_chance_a_shift_comes_up_short_is_exact():
    import attendance_risk as ar
    assert ar.p_short([0.5, 0.5], 2) == 0.75
    assert ar.p_short([0.18] * 5, 5) == pytest.approx(1 - 0.82 ** 5, abs=1e-4)
    assert ar.p_short([0.1], 0) == 0.0


def test_reliability_is_graded_not_a_cliff():
    import attendance_risk as ar
    assert ar.exposure(0.05) == 0.0 and ar.exposure(0.15) == 0.5 and ar.exposure(0.19) == 0.9
    assert ar.exposure(0.25) == 1.0


def test_weekday_rates_shrink_through_the_hierarchy():
    import attendance_risk as ar
    tally = {"Ana": {"w": 30.0, "w_absent": 3.0, "shifts": 30,
                     "days": {"Saturday": {"w": 6.0, "w_absent": 2.0}, "Tuesday": {"w": 6.0, "w_absent": 0.0}}}}
    r = ar.rates_from_tally(tally, 0.05, cohort=None)
    p = r["people"]["ana"]
    assert p["days"]["Saturday"] > p["all"] > p["days"]["Tuesday"]
    assert ar.p_for(r, "Nobody", "Friday") == r["base"]


def test_a_busy_shift_likely_short_needs_a_standby():
    import attendance_risk as ar
    rows = [{"date": "2026-10-16", "employee": n, "role": "Server", "shift_start": "5:00pm", "shift_end": "11:00pm"}
            for n in ("A", "B", "C", "D")]
    reqs = [{"date": "2026-10-16", "daypart": "night", "roles": [{"role": "Server", "required": 4}]}]
    r = {"base": 0.05, "people": {n.lower(): {"all": 0.2, "days": {"Friday": 0.2}} for n in "ABCD"}}
    risks = ar.shift_risks(rows, reqs, r, busy={("2026-10-16", "night")})
    assert risks[0]["expected"] == 3.2 and risks[0]["p_short"] == pytest.approx(1 - 0.8 ** 4, abs=1e-4)
    assert ar.standby_needed(risks) and "chance fewer than 4 servers turn up" in ar.words(risks[0])


def test_lateness_is_graded_too():
    import attendance_risk as ar
    import shift_quality as sq
    assert ar.late_exposure(0.05) == 0.0 and ar.late_exposure(0.2) == pytest.approx(0.667, abs=1e-3)
    assert ar.late_exposure(0.3) == 1.0
    rows = [{"date": "2026-10-13", "day": "Tuesday", "employee": n, "role": "Server", "shift_start": s,
             "shift_end": e} for n, s, e in (("Ana", "10:00am", "3:00pm"), ("Bo", "11:00am", "3:30pm"))]

    def score(rate):
        rel = {"Ana": {"no_show_rate": 0.0, "late_rate": rate, "late_risk": rate >= 0.25, "shifts": 30},
               "Bo": {"no_show_rate": 0.0, "late_rate": 0.0, "shifts": 30}}
        ctx = sq.ShiftContext(date="2026-10-13", day="Tuesday", daypart="morning", rows=rows, day_rows=rows,
                              profile=sq.profile_for_shift("Tuesday", "morning"), reliability=rel)
        return sq.dim_reliability(ctx).score
    # 19% late used to weigh nothing and 25% everything.
    assert score(0.0) > score(0.19) > score(0.3)


def test_the_score_reads_the_shifts_own_weekday():
    import shift_quality as sq
    rel = {"Ana": {"no_show_rate": 0.1, "weekday_rates": {"Saturday": 0.35}, "shifts": 30}}
    rows = [{"date": "2026-10-17", "day": "Saturday", "employee": "Ana", "role": "Bartender",
             "shift_start": "5:00pm", "shift_end": "11:00pm"}]
    ctx = sq.ShiftContext(date="2026-10-17", day="Saturday", daypart="night", rows=rows,
                          profile=sq.profile_for_shift("Saturday", "night"), reliability=rel)
    got = sq.dim_reliability(ctx)
    assert got.facts["exposed"] and "their Saturday shifts" in got.weaknesses[0]
    ctx_tue = sq.ShiftContext(date="2026-10-13", day="Tuesday", daypart="night",
                              rows=[dict(rows[0], date="2026-10-13", day="Tuesday")],
                              profile=sq.profile_for_shift("Tuesday", "night"), reliability=rel)
    assert not sq.dim_reliability(ctx_tue).facts["exposed"]


# ── RM-20 / LEARNING-6 / MARKET-23: what one more person is worth ────────

def _units(effect=0.12, n_weeks=20, absences=6):
    rnd = random.Random(5)
    out = []
    base = date(2026, 4, 3)
    for k in range(n_weeks):
        d = base + timedelta(weeks=k)
        absent = k % (n_weeks // absences) == 0
        people = 4 - (1 if absent else 0)
        out.append({"date": d.isoformat(), "weekday": "Friday", "people": people, "fc": math.log(5000),
                    "absent": absent, "typical": 5000,
                    "y": {"sales": -effect * (1 if absent else 0) + rnd.gauss(0, 0.02), "comps": None, "turn": None}})
    return out


def test_uncovered_no_shows_measure_one_more_person():
    import staffing_elasticity as se
    got = se.natural_experiment(_units(), "sales")
    assert got["n"] >= se.NAT_MIN and got["lo"] > 0.05 and got["est"] == pytest.approx(0.12, abs=0.03)
    assert se.natural_experiment(_units(absences=2), "sales") is None, "below the floor nothing is said"


def test_a_requirement_moves_only_when_the_interval_clears_zero():
    import staffing_elasticity as se
    # $120 of wages needs $400 of sales at a 30% target; one more measured
    # +$500 to +$800 a meal.
    pays = se.decide({"sales": {"est": 0.13, "lo": 0.10, "hi": 0.16}}, 5000, 120.0, 30.0)
    assert pays["move"] == 1 and pays["net"]["lo"] == 100
    unsure = se.decide({"sales": {"est": 0.05, "lo": -0.02, "hi": 0.12}}, 5000, 120.0, 30.0)
    assert unsure["move"] == 0
    costs = se.decide({"sales": {"est": 0.0, "lo": -0.01, "hi": 0.01}}, 5000, 120.0, 30.0)
    assert costs["move"] == -1
    service = se.decide({"sales": {"est": 0.0, "lo": -0.01, "hi": 0.01}, "turn": {"est": -9, "lo": -14, "hi": -4}},
                        5000, 120.0, 30.0)
    assert service["move"] == 1 and service["why"].startswith("service")


def test_the_trim_cuts_where_an_hour_measurably_returns_least():
    import schedule_economics as econ
    rows = [{"date": "2026-10-16", "day": "Friday", "employee": n, "role": role, "shift_start": "5:00pm",
             "shift_end": "11:00pm", "scheduled_hours": 6}
            for n, role in (("Ana", "Server"), ("Al", "Server"), ("Bo", "Bartender"), ("Bea", "Bartender"))]

    def removed(value):
        _new, tr, _h = econ.trim_to_budget([dict(r) for r in rows], 18.0, {"2026-10-16": 18.0},
                                           staffing_value=value)
        return [t["role"] for t in tr if t.get("kind") == "removed"]
    assert removed(None) == ["Server"]
    # One more server at dinner measured to pay; one more bartender not.
    assert removed({("server", "night"): {"lo": 5.0, "hi": 20.0}, ("bartender", "night"): {"lo": -20.0, "hi": -5.0}}) \
        == ["Bartender"]
    # Inside its noise, a figure moves nothing.
    assert removed({("bartender", "night"): {"lo": -20.0, "hi": 5.0}}) == ["Server"]


def test_the_requirement_carries_the_measured_move_and_never_takes_the_last_person():
    import schedule_requirements as req
    typical = {("Friday", "night"): {"Server": 4, "Dishwasher": 1}}
    el = {("server", "night"): {"move": 1, "reason": "one more server at dinner: one more measured +$600"},
          ("dish", "night"): {"move": -1, "reason": "x"}}
    rows = req.shift_requirements(["2026-10-16"], typical_headcount=typical, elasticity=el)
    night = {x["role"]: x for x in next(r for r in rows if r["daypart"] == "night")["roles"]}
    assert night["Server"]["required"] == 5 and "+1: one more server" in night["Server"]["reason"]
    assert night["Dishwasher"]["required"] == 1


# ── LEARNING-26: productivity, partial pooling ───────────────────────────

def test_a_few_lucky_tickets_are_shrunk_toward_the_house():
    import staff_productivity as sp
    rnd = random.Random(2)

    def _around(mean, n):
        xs = [rnd.gauss(0, 0.3) for _ in range(n)]
        m = sum(xs) / n
        return [x - m + mean for x in xs]
    obs = {f"p{i}": _around(m, 200) for i, m in enumerate((-0.1, -0.05, 0.0, 0.05))}
    obs["few"], obs["many"] = _around(0.12, 20), _around(0.12, 400)
    got = sp.shrunk_effects(obs)
    # The same +12% on 20 tickets is pulled further toward the house, with a
    # wider interval, than on 400.
    assert got["few"]["raw"] == pytest.approx(0.12) and got["many"]["raw"] == pytest.approx(0.12)
    assert got["few"]["est"] < got["many"]["est"] < 0.12
    assert (got["few"]["hi"] - got["few"]["lo"]) > (got["many"]["hi"] - got["many"]["lo"])


def test_a_rating_that_disagrees_with_the_measure_is_flagged():
    import staff_productivity as sp
    meas = {"sales_per_guest": {"est": -0.15, "lo": -0.22, "hi": -0.08, "measured": True}}
    j = sp.judge_rating(meas, 5, updated_at="2025-01-01", today=date(2026, 10, 10))
    assert j["disagrees"] and j["stale"]
    assert not sp.judge_rating({"sales_per_guest": {"est": 0.0, "lo": -0.1, "hi": 0.1, "measured": False}}, 5)["disagrees"]


# ── LEARNING-27: cohort priors ────────────────────────────────────────────

def test_scheduling_priors_are_server_only_staff_partition_metrics():
    from intelligence import metrics_registry as reg, engine
    assert reg.partition_family("sched_prior.std.server.night") == "staff"
    assert reg.meta("sched_prior.noshow")["module"] == "labor"
    got = engine.benchmarks_payload({"restaurant_id": 1, "role": "owner"}, metric="sched_prior.noshow") \
        if hasattr(engine, "benchmarks_payload") else {"ok": False}
    assert not got.get("ok")


def test_no_prior_below_the_cohort_floor(db_path):
    from intelligence import schedule_priors as sp
    rid = _rid()
    assert sp.prior(rid, "sched_prior.noshow", db_path=db_path) is None
    assert sp.standard_prior(rid, db_path=db_path) == {}


def test_a_restaurants_own_figures_are_ratios_with_no_names(db_path, monkeypatch):
    from intelligence import schedule_priors as sp, privacy
    import staffing_model as sm
    import demand_drivers as ddr
    rid = _rid()
    _exec("UPDATE restaurants SET latitude=41.88, longitude=-87.63 WHERE id=?", (rid,))
    monkeypatch.setattr(sm, "measure", lambda *a, **k: {"families": {"server": {"parts": {"night": {
        "source": "measured", "own": 11.5, "n": 12}}}}})
    monkeypatch.setattr(ddr, "weather_effects", lambda *a, **k: {"night": {"snow": {"lift_pct": -30.0, "n": 4}}})
    f = sp.compute_features(rid, today=date.today(), db_path=db_path)
    assert f["sched_prior.std.server.night"] == 11.5
    # A weather lift is only ever a same-region one.
    assert sp.region_of(rid, db_path=db_path) == "40n_chicago"
    assert f["sched_prior.wx.40n_chicago.night.snow"] == 0.7
    privacy.assert_anonymous(f)


# ── MARKET-33: earned hours ───────────────────────────────────────────────

def test_the_work_earns_hours_at_the_standard():
    import staffing_model as sm
    model = {"families": {"server": {"unit": "guests", "lag": 0,
                                     "parts": {"night": {"std": 10.0, "fixed": 1}}}}}
    work = {"guests": {18 * 60: 10, 18 * 60 + 30: 20, 19 * 60: 4}}
    got = sm.earned(model, "2026-10-16", "night", work, {"server": 20.0}, {"server": 18.0})["server"]
    # ceil(10/5)=2, ceil(20/5)=4, ceil(4/5)=1 people → 7 half hours = 3.5h.
    assert got["earned"] == 3.5 and got["peak"] == 4 and got["scheduled"] == 20.0


def test_a_floor_the_work_never_needed_is_said_with_its_evidence(db_path):
    import staffing_model as sm
    rid = _rid()
    _exec("UPDATE restaurants SET role_floors_json=? WHERE id=?", (json.dumps({"Server": {"morning": 3}}), rid))
    today = date(2026, 10, 10)
    for k in range(1, 5):
        d = (today - timedelta(days=3) - timedelta(weeks=k - 1)).isoformat()   # Wednesdays
        facts = {"blocks": {"labor": {"detail": {"earned": {"by_part": {"morning": {"server": {
            "earned": 6.0, "scheduled": 12.0, "worked": 11.0, "peak": 2}}}}}}}}
        _exec("INSERT INTO dsr_reports (restaurant_id, business_date, version, status, facts_json) "
              "VALUES (?,?,?,?,?)", (rid, d, 1, "final", json.dumps(facts)))
    got = sm.floor_suggestions(rid, db_path=db_path, today=today)
    assert got and got[0]["floor"] == 3 and got[0]["suggest"] == 2 and "Wednesday lunch" in got[0]["text"]


# ── the plumbing: the engine reads the model and the requirements carry it ─

def test_the_engine_sizes_a_week_from_the_work_end_to_end(db_path):
    import schedule_engine as se
    import schedule_requirements as req
    rid = _rid()
    shifts = []
    base = date(2026, 8, 14)                                      # Fridays
    for k in range(7):
        d = (base + timedelta(weeks=k)).isoformat()
        for i in range(4):
            shifts.append({"date": d, "employee": f"S{i}", "role": "Server", "shift_start": "5:00pm",
                           "shift_end": "10:00pm", "actual_hours": 5})
        for j, (at, guests) in enumerate((("17:40", 10), ("18:10", 20), ("18:35", 40), ("19:05", 20))):
            _exec("INSERT INTO pos_tickets (restaurant_id, provider, ticket_id, business_date, opened_at, "
                  "guest_count, net_sales) VALUES (?,?,?,?,?,?,?)",
                  (rid, "rpower", f"{d}-{j}", d, f"{d}T{at}:00", guests, guests * 30.0))
    se._REQUIREMENT_MODEL_CACHE.clear()
    monkey_today = date(2026, 10, 10)
    import labor_standards as _ls
    real_today = _ls._today
    try:
        _ls._today = lambda rid_, today=None: monkey_today
        got = se._requirement_model(rid, shifts)
    finally:
        _ls._today = real_today
    model = got["model"]
    assert model["families"]["server"]["parts"]["night"]["std"] == pytest.approx(20.0)
    rows = req.shift_requirements(["2026-10-16"], typical_headcount={("Friday", "night"): {"Server": 4}},
                                  staffing_model=model, concurrent={("Friday", "night"): {"Server": {"peak": 4}}},
                                  half_curve=got.get("half_curve"))
    night = next(r for r in rows if r["daypart"] == "night")
    # 40 guests in the 6:30pm half hour at 20 a server-hour: 4 on at once.
    assert night["driven"] == ["Server"] and night["half_need"][18 * 60 + 30]["Server"] == 4
    assert req.half_hours_map(rows)[("2026-10-16", "night")][18 * 60 + 30] == {"Server": 4}

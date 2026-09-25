"""The Manager DSR and what both reports share (9/25/26): every KPI with
direction (vs the same weekday, best/worst in weeks, a trend line, target,
restaurants like yours only when fair), the manager's shift recap and
operations (no finance; comps/voids/refunds only with LOSS_VIEW), tomorrow's
prep with a real confidence %, and "How did yesterday turn out?" — Cavnar's
predictions graded against what was measured."""
import sys
from datetime import date, datetime, timedelta

import pytest

import auth
import models
import pos
import dsr
from dsr import access, kpis, predictions, store, tomorrow
from models import Restaurant, create_restaurant, update_restaurant, get_restaurant

SAT = date(2026, 9, 19)


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


def _rest(db):
    rid = create_restaurant(Restaurant(name="Simple EJ's", owner_email="e@x.com", timezone="America/Chicago"),
                            db_path=db)
    update_restaurant(rid, {"labor_target_pct": 28.0, "labor_target_source": "set", "food_cost_target": 30.0,
                            "food_cost_target_source": "set"}, db_path=db)
    return get_restaurant(rid, db_path=db)


def _night(db, rid, day, net=9000.0, labor_pct=28.0, hours=180.0, extra_sales=None, labor=None, food=None,
           reviews=None):
    r = store.create_report(rid, day, trigger="sweep", db_path=db)
    m = {"net": net, "guests": 300.0, "avg_ticket": 42.0, "voids": 80.0, "discounts": 120.0, "comps": 60.0,
         "cat:Food": net * .7, "cat:Liquor": net * .2, "cat:Beer": net * .1}
    m.update(extra_sales or {})
    store.save_block(r["id"], "sales", dsr.block(dsr.READY, source="rpower", metrics=m), db_path=db)
    lm = {"pct": labor_pct, "cost": round(net * labor_pct / 100, 2), "hours": hours, "overtime_hours": 0,
          "no_shows": 0, "late_arrivals": 0, "shift_quality": 88, "scheduled": 17}
    lm.update(labor or {})
    store.save_block(r["id"], "labor", dsr.block(dsr.READY, source="rpower", metrics=lm), db_path=db)
    if food:
        store.save_block(r["id"], "food", dsr.block(dsr.READY, source="cavnar", metrics=food[0], detail=food[1]),
                         db_path=db)
    if reviews:
        store.save_block(r["id"], "reviews", dsr.block(dsr.READY, source="google", metrics=reviews), db_path=db)
    store.set_stage(r["id"], "collecting", db_path=db)
    store.set_stage(r["id"], "final", db_path=db)
    return store.get_report(rid, day, db_path=db)


def _saturdays(db, rid, labor):
    # Four earlier Saturdays, newest first in `labor`.
    for w, pct in enumerate(labor, start=1):
        _night(db, rid, SAT - timedelta(days=7 * w), labor_pct=pct)


# ── direction on every figure ──────────────────────────────────────────────

def test_labor_carries_its_direction_its_streak_its_target_and_no_unfair_peer(db):
    r = _rest(db)
    _saturdays(db, r.id, [28.9, 29.4, 30.1, 29.0])
    rep = _night(db, r.id, SAT, labor_pct=27.5)
    k = {x["key"]: x for x in access.render(rep, {"role": "owner"}, r)["kpis"]}["labor_pct"]
    assert k["value_text"] == "27.5%"
    assert k["change"] == {"delta": -1.4, "text": "↓ 1.4 pts vs last Saturday", "tone": "good"}
    assert k["streak"] == {"text": "Best Saturday in 5 weeks", "tone": "good"}
    assert k["target"]["value_text"] == "28%" and k["target"]["label"] == "Target"
    # The platform is below every peer floor: no "restaurants like yours" number, and it says why.
    assert k["peers"]["available"] is False and k["peers"]["why_not"]
    # The trend line is the last 14 nights; with only Saturdays on file that is two points, too few to draw.
    assert k["spark"] == []


def test_a_streak_needs_an_unbroken_run_and_money_moves_in_percent(db):
    r = _rest(db)
    _night(db, r.id, SAT - timedelta(days=7), net=8000.0)
    _night(db, r.id, SAT - timedelta(days=21), net=7000.0)          # a gap at two weeks back
    rep = _night(db, r.id, SAT, net=9000.0)
    k = {x["key"]: x for x in access.render(rep, {"role": "owner"}, r)["kpis"]}["net"]
    assert k["change"]["text"] == "↑ 12.5% vs last Saturday" and k["change"]["tone"] == "good"
    assert k["streak"] is None


def test_derived_figures_are_computed_from_the_night(db):
    r = _rest(db)
    rep = _night(db, r.id, SAT, net=9000.0, hours=180.0, labor_pct=27.0,
                 food=({"est_food_cost_pct": 30.5}, {}))
    k = {x["key"]: x for x in access.render(rep, {"role": "owner"}, r)["kpis"]}
    assert k["splh"]["value"] == 50.0 and k["prime_pct"]["value_text"] == "57.5%"
    assert k["bev_mix"]["value_text"] == "30.0%"
    assert k["prime_pct"]["estimate"] and k["food_pct"]["estimate"]


# ── the Manager DSR: operations, not finance ───────────────────────────────

def test_the_manager_sees_the_shift_and_operations_but_no_finance(db):
    r = _rest(db)
    rep = _night(db, r.id, SAT, labor={"no_shows": 1, "late_arrivals": 2, "overtime_hours": 0, "scheduled": 18,
                                         "shift_quality": 93},
                 food=({"est_food_cost_pct": 31.0}, {}), reviews={"negative": 2, "received": 6})
    p = access.render(rep, {"role": "manager"}, r)
    assert p["scorecard"] is None
    shift = {x["key"]: x["value_text"] for x in p["shift"]["rows"]}
    assert shift == {"scheduled": "18", "no_shows": "1", "late_arrivals": "2", "overtime_hours": "0",
                     "shift_quality": "93"}
    assert "break" not in str(p["shift"]).lower(), "no source for breaks yet — left off, not shown empty"
    top = {x["key"] for x in p["kpis"]}
    assert top.isdisjoint({"net", "food_pct", "prime_pct", "labor_cost", "bev_mix"})
    ops = [x["key"] for x in p["operations"]]
    assert "complaints" in ops and "discounts" in ops
    assert {"voids", "comps"}.isdisjoint(ops), "comps and voids are owner-granted (LOSS_VIEW)"
    owner = access.render(rep, {"role": "owner"}, r)
    assert owner["shift"] is None and owner["operations"] == []


# ── predictions, graded ────────────────────────────────────────────────────

def test_predictions_are_written_once_and_graded_by_what_was_measured(db):
    r = _rest(db)
    fc = {"available": True, "typical_sales": 9000.0, "low": 8200.0, "high": 9800.0, "samples": 12,
          "weekday": "Saturday"}
    preds = predictions.build(fc, budget_net=8500.0, weather={"precip_pct": 70}, weekday="Saturday")
    assert [p["key"] for p in preds] == ["sales_range", "sales_budget", "rain"]
    assert preds[1]["text"] == "Sales expected above budget ($8,500)"
    assert predictions.record(r.id, SAT - timedelta(days=1), SAT, preds) == 3
    # A re-run the next morning with hindsight does not rewrite them.
    assert predictions.record(r.id, SAT - timedelta(days=1), SAT,
                              [dict(preds[0], text="Sales between $9,000 and $9,300")]) == 0
    rep = _night(db, r.id, SAT, net=9400.0)
    rows = {x["key"]: x["outcome"] for x in predictions.grade(r.id, SAT, rep["facts"])}
    assert rows == {"sales_range": "correct", "sales_budget": "correct", "rain": "incorrect"}
    y = access.render(store.get_report(r.id, SAT), {"role": "owner"}, r)["yesterday"]
    assert [i["outcome"] for i in y["items"]] == ["correct", "correct", "incorrect"]
    assert y["items"][0]["text"] == "Sales between $8,200 and $9,800" and y["items"][0]["actual_text"] == "Net sales $9,400"
    # Three graded is under the floor: the count shows, the % does not.
    assert y["accuracy"] == {"pct": None, "correct": 2, "graded": 3, "window_days": 30, "min_graded": 5}


def test_a_night_with_no_sales_leaves_its_predictions_ungraded(db):
    r = _rest(db)
    predictions.record(r.id, SAT - timedelta(days=1), SAT,
                       [{"key": "sales_range", "metric": "sales.net", "op": "between", "low": 1.0, "high": 2.0,
                         "text": "x"}])
    rows = predictions.grade(r.id, SAT, {"blocks": {"sales": {"status": dsr.AWAITING}}})
    assert rows[0]["outcome"] is None


def test_accuracy_is_a_percentage_once_enough_are_graded(db):
    r = _rest(db)
    for i in range(6):
        d = SAT - timedelta(days=i)
        predictions.record(r.id, d - timedelta(days=1), d, [{"key": "sales_range", "metric": "sales.net",
                                                              "op": "between", "low": 0.0, "high": 10000.0,
                                                              "text": "x"}])
        predictions.grade(r.id, d, {"blocks": {"sales": {"status": dsr.READY,
                                                         "metrics": {"net": 20000.0 if i == 0 else 5000.0}}}})
    acc = predictions.accuracy(r.id, SAT)
    assert acc["graded"] == 6 and acc["correct"] == 5 and acc["pct"] == 83


# ── tomorrow ───────────────────────────────────────────────────────────────

def test_tomorrow_lists_the_prep_and_a_confidence_it_can_back(db, monkeypatch):
    import demand
    import weather
    r = _rest(db)
    sun = SAT + timedelta(days=1)
    c = models.get_conn(db)
    c.execute("INSERT INTO staff_time_off (restaurant_id, employee_name, start_date, end_date, status) "
              "VALUES (?,?,?,?, 'approved')", (r.id, "Dana", sun.isoformat(), sun.isoformat()))
    c.execute("INSERT INTO staff_time_off (restaurant_id, employee_name, start_date, end_date, status) "
              "VALUES (?,?,?,?, 'approved')", (r.id, "Jake", sun.isoformat(), sun.isoformat()))
    c.execute("INSERT INTO demand_signals (restaurant_id, date, kind, label) VALUES (?,?,?,?)",
              (r.id, sun.isoformat(), "event", "School football game"))
    c.commit(); c.close()
    monkeypatch.setattr(weather, "forecast_for_day", lambda rest, day, db_path=None: {
        "day": {"precip_pct": 70, "high_f": 61, "short_forecast": "Rain", "stale": False}, "night": None})
    monkeypatch.setattr(demand, "forecast_day", lambda rid, day=None, db_path=None: {
        "available": True, "typical_sales": 7000.0, "low": 6200.0, "high": 7900.0, "samples": 8, "weekday": "Sunday"})
    facts = _night(db, r.id, SAT, food=({"est_food_cost_pct": 30.0},
                                        {"stock": {"critical": [{"item": "Chicken", "days_remaining": 1}]}}))["facts"]
    t = tomorrow.build(r, SAT, facts)
    texts = [i["text"] for i in t["items"]]
    assert texts[0] == "2 employees off (Dana, Jake)"
    assert "Rain forecast (70% chance, high 61°)" in texts and "School football game" in texts
    assert "Chicken low (1 day left)" in texts
    assert t["forecast"]["text"] == "$6,200–$7,900"
    conf = t["confidence"]
    # history 8/12 of 35 + sales 20 + weather 15 + events 15 (no published schedule) = 73.3 → held at 70
    assert conf["pct"] == 70 and "the weather forecast" in conf["based_on"] and "tomorrow's schedule" in conf["missing"]
    assert "held at 70%" in conf["track"]
    # Rain outranks the event for a prediction; both are gradable claims.
    assert [p["key"] for p in t["_preds"]] == ["sales_range", "rain"]


def test_the_pipeline_records_predictions_only_while_tomorrow_is_ahead(db, monkeypatch):
    import demand
    from dsr import pipeline
    r = _rest(db)
    monkeypatch.setattr(demand, "forecast_day", lambda rid, day=None, db_path=None: {
        "available": True, "typical_sales": 7000.0, "low": 6200.0, "high": 7900.0, "samples": 8, "weekday": "Sunday"})
    rep = _night(db, r.id, SAT)
    night = datetime(2026, 9, 20, 5, 0)                       # 12am Sunday in Chicago
    pipeline._tomorrow(r, rep["id"], SAT, "sweep", night, db)
    assert [p["key"] for p in predictions.for_date(r.id, SAT + timedelta(days=1))] == ["sales_range"]
    assert store.get_report(r.id, SAT)["facts"]["tomorrow"]["forecast"]["text"] == "$6,200–$7,900"
    # A re-run of Saturday on Monday afternoon records nothing new for Sunday.
    monkeypatch.setattr(demand, "forecast_day", lambda rid, day=None, db_path=None: {
        "available": True, "typical_sales": 7000.0, "low": 1.0, "high": 2.0, "samples": 8, "weekday": "Sunday"})
    c = models.get_conn(db); c.execute("DELETE FROM dsr_predictions"); c.commit(); c.close()
    pipeline._tomorrow(r, rep["id"], SAT, "rerun", datetime(2026, 9, 21, 20, 0), db)
    assert predictions.for_date(r.id, SAT + timedelta(days=1)) == []


def test_every_report_opens_with_a_summary_the_manager_s_is_operations(db):
    r = _rest(db)
    rep = _night(db, r.id, SAT, extra_sales={"budget_net": 8000.0, "vs_budget_net": 1000.0,
                                             "vs_budget_net_pct": 12.5})
    ops = {"text": "Saturday ran 300 guests on 17 people.", "cites": ["sales.guests"]}
    lead = {"text": "Sales beat budget by $1,000.", "cites": ["sales.vs_budget_net"]}
    store.save_narrative(rep["id"], {"executive_summary": lead, "operations_summary": ops, "went_well": [],
                                     "needs_attention": [], "actions_tomorrow": []})
    rep = store.get_report(r.id, SAT)
    assert access.render(rep, {"role": "manager"}, r)["narrative"]["executive_summary"]["text"] == ops["text"]
    assert access.render(rep, {"role": "owner"}, r)["narrative"]["executive_summary"]["text"] == lead["text"]


def test_both_emails_follow_the_new_order(db):
    import emails
    from dsr import deliver
    r = _rest(db)
    _saturdays(db, r.id, [28.9, 29.4, 30.1, 29.0])
    predictions.record(r.id, SAT - timedelta(days=1), SAT,
                       [{"key": "sales_range", "metric": "sales.net", "op": "between", "low": 8000.0, "high": 9500.0,
                         "text": "Sales between $8,000 and $9,500"}])
    rep = _night(db, r.id, SAT, labor_pct=27.5, labor={"no_shows": 1, "late_arrivals": 2, "scheduled": 18},
                 extra_sales={"budget_net": 8500.0, "vs_budget_net": 500.0, "vs_budget_net_pct": 5.9})
    predictions.grade(r.id, SAT, rep["facts"])
    store.save_section(rep["id"], "tomorrow", {"date": "2026-09-20", "weekday": "Sunday",
                                               "items": [{"kind": "event", "tone": "warn", "text": "School football game"}],
                                               "forecast": {"text": "$6,200–$7,900", "basis": "the median of the last 8 Sundays"},
                                               "confidence": {"pct": 70, "based_on": ["tonight's sales"], "missing": [],
                                                              "track": "x"}})
    ops = {"text": "Saturday ran 300 guests on 17 people.", "cites": ["sales.guests"]}
    lead = {"text": "Sales beat budget by $500.", "cites": ["sales.vs_budget_net"]}
    store.save_narrative(rep["id"], {"executive_summary": lead, "operations_summary": ops, "went_well": [],
                                     "needs_attention": [], "actions_tomorrow": []})
    rep = store.get_report(r.id, SAT)
    _s, owner, _p = emails.dsr_email(deliver.digest(access.render(rep, {"role": "owner"}, r), r))
    order = [owner.index(k) for k in ("Executive summary", "Today&rsquo;s score", "Top KPIs",
                                      "Tomorrow &middot; Sunday", "How did yesterday turn out?")]
    assert order == sorted(order)
    assert "↓ 1.4 pts vs last Saturday" in owner and "Best Saturday in 5 weeks" in owner
    assert "Sales between $8,000 and $9,500" in owner and "Correct" in owner and "AI confidence 70%" in owner
    _s, mgr, _p = emails.dsr_email(deliver.digest(access.render(rep, {"role": "manager"}, r), r))
    order = [mgr.index(k) for k in ("Operations summary", "Today&rsquo;s shift", ">Operations<", "Top KPIs")]
    assert order == sorted(order)
    assert "Saturday ran 300 guests on 17 people." in mgr and "Sales beat budget" not in mgr
    assert "Employees scheduled" in mgr and "Call-offs" in mgr and "Today&rsquo;s score" not in mgr

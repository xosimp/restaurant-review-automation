"""Group I of the AI confidence calibration & trust audit (9/24/26):
forecasts, owner wording and cross-module agreement.

Each test fails on the code before the fix it names. Where a rule has to
hold everywhere, the test reads the source rather than one fixture
(feedback: a test on one rendered payload only covers that fixture).
"""
import inspect
import json
import os
import random
import re
from datetime import date, datetime, timedelta

import pytest

import models
from models import create_restaurant, Restaurant, get_conn

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(autouse=True)
def _redirect_db(monkeypatch, db_path):
    """Bound imports (CLAUDE.md): patch every module this file reaches that
    binds get_conn at import."""
    import business_intelligence, review_intelligence, food_cost_intelligence, activity, demand
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    for module in (models, business_intelligence, review_intelligence, food_cost_intelligence,
                   activity, demand):
        monkeypatch.setattr(module, "get_conn", redirect, raising=False)


def _rid(db_path, **flags):
    defaults = dict(module_reviews=1, module_labor=1, module_inventory=1, module_marketing=1)
    defaults.update(flags)
    return create_restaurant(Restaurant(name="Forecast Co", owner_email="f@x.com", **defaults), db_path=db_path)


def _sql(db_path, sql, args=()):
    conn = get_conn(db_path)
    conn.execute(sql, args)
    conn.commit()
    conn.close()


def _src(path):
    return open(os.path.join(ROOT, path), encoding="utf-8").read()


def _code(path):
    """Source with comment lines and docstrings' prose left in but `#`
    comments stripped — a rule about what the code DOES must not pass on a
    comment naming the old behaviour."""
    return "\n".join(l.split("#")[0] if not l.strip().startswith("#") else "" for l in _src(path).splitlines())


# ══ I1 — the waste forecast ═══════════════════════════════════════════════

def test_probe_w_the_four_week_mean_beats_last_week_and_momentum_on_pure_noise():
    """CA2 probe W, replayed: $400 ± $60 a week, 8 weeks, no trend at all.
    The old forecast (latest week + last week's change) missed by far more
    than simply repeating last week; the forecast now in use must beat both."""
    import forecast_log
    rng = random.Random(7)
    err = {"mean4": [], "last": [], "momentum": []}
    for _ in range(4000):
        series = [rng.gauss(400, 60) for _ in range(9)]
        hist, nxt = series[:8], series[8]
        err["mean4"].append(abs(forecast_log.waste_next_week(hist) - nxt))
        err["last"].append(abs(hist[-1] - nxt))
        err["momentum"].append(abs(max(0.0, hist[-1] + (hist[-1] - hist[-2])) - nxt))
    mae = {k: sum(v) / len(v) for k, v in err.items()}
    assert mae["mean4"] < mae["last"] < mae["momentum"], mae
    assert mae["momentum"] > 1.5 * mae["mean4"], mae


def test_probe_f_a_forecast_viewed_daily_is_one_row_per_iso_week(db_path):
    """CA2 probe F: eight daily views across two ISO weeks wrote eight rows
    and read as "8 scored". Insert-once per period: two rows, and a later
    prediction never replaces the first."""
    import forecast_log
    rid = _rid(db_path)
    monday = date(2026, 8, 3)
    for i in range(8):
        forecast_log.record(rid, "waste_week", 400 + i, period_of=monday + timedelta(days=i), db_path=db_path)
    conn = get_conn(db_path)
    rows = conn.execute("SELECT horizon_end, predicted FROM forecast_log WHERE restaurant_id=? ORDER BY horizon_end",
                        (rid,)).fetchall()
    conn.close()
    assert [(r["horizon_end"], r["predicted"]) for r in rows] == [("2026-08-09", 400.0), ("2026-08-16", 407.0)]
    again = forecast_log.record(rid, "waste_week", 999, period_of=monday, db_path=db_path)
    assert again["recorded"] is False and "already frozen" in again["reason"]


def test_legacy_daily_rows_count_once_per_week_in_the_record(db_path):
    """Rows written the old way (one per render day) are read one per week,
    the FIRST prediction for that week standing for it."""
    import forecast_log
    rid = _rid(db_path)
    days = ("2026-08-04", "2026-08-05", "2026-08-06", "2026-08-12", "2026-08-13",
            "2026-08-19", "2026-08-20", "2026-08-26")
    for i, day in enumerate(days):
        _sql(db_path, "INSERT INTO forecast_log (restaurant_id, kind, horizon_end, predicted, actual, error_pct, "
                      "signed_error_pct, created_at) VALUES (?,?,?,?,?,?,?,datetime('now', ?))",
             (rid, "waste_week", day, 110, 100, 10.0 + i, 10.0 + i, f"-{30 - i} days"))
    acc = forecast_log.accuracy(rid, "waste_week", db_path=db_path)
    assert acc["n_weeks"] == 4 and acc["scored"] == 4
    assert acc["mean_error_pct"] == round((10.0 + 13.0 + 15.0 + 17.0) / 4, 1)


def test_the_record_reads_often_wide_and_withholds_the_next_forecast(db_path):
    import forecast_log
    rid = _rid(db_path)
    for i in range(4):
        _sql(db_path, "INSERT INTO forecast_log (restaurant_id, kind, horizon_end, predicted, actual, error_pct, "
                      "signed_error_pct) VALUES (?,?,?,?,?,?,?)",
             (rid, "waste_week", (date(2026, 8, 2) + timedelta(weeks=i)).isoformat(), 140, 100, 40.0, 40.0))
    acc = forecast_log.accuracy(rid, "waste_week", db_path=db_path)
    assert acc["reading"] == "often wide" and acc["withheld"] is True and acc["n_weeks"] == 4
    # K8 shape
    assert {"reading", "mean_error_pct", "n_weeks", "withheld"} <= set(acc)
    # The insight shows nothing when the record withholds it, but keeps
    # recording and scoring the raw forecast so the record can recover.
    src = inspect.getsource(__import__("inventory").get_claude_insights)
    assert 'if not _facc.get("withheld")' in src
    assert "_flog_f.record(" in src and "forecast_shown = _corr if _corrected else forecast_next_week" in src


def test_the_owner_sees_the_corrected_figure_and_the_activity_line_tells_the_truth(db_path):
    """The bias correction used to live only in `basis` while the activity
    feed said "this one is corrected for it" (CA1 red flag 2). Now the waste
    forecast shown IS the corrected one, and the prime-cost projection —
    shown raw — never claims a correction."""
    import activity, forecast_log
    rid = _rid(db_path)
    for i in range(3):
        _sql(db_path, "INSERT INTO forecast_log (restaurant_id, kind, horizon_end, predicted, actual, error_pct, "
                      "signed_error_pct, created_at) VALUES (?,?,?,?,?,?,?,datetime('now','-40 days'))",
             (rid, "waste_week", (date(2026, 7, 5) + timedelta(weeks=i)).isoformat(), 120, 100, 20.0, 20.0))
    corrected, cal = forecast_log.calibrated(rid, "waste_week", 120, db_path=db_path)
    assert corrected == 100.0 and "ran 20% high" in cal["reading"]
    forecast_log.record(rid, "waste_week", 120, period_of=date.today() + timedelta(days=7), db_path=db_path)
    r = models.get_restaurant(rid, db_path)
    texts = [e["text"] for e in activity.build(rid, restaurant=r, db_path=db_path)["entries"]
             if e.get("kind") == "projected"]
    assert texts and "next week's waste" in texts[0] and "the figure shown is corrected for it" in texts[0]
    # the prime-cost projection is shown raw: no correction claimed
    for i in range(3):
        _sql(db_path, "INSERT INTO forecast_log (restaurant_id, kind, horizon_end, predicted, actual, error_pct, "
                      "signed_error_pct, created_at) VALUES (?,?,?,?,?,?,?,datetime('now','-90 days'))",
             (rid, "profitability_month", f"2026-0{4 + i}-28", 60, 50, 20.0, 20.0))
    _sql(db_path, "INSERT INTO forecast_log (restaurant_id, kind, horizon_end, predicted) VALUES (?,?,?,?)",
         (rid, "profitability_month", "2099-12-31", 61))
    texts = [e["text"] for e in activity.build(rid, restaurant=r, db_path=db_path)["entries"]
             if e.get("kind") == "projected"]
    assert texts and "profitability" in texts[0] and "corrected" not in texts[0]


def test_unknown_kinds_are_refused_and_the_other_groups_kinds_are_accepted(db_path):
    import forecast_log
    rid = _rid(db_path)
    with pytest.raises(ValueError):
        forecast_log.record(rid, "waste_weekly", 1, db_path=db_path)
    for kind in ("labor_week", "marketing_reach_week", "review_rating_week", "revenue_week"):
        assert forecast_log.record(rid, kind, 10, period_of=date(2026, 8, 5), db_path=db_path)["recorded"]


def test_labor_and_rating_forecasts_score_against_their_closed_week(db_path, monkeypatch):
    import forecast_log, metrics
    rid = _rid(db_path)
    forecast_log.record(rid, "labor_week", 30.0, period_of=date(2026, 8, 5), db_path=db_path)
    forecast_log.record(rid, "review_rating_week", 4.5, period_of=date(2026, 8, 5), db_path=db_path)
    seen = []
    monkeypatch.setattr(metrics, "measure", lambda r, key, s, e, db: (seen.append((key, s, e)) or
                                                                       ({"labor_pct": 25.0}.get(key), "")))
    out = forecast_log.score_due(rid, today=date(2026, 8, 20), db_path=db_path)
    assert out == {"scored": 1, "unmeasurable": 1, "gave_up": 0}
    assert ("labor_pct", "2026-08-03", "2026-08-09") in seen
    row = forecast_log.frozen(rid, "labor_week", date(2026, 8, 5), db_path=db_path)
    assert row["actual"] == 25.0 and row["error_pct"] == 20.0 and row["signed_error_pct"] == 20.0


def test_the_food_brief_carries_both_records_k8():
    import food_cost_intelligence as fci
    brief = inspect.getsource(fci.executive_brief)
    ev = inspect.getsource(fci.build_evidence)
    assert '"prime_cost_accuracy"' in ev and '"profitability_month"' in ev
    assert '"forecast_accuracy": ev["forecast_accuracy"]' in brief and '"prime_cost_accuracy"' in brief


# ══ I2 — demand accuracy, ranges and the weekly projection ════════════════

def _sales_history(db_path, rid, weekday, values, before):
    for i, v in enumerate(values, start=1):
        d = before - timedelta(weeks=i)
        _sql(db_path, "INSERT INTO labor_daily_history (restaurant_id, date, day_of_week, sales, labor_pct, "
                      "labor_cost, total_hours) VALUES (?,?,?,?,0,0,0)", (rid, d.isoformat(), weekday, v))


def test_the_forecast_range_is_a_percentile_interval_only_on_enough_weeks(db_path):
    """It was the min and max of as few as three nights (CA2 #6)."""
    import demand
    rid = _rid(db_path)
    day = date(2026, 9, 25)                          # a Friday
    _sales_history(db_path, rid, "Friday", [1000, 1100, 1200, 5000], day)
    few = demand.forecast_day(rid, day, db_path=db_path)
    assert few["available"] and few["low"] is None and few["high"] is None
    assert "range not yet measurable" in few["range_note"] and "needs 8" in few["range_note"]
    rid2 = _rid(db_path)
    vals = [1000, 1100, 1200, 1300, 1400, 1500, 1600, 5000]
    _sales_history(db_path, rid2, "Friday", vals, day)
    full = demand.forecast_day(rid2, day, db_path=db_path)
    # The 80% prediction range for the next night (re-audit B2 #13), not the
    # two extremes on file and not the 10th-90th percentile of the eight.
    lo, hi = demand.prediction_range(vals)
    assert full["low"] == round(lo, 2) and full["high"] == round(hi, 2)
    assert full["high"] < 5000                                 # not the extreme
    assert full["range_coverage_pct"] == 80


def test_demand_accuracy_reads_the_nightly_forecast_out_of_sample(db_path):
    import demand
    rid = _rid(db_path)
    today = date(2026, 9, 24)
    pcts = [10, -10, 20, -5, 5, 0, 30, -20]
    for i, p in enumerate(pcts, start=1):
        d = (today - timedelta(days=i)).isoformat()
        for metric, value in (("sales.vs_forecast_pct", p), ("sales.net", 1000 + p * 10),
                              ("sales.forecast_low", 900), ("sales.forecast_high", 1150)):
            _sql(db_path, "INSERT INTO dsr_metrics (restaurant_id, business_date, metric, value, status) "
                          "VALUES (?,?,?,?,'ready')", (rid, d, metric, value))
    acc = demand.demand_accuracy(rid, today=today, db_path=db_path)
    assert acc["available"] and acc["n_nights"] == 8
    assert acc["mean_error_pct"] == round(sum(abs(p) for p in pcts) / 8, 1)
    assert acc["bias_pct"] == round(sum(pcts) / 8, 1)
    inside = sum(1 for p in pcts if 900 <= 1000 + p * 10 <= 1150)
    assert acc["inside_range_pct"] == round(inside / 8 * 100) and acc["n_ranged"] == 8
    assert {"mean_error_pct", "bias_pct", "inside_range_pct", "n_nights"} <= set(acc)   # K8
    # under the floor: nothing stated
    rid2 = _rid(db_path)
    assert demand.demand_accuracy(rid2, today=today, db_path=db_path)["available"] is False


def test_the_nightly_report_stores_the_forecast_range_it_is_scored_against():
    src = _src("dsr/block_sales.py")
    assert '"forecast_low": (fc or {}).get("low")' in src and '"forecast_high": (fc or {}).get("high")' in src


def test_the_weekly_projection_is_frozen_at_publish_and_scored_on_the_same_days(db_path):
    import demand, forecast_log
    rid = _rid(db_path)
    week = date(2026, 8, 3)                          # Monday
    for wd_off in range(7):
        d = week + timedelta(days=wd_off)
        if d.strftime("%A") == "Monday":
            continue                                 # closed Mondays: no history, no forecast
        _sales_history(db_path, rid, d.strftime("%A"), [1000, 1000, 1000], d)
    out = demand.freeze_week_projection(rid, week, db_path=db_path)
    assert out["recorded"] and out["predicted"] == 6000.0
    assert demand.freeze_week_projection(rid, week, db_path=db_path)["recorded"] is False   # republish
    for wd_off in range(1, 7):
        d = week + timedelta(days=wd_off)
        _sql(db_path, "INSERT INTO labor_daily_history (restaurant_id, date, day_of_week, sales) VALUES (?,?,?,?)",
             (rid, d.isoformat(), d.strftime("%A"), 1200))
    assert forecast_log.score_due(rid, today=date(2026, 8, 12), db_path=db_path)["scored"] == 1
    row = forecast_log.frozen(rid, "revenue_week", week, db_path=db_path)
    assert row["actual"] == 7200.0 and row["signed_error_pct"] == round((6000 - 7200) / 7200 * 100, 1)
    # the publish path freezes it
    import client_api
    assert "freeze_week_projection(rid, row[\"week_start\"])" in inspect.getsource(client_api._publish_schedule)


def test_a_week_missing_a_projected_night_is_not_scored(db_path):
    import demand, forecast_log
    rid = _rid(db_path)
    week = date(2026, 8, 3)
    for wd_off in range(7):
        d = week + timedelta(days=wd_off)
        _sales_history(db_path, rid, d.strftime("%A"), [1000, 1000, 1000], d)
    demand.freeze_week_projection(rid, week, db_path=db_path)
    for wd_off in range(6):                           # Sunday never synced
        d = week + timedelta(days=wd_off)
        _sql(db_path, "INSERT INTO labor_daily_history (restaurant_id, date, day_of_week, sales) VALUES (?,?,?,?)",
             (rid, d.isoformat(), d.strftime("%A"), 1000))
    assert forecast_log.score_due(rid, today=date(2026, 8, 12), db_path=db_path) == {"scored": 0, "unmeasurable": 1, "gave_up": 0}


def test_k8_demand_accuracy_rides_on_the_brief_the_labor_payload_and_ask():
    import morning_brief, mobile_api, ask_cavnar_tools
    assert 'out["demand_accuracy"] = acc' in inspect.getsource(morning_brief.build)
    assert '"demand_accuracy": demand_accuracy' in inspect.getsource(mobile_api._do_mobile_labor)
    assert 'out["demand_accuracy"]' in inspect.getsource(ask_cavnar_tools._read_schedule)


def test_the_brief_never_calls_its_forecast_measured():
    import morning_brief
    brief = {"date": "2026-09-23", "lines": [{"key": "today", "tone": "neutral", "forecast": True,
                                              "text": "Today looks like a typical Wednesday: about $1,000."}]}
    html = morning_brief._email_html(brief, "R")
    assert "except today's forecast" in html and "Every figure above is measured from your own data." not in html


# ══ I3 — rating_trend's direction values and their consumers ══════════════

def _weekly_reviews(db_path, rid, weekly_ratings):
    """weekly_ratings oldest first; each week's reviews dated in its own
    %W week, the newest in the current one."""
    n = len(weekly_ratings)
    k = 0
    for i, ratings in enumerate(weekly_ratings):
        when = datetime.now() - timedelta(weeks=n - 1 - i)
        for r in ratings:
            k += 1
            _sql(db_path, "INSERT INTO reviews (restaurant_id, platform, external_id, author, rating, text, "
                          "review_date, fetched_at, processed) VALUES (?,?,?,?,?,?,?,?,1)",
                 (rid, "google", f"t{rid}-{k}", "G", r, "x", when.strftime("%Y-%m-%d %H:%M:%S"),
                  when.strftime("%Y-%m-%d %H:%M:%S")))


DECLINING = [(5, 5, 5), (5, 5, 4), (5, 4, 4), (4, 4, 4), (4, 4, 3), (4, 3, 3)]


def test_rating_trend_returns_only_its_declared_directions(db_path):
    import review_intelligence as ri
    rid = _rid(db_path)
    _weekly_reviews(db_path, rid, DECLINING)
    t = ri.rating_trend(rid, weeks=8, db_path=db_path)
    assert t["direction"] == "declining" and t["direction"] in ri.TREND_DIRECTIONS
    assert t["confidence"] in ("medium", "high")


def test_a_rising_line_that_ends_below_where_it_started_is_flat_not_improving(db_path):
    """CA1 R1: direction from the slope, size from first-vs-last — "Rating
    up" printed beside a negative change."""
    import review_intelligence as ri
    rid = _rid(db_path)
    _weekly_reviews(db_path, rid, [(5, 5, 4), (4, 4, 3), (5, 5, 4), (5, 5, 5), (5, 4, 4)])
    t = ri.rating_trend(rid, weeks=8, db_path=db_path)
    assert t["slope"] > 0 and t["change"] < 0
    assert t["direction"] == "flat"


def test_consumers_compare_against_the_real_directions_everywhere():
    """morning_brief:603 checked ("up","down") and business_intelligence:490
    checked "down" — values rating_trend never returns — and the tests mocked
    "down", so both passed and neither ever fired (CA1 red flag 3)."""
    import review_intelligence as ri
    for path in ("morning_brief.py", "business_intelligence.py", "notify.py"):
        code = _code(path)
        for m in re.finditer(r"(?:direction|\bd)\s*(?:==|in)\s*\(?([^\n:]+)", code):
            quoted = re.findall(r'"(up|down)"', m.group(1))
            assert not quoted, f"{path} compares a trend direction against {quoted}"
    assert set(ri.TREND_DIRECTIONS) == {"improving", "declining", "flat"}


def test_the_morning_brief_trend_line_fires_on_a_real_declining_trend(db_path):
    import morning_brief
    rid = _rid(db_path)
    _weekly_reviews(db_path, rid, DECLINING)
    tuesday = date(2026, 9, 22)
    line = morning_brief._review_variety(rid, tuesday, db_path)
    assert line and line["key"] == "rv:trend" and line["tone"] == "bad"
    assert "slipping" in line["text"] and "reviews)" in line["text"]


def test_the_intel_link_fires_on_real_rating_trend_output(db_path):
    import business_intelligence as bi
    rid = _rid(db_path)
    _weekly_reviews(db_path, rid, DECLINING)
    for i, (k, n) in enumerate(((7, 7), (1, 7))):
        _sql(db_path, "INSERT INTO ai_visibility_runs (restaurant_id, ai_score, gbp_score, answered, appeared, "
                      "created_at) VALUES (?,?,?,?,?,datetime('now', ?))",
             (rid, round(k / n * 100), 50, n, k, f"-{2 - i} days"))
    data = bi.gather(rid, db_path=db_path)
    data["visibility"] = dict(data.get("visibility") or {}, stale=False)
    links = [l for l in bi.correlations(rid, data=data, db_path=db_path) if l["kind"] == "intel_x_reviews"]
    assert len(links) == 1


# ══ I4 — AI visibility: ranges, not points ════════════════════════════════

def test_the_chip_label_comes_from_the_range_not_the_point():
    import client_api
    # a 57% point whose range spans every band is said as the range
    wide = client_api.ai_visibility_band(22, 86)
    assert wide["ai_score_band"] == "uncertain" and "between 22% and 86%" in wide["ai_score_label"]
    assert client_api.ai_visibility_band(70, 100)["ai_score_band"] == "often"
    assert client_api.ai_visibility_band(0, 30)["ai_score_band"] == "rarely"
    assert client_api.ai_visibility_band(None, None)["ai_score_label"] == "not measured"


def test_ask_and_the_cross_module_read_carry_the_range():
    import ask_cavnar_tools, business_intelligence as bi
    tool = inspect.getsource(ask_cavnar_tools._read_ai_visibility)
    assert '"ai_score_low"' in tool and '"ai_score_high"' in tool
    assert '"ai_score_low": lo, "ai_score_high": hi' in inspect.getsource(bi._visibility)


# ══ I5 — unsourced claims ═════════════════════════════════════════════════

UNSOURCED = ("come back more often", "cheapest thing a restaurant can do", "50-review AI threshold",
             "threshold for appearing", "0.031", "Cornell HBS", "your busiest Sundays follow")


def test_no_server_string_carries_an_unsourced_research_claim():
    """CA4 F5 / CA1 R16, H14, L30, M9: removed, and kept out everywhere on
    the server — templates are the clients' (group J)."""
    import subprocess
    files = subprocess.check_output(["git", "ls-files", "*.py"], cwd=ROOT, text=True).split()
    hits = []
    for f in files:
        if f.startswith(("tests/", "scripts/")):
            continue
        code = _code(f)
        for phrase in UNSOURCED:
            if phrase in code:
                hits.append((f, phrase))
    assert hits == []


def test_the_every_review_answered_milestone_states_only_what_is_true(db_path):
    import milestones
    rid = _rid(db_path)
    m = milestones.check_response_rate(rid, rate=100, db_path=db_path)
    assert m and "published on your listing" in m["body"]
    assert "more often" not in m["body"] and "cheapest" not in m["body"]


def test_the_holiday_banner_states_only_the_restaurants_own_history(db_path, monkeypatch):
    import demand, marketing
    rid = _rid(db_path)
    now = datetime(2026, 11, 20, 10, 0)
    monkeypatch.setattr(marketing, "get_upcoming_holidays", lambda d: "Thanksgiving (Nov 26)")
    out = demand.upcoming_holidays(rid, now=now, db_path=db_path)
    assert out[0]["label"] == demand.HOLIDAY_GENERIC_LABEL and out[0]["lift_pct"] is None
    assert out[0]["date_str"] == "11/26/26" and out[0]["days_away"] == 6
    # same-day holiday is today, not next year (the datetime compare dropped it)
    monkeypatch.setattr(marketing, "get_upcoming_holidays", lambda d: "Thanksgiving (Nov 20)")
    assert demand.upcoming_holidays(rid, now=now, db_path=db_path)[0]["days_away"] == 0


# ══ I6 — the monthly review reads the closed month ════════════════════════

def test_the_monthly_review_prime_cost_is_last_months_measurement(db_path, monkeypatch):
    import monthly_review, metrics, food_cost_intelligence as fci, business_intelligence as bi
    rid = _rid(db_path)
    monkeypatch.setattr(fci, "profitability_projection",
                        lambda *a, **k: pytest.fail("the review must not read this month's projection"))
    monkeypatch.setattr(bi, "executive_brief", lambda *a, **k: {})
    windows = {("2026-08-01", "2026-08-31"): {"food_cost_pct": 30.0, "labor_pct": 28.0},
               ("2026-07-01", "2026-07-31"): {"food_cost_pct": 31.0, "labor_pct": 29.0}}
    monkeypatch.setattr(metrics, "measure",
                        lambda r, key, s, e, db: ((windows.get((s, e)) or {}).get(key), "test"))
    r = monthly_review.build(rid, today=date(2026, 9, 16), db_path=db_path)
    assert r["prime_cost"]["pct"] == 58.0 and r["prime_cost"]["delta"] == -2.0
    assert r["prime_cost"]["claim_kind"] == "measured" and r["prime_cost"]["window"] == ["2026-08-01", "2026-08-31"]
    line = [l for l in monthly_review.lines(r) if l.startswith("Prime cost")][0]
    assert "was 58.0% of sales last month" in line and "measured" in line and "running at" not in line


# ══ I7 — which "measured" ═════════════════════════════════════════════════

def test_measured_dollars_carry_their_scope_and_only_delivered_is_measured(db_path):
    import value_delivered, milestones
    rid = _rid(db_path)
    assert value_delivered.breakdown(rid, db_path=db_path)["delivered"]["scope"] == "monthly_rate"
    measured = [s for s in value_delivered.VALUE_SECTIONS if s["key"] == "measured"][0]
    assert measured["figures"] == ["delivered"] and measured["heading"] == "What was measured"
    rest = [s for s in value_delivered.VALUE_SECTIONS if s["key"] != "measured"][0]
    assert set(rest["figures"]) == {"avoided", "surfaced", "opportunity"}
    assert value_delivered.home_block({"monthly": 10})["scope"] == "monthly_rate"
    assert '"scope": "all_time_sum"' in inspect.getsource(milestones.check_savings)


# ══ I8 — inventory wording and the 86'd recount ═══════════════════════════

def test_recoverable_is_labelled_an_opportunity_with_its_basis():
    import inventory
    items = [{"item": "Romaine", "category": "Produce", "par_level": 10, "current_stock": 8,
              "unit_cost": 2.0, "avg_daily_usage": 1.0, "last_order_qty": 20, "waste_last_week": 12.0,
              "unit": "lb", "case_size": 1}]
    out = inventory.analyse_inventory(items)
    assert out["recoverable_kind"] == "opportunity"
    assert "one week's count" in out["annual_recoverable_basis"] and "not money saved" in out["annual_recoverable_basis"]


def test_the_benchmark_comment_matches_the_bands_the_code_uses():
    code = inspect.getsource(__import__("inventory").analyse_inventory)
    comment = code.split("# Waste rate against Cavnar's STARTING target")[1].split("purchases_window comes")[0]
    # Benchmarking #36: four bands against the owner's target T (else the
    # 4-5% starting band) — this pinned the old five fixed cuts.
    for band in ("<=T (4%) under target", "<=T+1 (6%) near target", "<=2T (10%) over", "well over target"):
        assert band in comment
    for cut in ("waste_rate_pct <= _lo", "waste_rate_pct <= _hi + WASTE_NEAR_PTS",
                "waste_rate_pct <= _hi * WASTE_WELL_OVER_MULTIPLE"):
        assert cut in code


def test_an_86_at_close_is_not_inferred_as_waste(db_path, monkeypatch):
    """Verified real (CA1 F20): the close-out recount to 0 recorded the whole
    gap between expected stock and zero as inferred waste."""
    import inventory_ledger, closeout
    rid = _rid(db_path)
    conn = get_conn(db_path)
    ing = conn.execute("INSERT INTO ingredients (restaurant_id, name, unit, unit_cost, current_stock, is_active) "
                       "VALUES (?,?,?,?,?,1)", (rid, "Salmon", "lb", 10.0, 0)).lastrowid
    conn.commit(); conn.close()
    monkeypatch.setattr(models, "db_conn", _db_conn_for(db_path))
    inventory_ledger.record_recount(rid, ing, 12.0, source="manual")
    out = inventory_ledger.record_recount(rid, ing, 0.0, source="closeout", note="86'd at close: salmon",
                                          infer_waste=False)
    assert out["inferred_waste_qty"] == 0.0
    conn = get_conn(db_path)
    n = conn.execute("SELECT COUNT(*) FROM ingredient_stock_events WHERE ingredient_id=? AND event_type='waste'",
                     (ing,)).fetchone()[0]
    conn.close()
    assert n == 0
    assert "infer_waste=False" in inspect.getsource(closeout._act_on)


def _db_conn_for(db_path):
    import contextlib

    @contextlib.contextmanager
    def _cm():
        conn = models.get_conn(db_path)
        try:
            yield conn
        finally:
            conn.close()
    return _cm


# ══ I10 — one market average, one listing table, one industry figure ══════

def test_every_surface_reads_one_market_average():
    import competitor_intel_format as cif, mobile_api
    comps = [{"rating": 4.8, "review_count": 3000}, {"rating": 3.0, "review_count": 10},
             {"rating": 5.0, "review_count": 4, "rating_is_provisional": True}]
    one = cif.market_rating(comps)
    assert one["market_rating"] == 4.8 and one["market_rating_n"] == 2          # weighted, provisional out
    assert mobile_api._market_rating(comps) == one
    assert "market_rating(rivals)" in _src("first_look.py")
    assert "_cif.market_rating(" in _src("hosted_dashboard.py")
    # standing: a real lead, and never against an imported sample. Since
    # Benchmarking #38 a standing also needs three rivals matched on cuisine
    # and price (this pinned a standing on two unmatched ones).
    g = {"own_rating": 5.0, "own_rating_basis": "google_all_time"}
    assert cif.market_standing(g, one)["standing"] is None                      # two unmatched rivals
    matched = dict(one, market_matched_n=3)
    assert cif.market_standing(g, matched)["standing"] == "level"               # +0.2 is not a lead
    assert cif.market_standing(dict(g, own_rating=5.2), matched)["standing"] == "ahead"
    assert cif.market_standing({"own_rating": 3.0, "own_rating_basis": "imported_sample"}, one)["standing"] is None


def test_the_first_look_says_ahead_only_on_a_real_lead():
    import first_look
    close = first_look.lines({"rating": 4.2, "review_count": 90,
                              "neighbourhood": {"avg_rating": 4.1, "count": 5, "best": "X"}})
    assert not any("ahead of your own neighbourhood" in l for l in close)


def test_listing_strength_label_and_tone_come_from_one_table():
    import client_api
    assert client_api.presence_band(75) == {"presence_label": "Listing strength",
                                            "presence_band_label": "a few gaps", "presence_tone": "warn"}
    assert client_api.presence_band(85)["presence_tone"] == "good"
    assert client_api.presence_band(10)["presence_tone"] == "bad"


def test_labor_vs_industry_is_one_constant_with_one_set_of_guards():
    import thresholds
    for path in ("hosted_dashboard.py", "mobile_api.py"):
        code = _code(path)
        assert "0.345" not in code and "(0.32 -" not in code
        # both surfaces read one breakdown (labor.savings_breakdown, NS3 M9),
        # which is where the guarded comparison is made
        assert "savings_breakdown(" in code
    # labor.savings_breakdown no longer computes "$ under industry": the
    # only published labor figure includes benefits (re-audit #2). This
    # pinned the call, and dollars from a figure that is not like for like.
    assert "labor_vs_industry_monthly(" not in _code("labor.py")
    from metrics import DAYS_PER_MONTH   # one month definition (NS3 L5)
    # The benchmark is by restaurant type (benchmark_registry, NS4 H3): the
    # caller passes the type's published figure, and none means no claim —
    # nor does a figure measured differently.
    assert thresholds.labor_vs_industry_monthly(30.0, 30000, 30, industry_pct=34.2) == 0
    assert thresholds.labor_vs_industry_monthly(30.0, 30000, 30, industry_pct=34.2, comparable=True) == \
        round((34.2 - 30.0) / 100 * 30000 / 30 * DAYS_PER_MONTH)
    assert thresholds.labor_vs_industry_monthly(30.0, 30000, 30) == 0
    assert thresholds.labor_vs_industry_monthly(30.0, 30000, 30, hours_are_estimated=True, industry_pct=34.2) == 0
    assert thresholds.labor_vs_industry_monthly(30.0, 30000, 5, industry_pct=34.2) == 0


# ══ I11 — the nightly report ═════════════════════════════════════════════

def test_the_weekly_workbook_marks_provisional_nights():
    from dsr import xlsx
    grid = {"label": "Week 4", "start": "2026-09-14", "end": "2026-09-20", "categories": [],
            "days": [{"date": "2026-09-14", "weekday": "Mon", "net": 100.0, "provisional": True},
                     {"date": "2026-09-15", "weekday": "Tue", "net": 120.0, "provisional": False}],
            "totals": {"net": 220.0, "days_measured": 2}}
    rows, _w, _m = xlsx.week_rows(grid, "R")
    labels = [r[0] for r in rows if r and isinstance(r[0], str)]
    assert "Mon 9/14/26 (provisional)" in labels and "Tue 9/15/26" in labels
    flat = json.dumps(rows)
    assert "1 night marked provisional" in flat


# ══ I12 — floors and bands ════════════════════════════════════════════════

def test_a_week_is_judged_against_a_band_scaled_for_seven_days():
    """The 28-day labor band (0.5 points) was applied to 7-day windows, so
    an ordinary 0.8-point weekly wobble read as "worse than last week"."""
    import weekly_review, metrics
    scale = weekly_review.band_scale("labor_pct")
    assert scale == 2.0
    assert metrics.compare("labor_pct", 30.0, 30.8, band_scale=scale)["verdict"] == "no_clear_change"
    # One rule since the integration pass (weekly_review.week_band): the
    # scaled stated band is the floor, this restaurant's own 7-day band
    # (metrics.noise_band, F2) replaces it only when wider — both the weekly
    # review and the digest's "trending" line read it.
    band = weekly_review.week_band(None, "labor_pct", 30.0)["band"]
    assert band == 1.0
    assert metrics.compare("labor_pct", 30.0, 30.8, band=band)["verdict"] == "no_clear_change"
    assert "week_band(restaurant_id, key" in inspect.getsource(weekly_review.build)
    assert "_wr_lr.week_band(" in _src("reporter.py")


def test_one_ranking_floor_for_the_groups_strongest_and_weakest():
    for path in ("home_brief.py", "reporter.py"):
        assert "GROUP_RANK_MIN_REVIEWS" in _src(path)


# ══ I13 — demand thresholds ══════════════════════════════════════════════

def test_the_lineup_and_the_scorer_read_one_demand_table(db_path, monkeypatch):
    import preshift, labor, shift_quality, thresholds
    assert shift_quality.demand_from_pct(10) == thresholds.demand_level(10) == "high"
    rid = _rid(db_path)
    monkeypatch.setattr(labor, "build_demand_forecast", lambda *a, **k: {"days": [
        {"day": "Friday", "median_sales": 5000, "samples": 6, "vs_average_pct": 10}]})
    text = " ".join(i["text"] for i in preshift.build(rid, day=date(2026, 9, 18))["items"])
    # +10% is "high" to the scorer; the lineup used to call it "a typical Friday"
    assert "busier-than-average Friday" in text and "10%" in text
    monkeypatch.setattr(labor, "build_demand_forecast", lambda *a, **k: {"days": [
        {"day": "Friday", "median_sales": 5000, "samples": 2, "vs_average_pct": 40}]})
    assert not [i for i in preshift.build(rid, day=date(2026, 9, 18))["items"] if i["kind"] == "volume"]


def test_a_weekday_is_reliably_slow_only_when_its_nights_consistently_are(db_path, monkeypatch):
    import demand, labor
    rid = _rid(db_path)
    monkeypatch.setattr(labor, "build_demand_forecast", lambda *a, **k: {
        "ok": True, "overall_median": 1000.0,
        "days": [{"day": "Tuesday", "median_sales": 800, "samples": 6, "vs_average_pct": -20}]})
    tomorrow = date.today() + timedelta(days=1)
    # two dead Tuesdays among ordinary ones: a low median, not a slow day
    _sales_history(db_path, rid, "Tuesday", [1100, 1050, 200, 150, 1200, 790, 780], tomorrow)
    assert demand.slow_days(rid, db_path=db_path)["slow_days"] == []
    rid2 = _rid(db_path)
    _sales_history(db_path, rid2, "Tuesday", [800, 820, 790, 810, 780, 830, 1100], tomorrow)
    slow = demand.slow_days(rid2, db_path=db_path)["slow_days"]
    assert slow and slow[0]["day"] == "Tuesday" and slow[0]["consistency"] >= demand.RELIABLY_SLOW_SHARE


def test_the_scheduler_reads_a_weekday_level_only_on_three_readings():
    src = _src("schedule_engine.py")
    assert src.count("if (d.get(\"samples\") or 0) >= _MINR") == 2


# ══ I14 — one rating-trend rule for the SMS ═══════════════════════════════

def test_the_negative_trend_alert_reads_the_slope_scorer(db_path):
    import notify
    code = _code("notify.py")
    assert "avgs[0] > avgs[1] > avgs[2]" not in code
    rid = _rid(db_path)
    _weekly_reviews(db_path, rid, DECLINING)
    got = notify._negative_trend(rid, db_path=db_path)
    assert got and got["weeks"] >= 4 and got["reviews"] == 3 * got["weeks"]
    rid2 = _rid(db_path)
    _weekly_reviews(db_path, rid2, [(5, 4, 4)] * 6)
    assert notify._negative_trend(rid2, db_path=db_path) is None

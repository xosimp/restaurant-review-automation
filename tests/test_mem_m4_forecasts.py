"""forecasts: six forecast kinds were scored and only waste corrected itself
(memory audit 9/29/26, M4). Every producer now reads its own record
(forecast_log.shown): withheld while it reads often wide, corrected — and
saying so — when it has leaned one way, and the RAW figure is what is
recorded, so a correction never feeds on itself. The published weeks'
revenue record corrects the schedule's projected revenue and so its hours
budget."""
from datetime import date, timedelta

import pytest

import forecast_log
import models
from models import Restaurant, create_restaurant


@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    """models.get_conn, and every module that bound it at import
    (schedule_economics among them — CLAUDE.md, bound imports): imported
    earlier by another test file, its copy would read that file's database."""
    import sys
    import schedule_economics  # noqa: F401 — bound get_conn, redirected below
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    for mod in list(sys.modules.values()):
        try:
            if getattr(mod, "get_conn", None) is real:
                monkeypatch.setattr(mod, "get_conn", redirect)
        except Exception:
            pass
    monkeypatch.setattr(models, "get_conn", redirect)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    yield


def _rid(name="Forecast Co"):
    return create_restaurant(Restaurant(name=name, owner_email=f"{name.split()[0].lower()}@x.test"))


def _scored(rid, kind, predicted, actual, n=5):
    conn = models.get_conn()
    try:
        last_sunday = date.today() - timedelta(days=date.today().weekday() + 1)
        for i in range(n):
            end = last_sunday - timedelta(weeks=i + 1)
            if forecast_log.KINDS[kind]["period"] == "month":
                end = (date.today().replace(day=1) - timedelta(days=1 + 31 * i)).replace(day=1)
                end = forecast_log.month_bounds(end)[1]
            err, signed = forecast_log.errors(predicted, actual)
            conn.execute("INSERT INTO forecast_log (restaurant_id, kind, horizon_end, predicted, actual, error_pct, "
                         "signed_error_pct, scored_at, created_at) VALUES (?,?,?,?,?,?,?,datetime('now'),?)",
                         (rid, kind, end.isoformat(), predicted, actual, err, signed,
                          f"{(end - timedelta(days=10)).isoformat()} 09:00:00"))
        conn.commit()
    finally:
        conn.close()


def test_shown_follows_the_waste_pattern():
    rid = _rid()
    assert forecast_log.shown(rid, "labor_week", 30.0)["shown"] == 30.0       # no record: as is
    _scored(rid, "labor_week", 33.6, 30.0)                                     # ran 12% high
    rec = forecast_log.shown(rid, "labor_week", 33.6)
    assert rec["corrected"] and rec["raw"] == 33.6 and rec["shown"] == pytest.approx(30.0, abs=0.05)
    assert "ran 12% high" in rec["note"]
    wide = _rid("Wide Co")
    _scored(wide, "labor_week", 45.0, 30.0)                                    # 50% out: often wide
    rec = forecast_log.shown(wide, "labor_week", 45.0)
    assert rec["withheld"] and rec["shown"] is None and rec["raw"] == 45.0


def test_the_labor_forecast_line_reads_its_own_record():
    import labor
    rid = _rid()
    analysis = {"overall_labor_pct": 33.6}
    assert "near 33.6%" in labor._labor_forecast_line(analysis, 1.5, restaurant_id=rid)
    _scored(rid, "labor_week", 33.6, 30.0)
    line = labor._labor_forecast_line(analysis, 1.5, restaurant_id=rid)
    assert "near 30%" in line and "already corrected because earlier forecasts here ran 12% high" in line
    wide = _rid("Wide Co")
    _scored(wide, "labor_week", 45.0, 30.0)
    assert labor._labor_forecast_line(analysis, 1.5, restaurant_id=wide) is None


def test_a_withheld_labor_forecast_is_still_recorded_raw():
    import inspect
    import labor
    src = inspect.getsource(labor)
    # The record is kept on the raw figure whatever the line does.
    assert 'if has_trend and trend_diff is not None and restaurant_id:' in src
    assert '"labor_week", analysis.get("overall_labor_pct")' in src


def test_the_marketing_reach_line_reads_its_own_record():
    import client_api
    rid = _rid()
    _scored(rid, "marketing_reach_week", 1120.0, 1000.0)
    line, pred = client_api._mkt_forecast([900, 1000], 11, rid=rid, week_sum=1120)
    assert pred == int(round(1000 / 1.12)) and "already corrected" in line
    wide = _rid("Wide Co")
    _scored(wide, "marketing_reach_week", 2000.0, 1000.0)
    assert client_api._mkt_forecast([900, 1000], 11, rid=wide, week_sum=2000) == (None, None)


def test_the_rating_forecast_keeps_its_raw_figure_while_withheld():
    import review_intelligence as ri
    rid = _rid()
    trend = {"direction": "declining", "confidence": "high", "next_week": 4.2}
    assert ri.rating_forecast_detail(rid, trend) == {"raw": 4.2, "shown": 4.2, "note": None}
    _scored(rid, "review_rating_week", 4.2, 2.0)                               # wildly wide
    d = ri.rating_forecast_detail(rid, trend)
    assert d["raw"] == 4.2 and d["shown"] is None and ri.rating_forecast(rid, trend) is None
    import inspect
    import client_api
    assert '"review_rating_week", _rating_raw' in inspect.getsource(client_api)


def _sales(rid, weeks=8, per_day=1000.0):
    conn = models.get_conn()
    try:
        start = date.today() - timedelta(days=weeks * 7 + 7)
        for i in range(weeks * 7 + 6):
            d = start + timedelta(days=i)
            conn.execute("INSERT INTO labor_daily_history (restaurant_id, date, day_of_week, sales, labor_cost) "
                         "VALUES (?,?,?,?,?)", (rid, d.isoformat(), d.strftime("%A"), per_day, per_day * 0.3))
        conn.commit()
    finally:
        conn.close()


def test_the_published_weeks_record_corrects_the_schedules_revenue_and_budget():
    import schedule_economics as econ
    rid = _rid()
    _sales(rid)
    plain = econ.projected_weekly_revenue(rid)
    assert plain["value"] == 7000 and plain["calibration"] is None
    _scored(rid, "revenue_week", 7840.0, 7000.0)                              # projections ran 12% high
    got = econ.projected_weekly_revenue(rid)
    assert got["raw_value"] == 7000 and got["value"] == round(7000 / 1.12)
    assert "corrected down 11%" in got["source"] and "ran 12% high" in got["source"]
    # The frozen weekly projection itself is never corrected.
    import demand, inspect
    assert "calibrat" not in inspect.getsource(demand.freeze_week_projection)


def test_the_prime_cost_projection_carries_its_corrected_month_end(monkeypatch):
    import cogs
    import food_cost_intelligence as fci
    import labor
    rid = _rid()
    monkeypatch.setattr(fci, "_local_today", lambda r: date(2026, 9, 20))
    monkeypatch.setattr(cogs, "net_sales_in_window", lambda *a, **k: (20000.0, None))
    monkeypatch.setattr(cogs, "build_food_cost_pct", lambda *a, **k: {"ok": True, "cogs": 6000.0, "pct": 30.0})
    monkeypatch.setattr(labor, "analyse_shifts_for_restaurant",
                        lambda r: {"is_live": True, "total_sales": 20000.0, "overall_labor_pct": 30.0,
                                   "date_range": {"start": "2026-09-01", "end": "2026-09-19"}})
    monkeypatch.setattr(fci, "_labor_stale_why", lambda *a, **k: None)
    out = fci.profitability_projection(rid)
    assert out["available"] and out["prime_cost_pct"] == 60.0 and out["projection_correction"] is None
    _scored(rid, "profitability_month", 66.0, 60.0)                           # ran 10% high
    out = fci.profitability_projection(rid)
    corr = out["projection_correction"]
    assert out["prime_cost_pct"] == 60.0, "the month to date stays as measured"
    assert corr and corr["prime_cost_pct"] == pytest.approx(54.5, abs=0.1) and "ran 10% high" in corr["note"]
    assert fci.profitability_projection(rid, withhold=False)["projection_correction"] is None


# ── an event week is not the forecast's lean (event_memory, M5's contract) ──

def test_a_week_a_measured_event_explains_is_left_out_of_the_correction():
    rid = _rid()
    _scored(rid, "revenue_week", 10000.0, 8000.0, n=4)        # ran 25% high four weeks
    conn = models.get_conn()
    try:
        # A fifth, older week that ran LOW — the Cubs home game in it.
        end = date.today() - timedelta(days=date.today().weekday() + 1) - timedelta(weeks=5)
        err, signed = forecast_log.errors(10000.0, 13000.0)
        conn.execute("INSERT INTO forecast_log (restaurant_id, kind, horizon_end, predicted, actual, error_pct, "
                     "signed_error_pct, scored_at, created_at, explained_by) VALUES (?,?,?,?,?,?,?,datetime('now'),?,?)",
                     (rid, "revenue_week", end.isoformat(), 10000.0, 13000.0, err, signed,
                      f"{(end - timedelta(days=10)).isoformat()} 09:00:00", "Cubs home game on 9/1/26, +22% here"))
        conn.commit()
    finally:
        conn.close()
    cal = forecast_log.calibration(rid, "revenue_week")
    assert cal["scored"] == 4 and cal["left_out_events"] == 1 and cal["bias_pct"] == 25.0
    assert cal["reading"].endswith("(1 week with a measured event left out)")
    assert forecast_log.accuracy(rid, "revenue_week")["scored"] == 5, "how it held up counts every week"


def test_where_most_weeks_hold_the_event_it_is_an_ordinary_week():
    rid = _rid()
    _scored(rid, "revenue_week", 10000.0, 8000.0, n=4)
    conn = models.get_conn()
    conn.execute("UPDATE forecast_log SET explained_by='Trivia night' WHERE restaurant_id=?", (rid,))
    conn.commit()
    conn.close()
    assert forecast_log.calibration(rid, "revenue_week")["left_out_events"] == 0


def test_scoring_stamps_the_event_that_explains_a_week(monkeypatch):
    import event_memory
    rid = _rid()
    end = date.today() - timedelta(days=date.today().weekday() + 1) - timedelta(weeks=1)
    conn = models.get_conn()
    conn.execute("INSERT INTO forecast_log (restaurant_id, kind, horizon_end, predicted, created_at) "
                 "VALUES (?,?,?,?,?)", (rid, "revenue_week", end.isoformat(), 10000.0,
                                        f"{(end - timedelta(days=10)).isoformat()} 09:00:00"))
    conn.commit()
    conn.close()
    monkeypatch.setattr(forecast_log, "actual_for", lambda *a, **k: 12500.0)
    monkeypatch.setattr(forecast_log, "naive_forecasts", lambda *a, **k: {"last": None, "mean": None, "n": 0})
    game = end - timedelta(days=2)
    monkeypatch.setattr(event_memory, "night_facts", lambda rid_, d, db_path=None: [
        {"kind": "event", "label": "cubs", "display": "Cubs home game", "measured_lift_pct": 22.0, "n": 4,
         "applies": True}] if d == game else [
        {"kind": "event", "label": "trivia", "display": "Trivia", "measured_lift_pct": 30.0, "n": 1,
         "applies": False}])
    assert forecast_log.score_due(rid)["scored"] == 1
    conn = models.get_conn()
    got = conn.execute("SELECT explained_by FROM forecast_log WHERE restaurant_id=?", (rid,)).fetchone()[0]
    conn.close()
    from time_utils import mdy
    assert got == f"Cubs home game on {mdy(game)}, +22% here", "an effect measured once does not apply"

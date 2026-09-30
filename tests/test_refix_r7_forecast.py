"""Re-audit fix round R7 (9/29/26), LOOPS-13: the daily sales forecast now
corrects itself from its own measured misses — a consistent lean over enough
scored nights, bounded — and the nightly report keeps the RAW figure, which
is what the next correction is measured against."""
from datetime import date, timedelta

import pytest

import demand
import models
from models import Restaurant, create_restaurant

TODAY = date(2026, 9, 29)


@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(demand, "local_today", lambda rid: TODAY)
    import pos
    monkeypatch.setattr(pos, "connected_provider", lambda rid: ("rpower", object()))
    yield


def _rid():
    rid = create_restaurant(Restaurant(name="Lean Tap", owner_email="lean@x.test"))
    conn = models.get_conn()
    for k in range(1, 9):                           # eight plain $4,000 Tuesdays before TODAY
        d = TODAY - timedelta(weeks=k)
        conn.execute("INSERT INTO labor_daily_history (restaurant_id, date, day_of_week, sales, labor_cost, "
                     "labor_pct, final, provider) VALUES (?,?,?,?,?,?,?,?)",
                     (rid, d.isoformat(), d.strftime("%A"), 4000.0, 1200.0, 30.0, 1, "rpower"))
    conn.commit()
    conn.close()
    return rid


def _nights(rid, ratios, raw_key="sales.forecast_net", shown=None):
    """One scored night per ratio (actual ÷ forecast), newest first."""
    conn = models.get_conn()
    for i, ratio in enumerate(ratios):
        d = (TODAY - timedelta(days=i + 1)).isoformat()
        rows = [("sales.net", 1000.0 * ratio), (raw_key, 1000.0), ("sales.forecast_same_basis", 1.0)]
        if shown is not None:
            rows.append(("sales.forecast_net", shown))
        for metric, value in rows:
            conn.execute("INSERT INTO dsr_metrics (restaurant_id, business_date, metric, value, status, source) "
                         "VALUES (?,?,?,?,?,?)", (rid, d, metric, value, "ready", "rpower"))
    conn.commit()
    conn.close()


def test_a_forecast_that_keeps_running_low_is_corrected_and_says_so():
    rid = _rid()
    _nights(rid, [1.12] * 20)
    fc = demand.forecast_day(rid, TODAY)
    assert fc["raw_sales"] == 4000.0 and fc["typical_sales"] == 4480.0
    assert fc["calibration"]["n"] == 20 and fc["calibration"]["lean_pct"] == 12.0
    assert "+12%" in fc["calibration_note"] and "ran 12% above the forecast" in fc["calibration_note"]
    # The plain median, and the weekly projection (its own record corrects it), stay raw.
    assert demand.forecast_day(rid, TODAY, calibrate=False)["typical_sales"] == 4000.0
    assert demand.forecast_day(rid, TODAY, effects=False)["typical_sales"] == 4000.0
    assert demand.week_projection(rid, [TODAY])["by_day"][TODAY.isoformat()] == 4000.0


def test_the_correction_is_measured_against_the_raw_figure_never_itself():
    rid = _rid()
    # The corrected forecast shown was spot on (4,480 against 4,480), but the
    # RAW forecast still ran 12% low: the correction stands.
    _nights(rid, [1.12] * 20, raw_key="sales.forecast_raw_net", shown=1120.0)
    assert demand.forecast_calibration(rid)["factor"] == 1.12


def test_too_few_nights_an_inconsistent_lean_or_a_small_one_correct_nothing():
    rid = _rid()
    _nights(rid, [1.12] * 10)
    cal = demand.forecast_calibration(rid)
    assert cal["available"] is False and cal["factor"] == 1.0 and "needs 14" in cal["reason"]
    rid = _rid()
    _nights(rid, [1.25, 0.95] * 10)                  # mean +10%, but only half the nights ran high
    assert demand.forecast_calibration(rid)["factor"] == 1.0
    rid = _rid()
    _nights(rid, [1.03] * 20)
    assert demand.forecast_calibration(rid)["reading"] == "no consistent lean"


def test_the_correction_is_bounded():
    rid = _rid()
    _nights(rid, [1.40] * 20)
    assert demand.forecast_calibration(rid)["factor"] == 1.15


def test_the_nightly_report_records_the_raw_forecast():
    import inspect
    from dsr import block_sales, narrative
    src = inspect.getsource(block_sales)
    assert '"forecast_raw_net"' in src and 'fc.get("raw_sales")' in src
    assert "sales.forecast_raw_net" in narrative.BOOKKEEPING_FACTS

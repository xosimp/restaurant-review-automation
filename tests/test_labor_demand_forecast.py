"""Sales-based demand forecasting for schedule generation.

The scheduler knew what a typical week looked like in headcount and what
the weather was doing, but nothing told it which days actually take the
money — even though labor_daily_history has carried per-day sales all
along. These cover the maths, the honesty guard when there isn't enough
history, and the prompt block's framing.
"""
import pytest

import labor
import models
from models import create_restaurant, Restaurant, get_conn


@pytest.fixture(autouse=True)
def _redirect_db(monkeypatch, db_path):
    real_get_conn = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real_get_conn(db_path))


def _restaurant(db_path):
    return create_restaurant(Restaurant(name="Demand Co", owner_email="d@x.com"), db_path=db_path)


def _history(db_path, rid, rows):
    """rows: [(days_ago, day_of_week, sales)]"""
    conn = get_conn(db_path)
    for days_ago, dow, sales in rows:
        conn.execute("""
            INSERT INTO labor_daily_history (restaurant_id, date, day_of_week, sales, labor_pct, labor_cost, total_hours)
            VALUES (?, date('now', ?), ?, ?, 0, 0, 0)
        """, (rid, f"-{days_ago} days", dow, sales))
    conn.commit()
    conn.close()


def test_reports_median_sales_per_weekday_and_relative_standing(db_path):
    rid = _restaurant(db_path)
    _history(db_path, rid, [
        (7, "Friday", 5000), (14, "Friday", 5200),
        (8, "Tuesday", 2000), (15, "Tuesday", 2100),
        (9, "Saturday", 6000), (16, "Saturday", 6400),
    ])
    f = labor.build_demand_forecast(rid)
    assert f["ok"] is True
    by_day = {d["day"]: d for d in f["days"]}
    assert by_day["Friday"]["median_sales"] == 5100
    assert by_day["Tuesday"]["median_sales"] == 2050
    assert f["busiest"] == "Saturday"
    assert f["quietest"] == "Tuesday"
    # Tuesday is well below an average day; Saturday well above.
    assert by_day["Tuesday"]["vs_average_pct"] < -30
    assert by_day["Saturday"]["vs_average_pct"] > 10


def test_a_one_off_outlier_does_not_redefine_a_normal_day(db_path):
    """Median, not mean — one huge private event shouldn't make every
    Saturday look like a banquet."""
    rid = _restaurant(db_path)
    _history(db_path, rid, [
        (7, "Saturday", 6000), (14, "Saturday", 6200), (21, "Saturday", 40000),
        (8, "Tuesday", 2000), (15, "Tuesday", 2100),
        (9, "Friday", 5000), (16, "Friday", 5100),
    ])
    by_day = {d["day"]: d for d in labor.build_demand_forecast(rid)["days"]}
    # The mean would be ~17k; the median stays in the real range.
    assert by_day["Saturday"]["median_sales"] == 6200


def test_refuses_to_guess_without_enough_history(db_path):
    rid = _restaurant(db_path)
    _history(db_path, rid, [(7, "Friday", 5000), (8, "Tuesday", 2000)])
    f = labor.build_demand_forecast(rid)
    assert f["ok"] is False
    assert "not enough" in f["reason"]


def test_ignores_days_with_no_recorded_sales(db_path):
    rid = _restaurant(db_path)
    _history(db_path, rid, [
        (7, "Friday", 5000), (14, "Friday", 5200),
        (8, "Tuesday", 2000), (15, "Tuesday", 2100),
        (9, "Sunday", 0), (16, "Sunday", 0),
        (10, "Saturday", 6000), (17, "Saturday", 6100),
    ])
    days = {d["day"] for d in labor.build_demand_forecast(rid)["days"]}
    assert "Sunday" not in days
    assert days == {"Friday", "Tuesday", "Saturday"}


def test_only_looks_at_the_recent_window(db_path):
    rid = _restaurant(db_path)
    _history(db_path, rid, [
        (7, "Friday", 5000), (14, "Friday", 5200),
        (8, "Tuesday", 2000), (15, "Tuesday", 2100),
        (9, "Saturday", 6000), (16, "Saturday", 6100),
        # Last year's numbers must not count toward an 8-week window.
        (300, "Monday", 99000), (307, "Monday", 99000),
    ])
    days = {d["day"] for d in labor.build_demand_forecast(rid)["days"]}
    assert "Monday" not in days


def test_prompt_block_states_the_numbers_and_keeps_the_floors_authoritative(db_path):
    rid = _restaurant(db_path)
    _history(db_path, rid, [
        (7, "Friday", 5000), (14, "Friday", 5200),
        (8, "Tuesday", 2000), (15, "Tuesday", 2100),
        (9, "Saturday", 6000), (16, "Saturday", 6400),
    ])
    block = labor.format_demand_block(labor.build_demand_forecast(rid))
    assert "EXPECTED DEMAND BY DAY" in block
    assert "Saturday is the busiest day" in block
    assert "Tuesday the quietest" in block
    assert "$5,100" in block
    # The framing has to stop the model treating this as an override.
    assert "does not replace it" in block
    assert "never" in block and "minimum staffing floors" in block


def test_no_block_at_all_when_there_is_nothing_to_say(db_path):
    rid = _restaurant(db_path)
    assert labor.format_demand_block(labor.build_demand_forecast(rid)) == ""
    assert labor.format_demand_block({"ok": False}) == ""
    assert labor.format_demand_block(None) == ""

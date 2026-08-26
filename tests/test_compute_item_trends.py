"""inventory.compute_item_trends() — price_alerts (single-week spike) and
trend_alerts (3+ week rise) must still fire even after get_claude_insights()
has already auto-written a same-day snapshot matching the current live
price (it does this on every insight view, not just once a week) — that
same-day row must be excluded from history, or the live price ends up
compared against a same-day snapshot of itself and always reads as
unchanged, silently hiding a real price movement for the rest of that day."""
from datetime import date, timedelta

from models import create_restaurant, Restaurant, get_conn
from inventory import compute_item_trends


def _restaurant(db_path):
    return create_restaurant(Restaurant(name="Trend Test Co", owner_email="t@x.com"), db_path=db_path)


def _insert_history(db_path, rid, week_end, price):
    # inventory_history isn't part of init_db()'s base schema — it's
    # created lazily by get_claude_insights() on first real use, same
    # pattern as webhooks/job_failures/staff_notes.
    conn = get_conn(db_path)
    conn.execute("""CREATE TABLE IF NOT EXISTS inventory_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        restaurant_id INTEGER NOT NULL,
        waste_json TEXT,
        week_end    TEXT,
        items_json  TEXT,
        saved_at    TEXT DEFAULT (datetime('now'))
    )""")
    conn.execute(
        "INSERT INTO inventory_history (restaurant_id, waste_json, week_end, items_json) VALUES (?,?,?,?)",
        (rid, "{}", week_end.isoformat(), f'[{{"item": "Salmon", "unit_cost": {price}}}]')
    )
    conn.commit()
    conn.close()


def _items(price):
    return [{"item": "Salmon", "category": "Protein", "par_level": 10, "current_stock": 8,
             "unit_cost": price, "avg_daily_usage": 2.0, "last_order_qty": 10, "waste_last_week": 0}]


def test_trend_detected_across_three_prior_weeks(db_path):
    rid = _restaurant(db_path)
    today = date.today()
    _insert_history(db_path, rid, today - timedelta(days=21), 10.00)
    _insert_history(db_path, rid, today - timedelta(days=14), 11.00)
    _insert_history(db_path, rid, today - timedelta(days=7), 12.00)

    trends = compute_item_trends(rid, _items(13.50), db_path=db_path)
    assert any(a["item"] == "Salmon" for a in trends["trend_alerts"])


def test_same_day_snapshot_matching_current_price_does_not_hide_the_trend(db_path):
    """The exact bug: get_claude_insights() already wrote today's row at
    the current live price (13.50) before compute_item_trends() runs —
    without excluding it, history[-1] == curr and the real 3-week rise
    from the prior weeks becomes undetectable."""
    rid = _restaurant(db_path)
    today = date.today()
    _insert_history(db_path, rid, today - timedelta(days=21), 10.00)
    _insert_history(db_path, rid, today - timedelta(days=14), 11.00)
    _insert_history(db_path, rid, today - timedelta(days=7), 12.00)
    _insert_history(db_path, rid, today, 13.50)  # same-day auto-snapshot collision

    trends = compute_item_trends(rid, _items(13.50), db_path=db_path)
    assert any(a["item"] == "Salmon" for a in trends["trend_alerts"]), \
        "same-day snapshot at the current price should not mask a real prior-week trend"


def test_single_week_spike_detected_against_last_real_prior_week(db_path):
    rid = _restaurant(db_path)
    today = date.today()
    _insert_history(db_path, rid, today - timedelta(days=7), 10.00)

    trends = compute_item_trends(rid, _items(11.00), db_path=db_path)  # +10%
    assert any(a["item"] == "Salmon" for a in trends["price_alerts"])


def test_single_week_spike_not_masked_by_same_day_snapshot(db_path):
    rid = _restaurant(db_path)
    today = date.today()
    _insert_history(db_path, rid, today - timedelta(days=7), 10.00)
    _insert_history(db_path, rid, today, 11.00)  # same-day auto-snapshot at the new price

    trends = compute_item_trends(rid, _items(11.00), db_path=db_path)
    assert any(a["item"] == "Salmon" for a in trends["price_alerts"])

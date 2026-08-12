"""value_delivered.py — the "Total Value Delivered" figure shown on the web
dashboard's Home banner and (new) the mobile Home tab: the dollar value
Cavnar AI has generated or saved this restaurant, summed across whichever
modules are active.

compute_total_value_delivered() mirrors hosted_dashboard.py's index() route
(its inline savings_breakdown computation) rather than being a shared
extraction of it — that route's version feeds many other template fields
off the same intermediate values (labor_annual, labor_vs_industry_monthly,
sales_lift_yr, ...), so pulling only the "total" piece out from under it
would mean either dragging all those unrelated fields along for no reason,
or refactoring a large, currently-working, production code path just to
save ~20 lines here. Duplicating this one isolated formula was the lower-
risk option.
"""
from models import get_conn, get_restaurant, get_review_stats, DB_PATH


def compute_total_value_delivered(restaurant_id: int, db_path: str = DB_PATH) -> int:
    """Reviews-response value ($5/response) + labor scheduling savings +
    recoverable food cost + marketing agency-equivalent value — each
    counted only when its module is active. Matches
    hosted_dashboard.py's savings_breakdown['total'] exactly."""
    restaurant = get_restaurant(restaurant_id, db_path=db_path)
    if not restaurant:
        return 0

    reviews_value = 0
    if restaurant.module_reviews:
        rstats = get_review_stats(restaurant_id)
        reviews_value = int(rstats.get("responded", 0)) * 5

    labor_value = 0
    if restaurant.module_labor:
        try:
            from labor import analyse_shifts_for_restaurant
            labor = analyse_shifts_for_restaurant(restaurant_id)
            labor_value = int(round(labor.get("potential_savings", 0) * 4.33))
        except Exception:
            labor_value = 0

    inv_value = 0
    if restaurant.module_inventory:
        try:
            from inventory import load_inventory_for_restaurant, analyse_inventory
            items, _live = load_inventory_for_restaurant(restaurant_id)
            inv = analyse_inventory(items)
            inv_value = int(inv.get("recoverable_monthly", 0))
        except Exception:
            inv_value = 0

    mkt_value = 0
    if restaurant.module_marketing:
        try:
            mkt_value = _marketing_agency_value(restaurant_id, db_path)
        except Exception:
            mkt_value = 0

    return reviews_value + labor_value + inv_value + mkt_value


def _marketing_agency_value(restaurant_id: int, db_path: str) -> int:
    """$1,500/mo social-media-manager baseline, only once content has
    actually been generated — same rule as the web dashboard."""
    conn = get_conn(db_path)
    conn.execute("""CREATE TABLE IF NOT EXISTS marketing_content_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT, restaurant_id INTEGER NOT NULL,
        content_type TEXT, topic TEXT, post_id TEXT, post_platform TEXT,
        created_at TEXT DEFAULT (datetime('now')))""")
    generated = conn.execute(
        "SELECT COUNT(*) FROM marketing_content_log WHERE restaurant_id=?", (restaurant_id,)
    ).fetchone()[0] or 0
    created_row = conn.execute(
        "SELECT created_at FROM restaurants WHERE id=?", (restaurant_id,)
    ).fetchone()
    conn.close()

    months_active = 1
    if created_row and created_row[0]:
        from datetime import datetime as _dt
        try:
            created = _dt.fromisoformat(created_row[0][:10])
            now = _dt.now()
            months_active = max(1, (now.year - created.year) * 12 + (now.month - created.month))
        except Exception:
            months_active = 1

    return months_active * 1500 if generated > 0 else 0


def record_value_snapshot(restaurant_id: int, total_value: int, db_path: str = DB_PATH):
    """Upserts today's total — called opportunistically from the mobile
    Home endpoint, so the first Home-tab load of each day records that
    day's figure. No separate scheduled job: a restaurant whose owner never
    opens the app that day simply doesn't get a data point for it, which is
    fine for a "how's this trending" sparkline."""
    conn = get_conn(db_path)
    conn.execute("""
        INSERT INTO value_snapshots (restaurant_id, snapshot_date, total_value)
        VALUES (?, date('now'), ?)
        ON CONFLICT(restaurant_id, snapshot_date) DO UPDATE SET total_value = excluded.total_value
    """, (restaurant_id, total_value))
    conn.commit()
    conn.close()


def get_value_history(restaurant_id: int, days: int = 30, db_path: str = DB_PATH) -> list[dict]:
    """Ascending by date — oldest first, matching how a sparkline is drawn
    left to right."""
    conn = get_conn(db_path)
    rows = conn.execute(f"""
        SELECT snapshot_date, total_value FROM value_snapshots
        WHERE restaurant_id=? AND snapshot_date >= date('now', '-{int(days)} days')
        ORDER BY snapshot_date ASC
    """, (restaurant_id,)).fetchall()
    conn.close()
    return [{"date": r["snapshot_date"], "value": r["total_value"]} for r in rows]

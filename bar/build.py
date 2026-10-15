"""
bar/build.py — the Bar page's payload, assembled from the package's reads.
"""
from datetime import date

from bar.common import LABELS, _mdy
from bar.counts import counted as _counted
from bar.counts import latest_counted
from bar.insights import _insights
from bar.judge import owner_stopped
from bar.pour_cost import _pour_cost, window_span
from bar.prices import _price_watch
from bar.purchases import _lines
from bar.sales import _sales
from bar.spend import _distributors, _products
from bar.targets import _restaurant, target_details


def _feed_status(restaurant_id, db_path, today):
    """The invoice feed's own status (bar.feed.feed_status, W2), or None
    when that module is not there — bar.judge then reads freshness from the
    invoices on file."""
    try:
        from bar import feed as _feed
    except ImportError:
        return None
    return _feed.feed_status(restaurant_id, db_path=db_path, today=today)


def build(restaurant_id, db_path=None, today=None) -> dict:
    """The Bar page's payload."""
    today = today or date.today()
    all_lines = _lines(restaurant_id, db_path)
    if not all_lines:
        return {"ok": True, "available": False,
                "why_not": "No distributor invoices yet. Once Fintech's daily file reaches Cavnar AI, "
                           "the bar's prices and what it buys against what it sells fill in here."}
    # An invoice dated after today is a typo or a mis-read: held for a date
    # check, never the page's "as of" or the latest price (V35).
    today_iso = today.isoformat()
    lines = [l for l in all_lines if str(l["invoice_date"])[:10] <= today_iso]
    held = sorted({(l["distributor"], l["invoice_number"], str(l["invoice_date"])[:10])
                   for l in all_lines if str(l["invoice_date"])[:10] > today_iso})
    held_rows = [{"distributor": d, "invoice_number": n, "date": x, "date_mdy": _mdy(x)} for d, n, x in held]
    if not lines:
        return {"ok": True, "available": False, "held_future": held_rows,
                "why_not": "Every invoice on file is dated after today — they're held for a date check."}
    sales = _sales(restaurant_id, db_path)
    restaurant = _restaurant(restaurant_id, db_path)
    feed = _feed_status(restaurant_id, db_path, today)
    inv_dates = sorted({str(l["invoice_date"])[:10] for l in lines})
    products = _products(lines)
    other_spend = round(sum(p["spend"] for p in products if p["category"] == "other"), 2)
    # Counted pour cost from the bar's shelf counts (bar.counts, F1),
    # withheld with `missing` until two counts bracket a span; when a counted
    # period covers the four-week window, the reading's basis is "counted".
    counted = _counted(restaurant_id, today=today, db_path=db_path, lines=lines, sales=sales)
    span = window_span(lines, sales, today)
    period = (latest_counted(restaurant_id, today=today, window=span, block=counted)
              if "why_not" not in span else None)
    pour = _pour_cost(lines, sales, today, targets=target_details(restaurant_id, db_path, restaurant=restaurant),
                      feed=feed, owner_stopped=owner_stopped(restaurant_id, db_path), restaurant=restaurant,
                      counted=period)
    prices = _price_watch(lines, today)
    to_check = sum(1 for p in products if not (p["sure"] or p["owner_set"]))
    fresh = pour.get("freshness") or {}
    return {"ok": True, "available": True,
            "as_of": inv_dates[-1], "as_of_mdy": _mdy(inv_dates[-1]),
            "invoices": {"from_mdy": _mdy(inv_dates[0]), "to_mdy": _mdy(inv_dates[-1]),
                         "count": len({(l["distributor"], l["invoice_number"]) for l in lines})},
            "held_future": held_rows,
            "feed": feed or {"invoices_through": fresh.get("invoices_through"),
                             "invoices_through_mdy": fresh.get("invoices_through_mdy"),
                             "sales_through_mdy": fresh.get("sales_through_mdy"),
                             "last_file_mdy": fresh.get("last_file_mdy"),
                             "stale_days": fresh.get("stale_days")},
            "pour_cost": pour,
            "counted": counted,
            "price_watch": prices,
            "insights": _insights(pour, prices, to_check),
            "distributors": _distributors(lines),
            "products": products,
            "to_check": to_check,
            "other_spend": other_spend,
            "categories": [{"key": c, "label": LABELS[c]} for c in ("beer", "liquor", "wine", "na", "supplies", "other")],
            "basis": (("Purchases are Fintech's distributor invoices; sales are the nightly report's sales by "
                       "category. Pour cost (counted) is what the bar used between two counts — opening count plus "
                       "purchases minus closing count — as a % of what it sold; the month table is bought vs sold.")
                      if pour.get("basis") == "counted" else
                      ("Purchases are Fintech's distributor invoices; sales are the nightly report's sales by "
                       "category. Bought vs sold is what the bar bought as a % of what it sold, over four weeks and "
                       "by month — it becomes pour cost (counted) once two bar counts bracket a window. The ranges "
                       "describe counted pour cost, so they are context for it."))}

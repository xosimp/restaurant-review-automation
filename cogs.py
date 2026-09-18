"""cogs.py — actual food cost %, the number the module is named after.

Until this existed the Food Cost module never computed food cost %. It
computed waste dollars, a waste rate against purchases, stock value and
suggested orders — all useful, none of them the figure an owner reads off
their P&L and compares to a target. `restaurants.food_cost_target` was
settable in client settings, shown in the admin console, and read by
nothing.

The identity is the ordinary one:

    COGS = opening inventory + purchases - closing inventory
    food cost % = COGS / net sales * 100

Every input is measured, never assumed. When a component is missing this
returns what is missing and why, and no percentage at all — a food cost %
built on a guessed opening inventory is worse than no food cost %, because
it looks exactly like a real one. That rule is the whole design: see
`missing` in the returned payload, and note that nothing here ever
substitutes 0 for an absent measurement.
"""
from datetime import date, timedelta

# A four-week window by default: long enough that one heavy delivery week
# doesn't dominate, short enough to still describe the current menu and
# current prices.
DEFAULT_WINDOW_DAYS = 28

# Full-service industry band for food cost as a share of sales. Used only to
# label a computed figure, never to invent one.
INDUSTRY_BAND = (28.0, 35.0)

# An inventory snapshot this far either side of the window edge is still a
# fair opening/closing measure. Beyond it the count is too stale to anchor a
# COGS figure and the whole calculation is withheld instead.
SNAPSHOT_TOLERANCE_DAYS = 10


def _f(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def purchases_in_window(restaurant_id, start, end, db_path=None):
    """Dollar value of everything received between two dates, inclusive.

    Priced at each ingredient's current unit_cost. Returns (dollars, n_events)
    so a caller can tell "nothing was bought" from "nothing was recorded".
    """
    from models import get_conn, DB_PATH
    conn = get_conn(db_path or DB_PATH)
    try:
        row = conn.execute(
            "SELECT COALESCE(SUM(e.qty * COALESCE(i.unit_cost, 0)), 0) AS total, COUNT(*) AS n "
            "FROM ingredient_stock_events e "
            "JOIN ingredients i ON i.id = e.ingredient_id AND i.restaurant_id = e.restaurant_id "
            "WHERE e.restaurant_id=? AND e.event_type='receiving' "
            "AND e.event_date >= ? AND e.event_date <= ?",
            (restaurant_id, start.isoformat(), end.isoformat()),
        ).fetchone()
    finally:
        conn.close()
    return round(_f(row["total"]), 2), int(row["n"] or 0)


def inventory_value_near(weeks, target_day, tolerance_days=SNAPSHOT_TOLERANCE_DAYS):
    """The recorded inventory value closest to `target_day`, or None.

    `weeks` is waste_trend.load_waste_history's series; entries carry
    inv_value only when their snapshot included per-item detail. Returns
    (value, snapshot_date) or (None, None).
    """
    best, best_gap, best_day = None, None, None
    for w in weeks or []:
        if w.get("inv_value") in (None, 0):
            continue
        try:
            day = date.fromisoformat(w["week_end"])
        except Exception:
            continue
        gap = abs((day - target_day).days)
        if gap <= tolerance_days and (best_gap is None or gap < best_gap):
            best, best_gap, best_day = _f(w["inv_value"]), gap, day
    return best, (best_day.isoformat() if best_day else None)


def _archived_net_sales(restaurant_id, start, end):
    """Net sales for the window from the LOCAL archive, or None.

    labor_daily_history carries one row per business date with that day's
    sales, written by each provider's nightly sync_to_db. Reading it is free
    and works when the POS is unreachable.

    Coverage is decided by the archive's own edges, not by counting dates: a
    restaurant closed on Mondays has no Monday row, so "every calendar date
    present" would never be true. If the archive spans the window — its
    earliest row is at or before `start` and its latest is at or after `end`
    — the nightly sync has been through this period and what is there is what
    there is. Anything narrower falls through to the live POS rather than
    quietly returning a smaller number, which is the one failure mode that
    matters here: an under-reported sales figure inflates food cost %.
    """
    from models import get_conn
    s, e = str(start)[:10], str(end)[:10]
    try:
        conn = get_conn()
        try:
            row = conn.execute(
                "SELECT MIN(date) AS first, MAX(date) AS last, "
                "       COALESCE(SUM(sales), 0) AS total, COUNT(*) AS n "
                "FROM labor_daily_history "
                "WHERE restaurant_id=? AND sales IS NOT NULL AND sales > 0 "
                "  AND date >= ? AND date <= ?", (restaurant_id, s, e)).fetchone()
            edges = conn.execute(
                "SELECT MIN(date) AS first, MAX(date) AS last FROM labor_daily_history "
                "WHERE restaurant_id=? AND sales IS NOT NULL AND sales > 0",
                (restaurant_id,)).fetchone()
        finally:
            conn.close()
    except Exception:
        return None
    if not row or not row["n"] or not edges or not edges["first"]:
        return None
    if str(edges["first"])[:10] > s or str(edges["last"])[:10] < e:
        return None          # the archive does not span this window
    total = _f(row["total"])
    return round(total, 2) if total > 0 else None


def net_sales_in_window(restaurant_id, start, end):
    """Net sales for the window, or None when it cannot be known. None means
    unknown — never 0.

    Reads the LOCAL ARCHIVE first and only calls the POS when the archive
    does not span the window.

    Two reasons, and the second is a vendor requirement rather than an
    optimisation. RPOWER's integrator asked explicitly that we "download and
    archive the data" rather than query it repeatedly, and told us there is
    no sandbox — every call is against a live store. Meanwhile this function
    is reached twice per profitability_projection, which is reached by
    business_intelligence.gather, which runs on every Ask Cavnar context
    build. That is a lot of traffic against an API whose rate limits we have
    not been given.

    Goes through pos.py rather than importing toast directly. That import was
    the reason food cost % was a Toast-only feature: a Square, Clover or
    RPOWER restaurant was told "no POS connected" by this function while
    pos.connected_provider knew perfectly well which POS they were on, so the
    one number this module is named after could never be computed for them.
    """
    archived = _archived_net_sales(restaurant_id, start, end)
    if archived is not None:
        return archived, None
    try:
        import pos
        by_date, _provider = pos.fetch_business_days(restaurant_id, start, end)
    except Exception as e:
        # POSCapabilityError carries the honest reason ("no POS connected",
        # or connected-but-cannot-report); anything else is a transport
        # failure. Both are "unknown", and both say which.
        import pos as _pos
        if isinstance(e, getattr(_pos, "POSCapabilityError", ())):
            return None, str(e)
        return None, f"POS request failed: {e}"
    if not by_date:
        return None, "POS returned no business days for this window"
    total = sum(_f(v) for v in by_date.values())
    if total <= 0:
        return None, "POS reported no sales for this window"
    return round(total, 2), None


def band_label(pct, target=None):
    """How a computed food cost % reads. `target` is the restaurant's own
    when they've set one; the industry band is the fallback."""
    if pct is None:
        return None, None
    if target:
        if pct <= target:
            return "On target", "good"
        if pct <= target + 2:
            return "Slightly over", "warn"
        return "Over target", "bad"
    low, high = INDUSTRY_BAND
    if pct <= low:
        return "Below the industry band", "good"
    if pct <= high:
        return "Within the industry band", "neutral"
    return "Above the industry band", "bad"


def build_food_cost_pct(restaurant_id, days=DEFAULT_WINDOW_DAYS, db_path=None, today=None):
    """Actual food cost % for one restaurant over the trailing `days`.

    Returns a payload that always says what it knows and what it doesn't:

        ok            — whether a percentage could be computed at all
        pct           — COGS / net sales * 100, or None
        cogs, opening, closing, purchases, net_sales — the components
        target        — restaurants.food_cost_target
        missing       — list of {component, why} for anything absent
        basis         — one sentence naming the identity and the window

    A missing component yields ok=False and pct=None. It never yields a
    number computed from a substituted zero.
    """
    from models import get_restaurant
    from waste_trend import load_waste_history

    today = today or date.today()
    end = today
    start = today - timedelta(days=days - 1)

    restaurant = get_restaurant(restaurant_id)
    target = None
    if restaurant is not None and getattr(restaurant, "food_cost_target", None):
        target = _f(restaurant.food_cost_target) or None

    missing = []
    weeks, _total = load_waste_history(restaurant_id, None, db_path=db_path)

    opening, opening_day = inventory_value_near(weeks, start)
    if opening is None:
        missing.append({
            "component": "opening inventory",
            "why": f"no counted inventory value within {SNAPSHOT_TOLERANCE_DAYS} days of {start.isoformat()}",
        })

    closing, closing_day = inventory_value_near(weeks, end)
    if closing is None:
        missing.append({
            "component": "closing inventory",
            "why": f"no counted inventory value within {SNAPSHOT_TOLERANCE_DAYS} days of {end.isoformat()}",
        })

    # Two snapshots that are actually the same count can't bracket a period.
    if opening_day and closing_day and opening_day == closing_day:
        missing.append({
            "component": "a second inventory count",
            "why": "opening and closing resolve to the same count, so nothing brackets the window",
        })
        closing = None

    purchases, n_receipts = purchases_in_window(restaurant_id, start, end, db_path=db_path)
    if n_receipts == 0:
        missing.append({
            "component": "purchases",
            "why": "no deliveries were recorded in this window",
        })

    net_sales, sales_why = net_sales_in_window(restaurant_id, start, end)
    if net_sales is None:
        missing.append({"component": "net sales", "why": sales_why or "unavailable"})

    basis = (f"COGS = opening inventory + purchases − closing inventory, over "
             f"{start.isoformat()} to {end.isoformat()}, divided by POS net sales for the same days.")

    payload = {
        "ok": False,
        "pct": None,
        "cogs": None,
        "opening": opening, "opening_date": opening_day,
        "closing": closing, "closing_date": closing_day,
        "purchases": purchases if n_receipts else None,
        "purchase_events": n_receipts,
        "net_sales": net_sales,
        "target": target,
        "window_days": days,
        "start": start.isoformat(), "end": end.isoformat(),
        "missing": missing,
        "basis": basis,
        "label": None, "tone": None, "variance_pts": None,
    }
    if missing:
        return payload

    cogs = round(opening + purchases - closing, 2)
    payload["cogs"] = cogs
    if cogs < 0:
        # Physically possible only if a count is wrong or deliveries went
        # unrecorded. Reporting a negative food cost % would be nonsense.
        payload["missing"] = [{
            "component": "a consistent count",
            "why": "closing inventory exceeds opening plus everything recorded as received, "
                   "which means a delivery went unrecorded or a count is wrong",
        }]
        return payload

    pct = round(cogs / net_sales * 100, 1)
    label, tone = band_label(pct, target)
    payload.update({
        "ok": True,
        "pct": pct,
        "label": label,
        "tone": tone,
        "variance_pts": round(pct - target, 1) if target else None,
    })
    return payload

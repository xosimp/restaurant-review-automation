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

    Priced at what each delivery cost when it arrived (the unit_cost the
    receiving event recorded). Priced at today's unit_cost, an invoice that
    raised a price re-priced every delivery already in the window (MOD-FC-23).
    Rows from before the event carried a price fall back to the current one.
    Returns (dollars, n_events) so a caller can tell "nothing was bought"
    from "nothing was recorded".
    """
    from models import get_conn, DB_PATH
    conn = get_conn(db_path or DB_PATH)
    try:
        row = conn.execute(
            "SELECT COALESCE(SUM(e.qty * COALESCE(e.unit_cost, i.unit_cost, 0)), 0) AS total, COUNT(*) AS n "
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


def _archived_net_sales(restaurant_id, start, end, today=None):
    """Net sales for the window from the LOCAL archive, or None.

    labor_daily_history carries one row per business date with that day's
    sales, written by each provider's nightly sync_to_db. Reading it is free
    and works when the POS is unreachable.

    Coverage takes two things. The archive must span the window — its
    earliest row at or before `start`, its latest at or after `end` — and it
    must have a row for (nearly) every day the restaurant trades inside it.
    Spanning alone was the whole test, so an archive with a three-week hole
    in the middle was summed as three weeks of zero sales and read food cost
    about eleven times too high (MOD-FC-16). "Every day it trades" is every
    date whose weekday has sales somewhere in the window or the eight weeks
    before it: a restaurant closed on Mondays has no Monday row and is not
    counted short for it. One missing day in ten (a holiday, a snow day) is
    allowed; more falls through to the live POS rather than quietly
    returning a smaller number — an under-reported sales figure inflates
    food cost %.

    A window ending today is judged through yesterday. The nightly sync
    archives each business date after it closes, so an archive complete
    through yesterday never reached `end`, and every load of a window
    ending today called the live POS (MOD-FC-17) — the traffic the RPOWER
    integrator asked us not to send. Today's sales are not in the figure
    until tonight's sync writes them.
    """
    from models import get_conn
    try:
        d0 = date.fromisoformat(str(start)[:10])
        d1 = date.fromisoformat(str(end)[:10])
    except ValueError:
        return None
    yesterday = (today or date.today()) - timedelta(days=1)
    if d1 > yesterday:
        d1 = yesterday
    if d1 < d0:
        return None
    s, e = d0.isoformat(), d1.isoformat()
    lookback = (d0 - timedelta(days=56)).isoformat()
    try:
        conn = get_conn()
        try:
            rows = conn.execute(
                "SELECT date, sales FROM labor_daily_history "
                "WHERE restaurant_id=? AND sales IS NOT NULL AND sales > 0 "
                "  AND date >= ? AND date <= ?", (restaurant_id, lookback, e)).fetchall()
            edges = conn.execute(
                "SELECT MIN(date) AS first, MAX(date) AS last FROM labor_daily_history "
                "WHERE restaurant_id=? AND sales IS NOT NULL AND sales > 0",
                (restaurant_id,)).fetchone()
        finally:
            conn.close()
    except Exception:
        return None
    if not edges or not edges["first"]:
        return None
    if str(edges["first"])[:10] > s or str(edges["last"])[:10] < e:
        return None          # the archive does not span this window
    by_day = {}
    for r in rows:
        try:
            by_day[date.fromisoformat(str(r["date"])[:10])] = by_day.get(
                date.fromisoformat(str(r["date"])[:10]), 0.0) + _f(r["sales"])
        except ValueError:
            continue
    in_window = {d: v for d, v in by_day.items() if d0 <= d <= d1}
    if not in_window:
        return None
    if not _covers_trading_days(in_window, by_day, d0, d1)[0]:
        return None          # a hole inside the span: not what the restaurant sold
    total = sum(in_window.values())
    return round(total, 2) if total > 0 else None


def _covers_trading_days(in_window, reference, d0, d1):
    """(ok, have, expected): whether `in_window` ({date: sales > 0}) has a
    figure for (nearly) every day the restaurant trades between d0 and d1.
    Trading days are the weekdays with sales anywhere in `reference` (a
    closed-Monday restaurant is not counted short for Mondays). One missing
    day in ten is allowed. The one coverage rule for net sales — the archive
    and the live POS both answer to it (CA3 F14)."""
    trading_weekdays = {d.weekday() for d in reference}
    expected = sum(1 for k in range((d1 - d0).days + 1)
                   if (d0 + timedelta(days=k)).weekday() in trading_weekdays)
    have = len(in_window)
    return have >= expected - max(1, expected // 10), have, expected


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
    # The same 90% coverage rule as the archive (CA3 F14). The live path
    # summed whatever days came back, so a POS that answered for 9 of 28
    # days gave a sales figure a third of the truth and a food cost % three
    # times too high. Judged through yesterday, like the archive: today's
    # sales are not in until tonight's close.
    live = {}
    for k, v in by_date.items():
        try:
            d = date.fromisoformat(str(k)[:10])
        except ValueError:
            continue
        if _f(v) > 0:
            live[d] = live.get(d, 0.0) + _f(v)
    total = sum(live.values())
    if total <= 0:
        return None, "POS reported no sales for this window"
    try:
        d0 = date.fromisoformat(str(start)[:10])
        d1 = min(date.fromisoformat(str(end)[:10]), date.today() - timedelta(days=1))
    except ValueError:
        return None, "window dates unreadable"
    if d1 >= d0:
        closed = {d: v for d, v in live.items() if d0 <= d <= d1}
        ok, have, expected = _covers_trading_days(closed, live, d0, d1)
        if not ok:
            return None, (f"POS returned sales for {have} of {expected} trading days in this window — "
                          "too few to state a food cost %")
    return round(total, 2), None


# Above this, COGS / net sales is not a food cost % but a data error.
MAX_MEASURABLE_PCT = 100.0


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
    # Only the snapshots that can be the opening or closing count are read:
    # within SNAPSHOT_TOLERANCE_DAYS of the window's edges (MOD-FC-18).
    weeks, _total = load_waste_history(
        restaurant_id, None, db_path=db_path,
        since=start - timedelta(days=SNAPSHOT_TOLERANCE_DAYS),
        until=end + timedelta(days=SNAPSHOT_TOLERANCE_DAYS))

    # Owner-facing reasons (they reach Track warnings and the Food Cost
    # tab): dates M/D/YY (re-audit A34).
    from time_utils import mdy as _mdy
    opening, opening_day = inventory_value_near(weeks, start)
    if opening is None:
        missing.append({
            "component": "opening inventory",
            "why": f"no counted inventory value within {SNAPSHOT_TOLERANCE_DAYS} days of {_mdy(start)}",
        })

    closing, closing_day = inventory_value_near(weeks, end)
    if closing is None:
        missing.append({
            "component": "closing inventory",
            "why": f"no counted inventory value within {SNAPSHOT_TOLERANCE_DAYS} days of {_mdy(end)}",
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
             f"{_mdy(start)} to {_mdy(end)}, divided by POS net sales for the same days.")

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
    if pct > MAX_MEASURABLE_PCT:
        # More food cost than sales is not a kitchen, it is a count, a
        # delivery or a sales window that is wrong. Refused as not
        # measurable rather than shown (CA3 F14).
        payload["cogs"] = cogs
        payload["missing"] = [{
            "component": "a consistent count",
            "why": f"food cost would be {pct:.0f}% of sales — a count, an unrecorded delivery "
                   "or missing sales days, not a measurement",
        }]
        return payload
    label, tone = band_label(pct, target)
    payload.update({
        "ok": True,
        "pct": pct,
        "label": label,
        "tone": tone,
        "variance_pts": round(pct - target, 1) if target else None,
    })
    return payload

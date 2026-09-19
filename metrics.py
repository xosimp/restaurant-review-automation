"""
metrics.py — one definition of each business number, measured over any window.

Goal tracking and outcome tracking both ask the same question — "what was
this number over that stretch of time?" — and if each answered it its own
way, "labour was 28% when you started and 26% now" could be two different
28%s. So both read from here.

Every metric returns (value, detail) where value is None when the number
cannot be known for that window. None is never 0: a restaurant that synced
no sales last month did not have $0 in sales, it has an unknown, and a goal
or an outcome built on a fabricated zero would report a triumph or a disaster
that never happened.

Windows are inclusive date strings (YYYY-MM-DD). Metric keys may carry a
parameter after a colon: "weekday_sales:Tuesday", "complaints:service".
"""
from datetime import date, datetime, timedelta

from models import get_conn, DB_PATH, REVIEW_TIME_AXIS_BARE


def _f(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _d(v):
    return str(v)[:10]


# ── the metrics ────────────────────────────────────────────────────────────

def _labor_pct(rid, start, end, param, db_path):
    conn = get_conn(db_path)
    try:
        row = conn.execute(
            "SELECT SUM(labor_cost) AS labor, SUM(sales) AS sales, COUNT(*) AS n "
            "FROM labor_daily_history WHERE restaurant_id=? AND date>=? AND date<=? "
            "AND sales IS NOT NULL AND sales > 0 AND labor_cost IS NOT NULL",
            (rid, _d(start), _d(end))).fetchone()
    finally:
        conn.close()
    if not row or not row["n"] or not _f(row["sales"]):
        return None, "no days with both labour and sales in this window"
    return round(_f(row["labor"]) / _f(row["sales"]) * 100, 1), f"{row['n']} days"


def _sales(rid, start, end, param, db_path):
    conn = get_conn(db_path)
    try:
        row = conn.execute(
            "SELECT SUM(sales) AS s, COUNT(*) AS n FROM labor_daily_history "
            "WHERE restaurant_id=? AND date>=? AND date<=? AND sales IS NOT NULL AND sales > 0",
            (rid, _d(start), _d(end))).fetchone()
    finally:
        conn.close()
    if not row or not row["n"]:
        return None, "no sales recorded in this window"
    # Sales are compared per DAY, not as a window total: a 25-day window and a
    # 28-day window are not the same size, and a total would read a shorter
    # window as a decline.
    return round(_f(row["s"]) / row["n"], 2), f"average per day over {row['n']} days"


def _weekday_sales(rid, start, end, param, db_path):
    day = (param or "").strip().capitalize()
    if not day:
        return None, "which weekday?"
    conn = get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT sales FROM labor_daily_history WHERE restaurant_id=? AND date>=? AND date<=? "
            "AND sales IS NOT NULL AND sales > 0 AND day_of_week=?",
            (rid, _d(start), _d(end), day)).fetchall()
    finally:
        conn.close()
    vals = sorted(_f(r["sales"]) for r in rows)
    if len(vals) < 2:
        return None, f"fewer than two {day}s with sales in this window"
    mid = len(vals) // 2
    med = vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2
    # Median, matching labor.build_demand_forecast: one private event should
    # not make a Tuesday look transformed.
    return round(med, 2), f"median of {len(vals)} {day}s"


def _avg_rating(rid, start, end, param, db_path):
    conn = get_conn(db_path)
    try:
        row = conn.execute(
            f"SELECT AVG(rating) AS r, COUNT(*) AS n FROM reviews "
            f"WHERE restaurant_id=? AND deleted_at IS NULL "
            f"AND date({REVIEW_TIME_AXIS_BARE})>=? AND date({REVIEW_TIME_AXIS_BARE})<=?",
            (rid, _d(start), _d(end))).fetchone()
    finally:
        conn.close()
    # A rating built on a handful of reviews is noise; the same floor the
    # rest of the product uses for "is this a trend".
    if not row or (row["n"] or 0) < 5:
        return None, f"only {row['n'] if row else 0} reviews in this window"
    return round(_f(row["r"]), 2), f"{row['n']} reviews"


def _complaints(rid, start, end, param, db_path):
    category = (param or "").strip().lower()
    conn = get_conn(db_path)
    try:
        rows = conn.execute(
            f"SELECT categories FROM reviews WHERE restaurant_id=? AND deleted_at IS NULL "
            f"AND processed=1 AND sentiment='negative' "
            f"AND date({REVIEW_TIME_AXIS_BARE})>=? AND date({REVIEW_TIME_AXIS_BARE})<=?",
            (rid, _d(start), _d(end))).fetchall()
        total = conn.execute(
            f"SELECT COUNT(*) AS n FROM reviews WHERE restaurant_id=? AND deleted_at IS NULL "
            f"AND date({REVIEW_TIME_AXIS_BARE})>=? AND date({REVIEW_TIME_AXIS_BARE})<=?",
            (rid, _d(start), _d(end))).fetchone()["n"]
    finally:
        conn.close()
    if (total or 0) < 5:
        return None, f"only {total} reviews in this window"
    import json
    hits = 0
    for r in rows:
        try:
            cats = [c.lower() for c in json.loads(r["categories"] or "[]")]
        except Exception:
            cats = []
        if not category or category in cats:
            hits += 1
    # A share of all reviews, not a count: a quieter month has fewer
    # complaints without anything having improved.
    return round(hits / total * 100, 1), f"{hits} of {total} reviews"


def _weekly_waste(rid, start, end, param, db_path):
    conn = get_conn(db_path)
    try:
        row = conn.execute(
            "SELECT COALESCE(SUM(e.qty * COALESCE(i.unit_cost,0)),0) AS cost, COUNT(*) AS n "
            "FROM ingredient_stock_events e JOIN ingredients i ON i.id=e.ingredient_id "
            "AND i.restaurant_id=e.restaurant_id WHERE e.restaurant_id=? AND e.event_type='waste' "
            "AND e.event_date>=? AND e.event_date<=?", (rid, _d(start), _d(end))).fetchone()
    finally:
        conn.close()
    if not row or not row["n"]:
        return None, "no waste logged in this window"
    days = (date.fromisoformat(_d(end)) - date.fromisoformat(_d(start))).days + 1
    return round(_f(row["cost"]) / max(days, 1) * 7, 2), f"{row['n']} waste events, per week"


def _food_cost_pct(rid, start, end, param, db_path):
    import cogs
    days = (date.fromisoformat(_d(end)) - date.fromisoformat(_d(start))).days + 1
    out = cogs.build_food_cost_pct(rid, days=days, db_path=db_path,
                                   today=date.fromisoformat(_d(end)))
    if not out or not out.get("ok"):
        why = "; ".join(m.get("why", "") for m in (out or {}).get("missing") or []) or "not computable"
        return None, why
    return out["pct"], out.get("basis") or ""


# key -> (function, label, unit, lower_is_better, noise, default window days)
#
# `noise` is the smallest move that counts as a move. Below it the honest
# reading is "no clear change", because weekly labour percentages wobble by
# half a point on their own and a rating wobbles by a tenth of a star.
_REGISTRY = {
    "labor_pct":     (_labor_pct,     "Labour %",           "%",    True,  0.5,  28),
    "food_cost_pct": (_food_cost_pct, "Food cost %",        "%",    True,  1.0,  28),
    "sales":         (_sales,         "Sales per day",      "$",    False, 0.05, 28),
    "weekday_sales": (_weekday_sales, "Sales on",           "$",    False, 0.08, 56),
    "avg_rating":    (_avg_rating,    "Average rating",     "★",    False, 0.1,  30),
    "complaints":    (_complaints,    "Complaint share",    "%",    True,  3.0,  60),
    "weekly_waste":  (_weekly_waste,  "Waste per week",     "$",    True,  0.10, 28),
}

# Relative noise: these are fractions of the baseline, not absolute amounts.
_RELATIVE_NOISE = {"sales", "weekday_sales", "weekly_waste"}


def parse(key):
    base, _, param = (key or "").partition(":")
    return base, (param or None)


_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def known(key) -> bool:
    """A metric key that can actually be measured. A parameterised key must
    carry a usable parameter: "weekday_sales:Funday" would be accepted as a
    goal and then read "unknown" forever."""
    base, param = parse(key)
    if base not in _REGISTRY:
        return False
    if base == "weekday_sales":
        return (param or "").strip().capitalize() in _WEEKDAYS
    return True


def describe(key) -> dict:
    base, param = parse(key)
    fn, label, unit, lower, noise, window = _REGISTRY[base]
    if param:
        label = f"{label} {param.capitalize()}" if base == "weekday_sales" else f"{label} ({param})"
    return {"key": key, "label": label, "unit": unit, "lower_is_better": lower,
            "noise": noise, "relative_noise": base in _RELATIVE_NOISE,
            "default_window_days": window}


def measure(restaurant_id, key, start, end, db_path=DB_PATH):
    """(value or None, detail) for one metric over one inclusive window."""
    base, param = parse(key)
    if base not in _REGISTRY:
        return None, f"unknown metric {key}"
    try:
        return _REGISTRY[base][0](restaurant_id, start, end, param, db_path)
    except Exception as e:
        return None, f"could not measure: {e}"


def trailing(restaurant_id, key, days=None, end=None, db_path=DB_PATH):
    """The metric over the `days` ending on `end` (default today)."""
    info = describe(key)
    end = date.fromisoformat(_d(end)) if end else date.today()
    days = days or info["default_window_days"]
    start = end - timedelta(days=days - 1)
    value, detail = measure(restaurant_id, key, start.isoformat(), end.isoformat(), db_path)
    return {"value": value, "detail": detail, "start": start.isoformat(), "end": end.isoformat()}


def compare(key, before, after):
    """How a move from `before` to `after` reads, honestly.

    Returns {"verdict", "delta", "delta_pct"} where verdict is one of
    improved | worsened | no_clear_change | unknown. A move inside the
    metric's noise band is "no_clear_change" even if the sign is right —
    claiming a win on a wobble is how an owner stops trusting the number.
    """
    if before is None or after is None:
        return {"verdict": "unknown", "delta": None, "delta_pct": None}
    info = describe(key)
    delta = round(after - before, 2)
    delta_pct = round(delta / before * 100, 1) if before else None
    threshold = info["noise"] * abs(before) if info["relative_noise"] else info["noise"]
    if abs(delta) < threshold:
        verdict = "no_clear_change"
    else:
        better = (delta < 0) if info["lower_is_better"] else (delta > 0)
        verdict = "improved" if better else "worsened"
    return {"verdict": verdict, "delta": delta, "delta_pct": delta_pct}


def monthly_dollars(restaurant_id, key, delta, db_path=DB_PATH):
    """Rough monthly dollar value of a move, or None when it has no honest
    dollar reading. Always an estimate, and labelled as one by callers."""
    if delta is None:
        return None
    base, _ = parse(key)
    if base in ("labor_pct", "food_cost_pct"):
        # A point of labour or food cost is worth a point of monthly sales.
        s = trailing(restaurant_id, "sales", days=28, db_path=db_path)["value"]
        if s is None:
            return None
        info = describe(key)
        sign = -1 if info["lower_is_better"] else 1
        return round(sign * delta / 100 * s * 30, 2)
    if base == "sales":
        return round(delta * 30, 2)
    if base == "weekday_sales":
        return round(delta * 4.33, 2)
    if base == "weekly_waste":
        return round(-delta * 4.33, 2)
    return None

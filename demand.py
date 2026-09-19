"""
demand.py — what the day will ask of the restaurant, and what yesterday did.

labor.build_demand_forecast has computed per-weekday median sales for months,
and the only thing that ever read it was the schedule prompt. The same number
answers three more questions an owner actually asks:

  * Was yesterday a bad day, or a normal Tuesday?      -> yesterday_vs_typical
  * Which days need help?                               -> slow_days
  * What will the kitchen go through tomorrow?          -> prep_list

Medians throughout, matching the forecast: one catered event or one storm-shut
Saturday should not redefine what a normal day looks like. Every result says
how many days it rests on and returns nothing when that is too few.
"""
from datetime import date, timedelta

from models import get_conn, DB_PATH

# Yesterday is "off" only past this far from its typical weekday. Day-to-day
# sales swing ±10-15% on their own.
OFF_DAY_PCT = 20
# A weekday is "slow" past this far below the typical day, on enough samples.
SLOW_DAY_PCT = 15
MIN_SAMPLES = 3
LOOKBACK_WEEKS = 8


def _median(vals):
    s = sorted(vals)
    if not s:
        return None
    mid = len(s) // 2
    return s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2


def _weekday_history(restaurant_id, weekday, before, weeks=LOOKBACK_WEEKS, db_path=DB_PATH):
    """Sales on `weekday` in the `weeks` before `before` (exclusive), so a day
    is never compared with a baseline that contains itself."""
    start = (before - timedelta(weeks=weeks)).isoformat()
    conn = get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT date, sales FROM labor_daily_history WHERE restaurant_id=? AND day_of_week=? "
            "AND date>=? AND date<? AND sales IS NOT NULL AND sales > 0",
            (restaurant_id, weekday, start, before.isoformat())).fetchall()
    finally:
        conn.close()
    return [float(r["sales"]) for r in rows]


def forecast_day(restaurant_id, day=None, db_path=DB_PATH):
    """Typical sales for `day` (default today), from its own weekday history."""
    day = day or date.today()
    weekday = day.strftime("%A")
    hist = _weekday_history(restaurant_id, weekday, day, db_path=db_path)
    if len(hist) < MIN_SAMPLES:
        return {"available": False, "day": day.isoformat(), "weekday": weekday,
                "reason": f"only {len(hist)} past {weekday}s with sales on file"}
    return {"available": True, "day": day.isoformat(), "weekday": weekday,
            "typical_sales": round(_median(hist), 2), "samples": len(hist),
            "low": round(min(hist), 2), "high": round(max(hist), 2)}


def yesterday_vs_typical(restaurant_id, today=None, db_path=DB_PATH):
    """Yesterday's sales against a typical day of the same weekday.

    Returns off=True only past OFF_DAY_PCT, and says which way. Never
    compares a day that has no sales recorded — an unsynced day is unknown,
    not a catastrophe.
    """
    today = today or date.today()
    y = today - timedelta(days=1)
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT sales FROM labor_daily_history WHERE restaurant_id=? AND date=? "
                           "AND sales IS NOT NULL AND sales > 0", (restaurant_id, y.isoformat())).fetchone()
    finally:
        conn.close()
    if not row:
        return {"available": False, "reason": "no sales recorded for yesterday yet"}
    fc = forecast_day(restaurant_id, y, db_path=db_path)
    if not fc["available"]:
        return {"available": False, "reason": fc["reason"]}
    actual = float(row["sales"])
    pct = round((actual / fc["typical_sales"] - 1) * 100, 1)
    return {"available": True, "date": y.isoformat(), "weekday": fc["weekday"],
            "actual": round(actual, 2), "typical": fc["typical_sales"], "pct": pct,
            "samples": fc["samples"], "off": abs(pct) >= OFF_DAY_PCT,
            "direction": "above" if pct > 0 else "below"}


def slow_days(restaurant_id, db_path=DB_PATH):
    """Weekdays that run materially below this restaurant's typical day."""
    import labor
    fc = labor.build_demand_forecast(restaurant_id, weeks=LOOKBACK_WEEKS, db_path=db_path)
    if not fc.get("ok"):
        return {"available": False, "reason": fc.get("reason", "not enough history")}
    slow = [d for d in fc.get("days", [])
            if d["vs_average_pct"] <= -SLOW_DAY_PCT and d["samples"] >= MIN_SAMPLES]
    return {"available": True, "slow_days": slow, "all_days": fc.get("days", []),
            "threshold_pct": SLOW_DAY_PCT}


def prep_list(restaurant_id, day=None, db_path=DB_PATH, limit=15):
    """Expected ingredient usage for `day` against what's on hand.

    Built from each dish's typical quantity sold on that weekday (menu item
    sales) times its recipe. This is a USAGE forecast — it does not model
    sub-recipes or batch sizes, and says so — but it answers the question the
    kitchen asks the night before: what are we going to run out of.
    """
    day = day or (date.today() + timedelta(days=1))
    weekday_num = int(day.strftime("%w"))
    start = (day - timedelta(weeks=LOOKBACK_WEEKS)).isoformat()
    conn = get_conn(db_path)
    try:
        sales = conn.execute(
            "SELECT menu_item_id, business_date, SUM(qty_sold) AS q FROM menu_item_sales "
            "WHERE restaurant_id=? AND business_date>=? AND business_date<? "
            "AND CAST(strftime('%w', business_date) AS INTEGER)=? "
            "GROUP BY menu_item_id, business_date", (restaurant_id, start, day.isoformat(), weekday_num)).fetchall()
        recipes = conn.execute(
            "SELECT ri.menu_item_id, ri.ingredient_id, ri.qty_per_unit, i.name, i.unit, i.current_stock "
            "FROM recipe_ingredients ri JOIN ingredients i ON i.id=ri.ingredient_id "
            "JOIN menu_items m ON m.id=ri.menu_item_id WHERE m.restaurant_id=? AND m.is_active=1",
            (restaurant_id,)).fetchall()
    finally:
        conn.close()
    if not sales:
        return {"available": False, "day": day.isoformat(),
                "reason": "no dish-level sales on file for this weekday yet"}

    # Zero-filled across every past weekday that had ANY dish sales: a dish
    # that sold on 2 of 8 Fridays sold nothing on the other 6, and a median of
    # only the days it appeared would forecast it as a Friday staple.
    dates = sorted({r["business_date"] for r in sales})
    if len(dates) < 2:
        return {"available": False, "day": day.isoformat(),
                "reason": "fewer than two past weekdays of dish sales"}
    sold = {}
    for r in sales:
        sold.setdefault(r["menu_item_id"], {})[r["business_date"]] = float(r["q"] or 0)
    expected = {mid: _median([by_date.get(d, 0.0) for d in dates]) for mid, by_date in sold.items()}
    expected = {mid: q for mid, q in expected.items() if q}

    need = {}
    for rec in recipes:
        qty = expected.get(rec["menu_item_id"])
        if not qty:
            continue
        n = need.setdefault(rec["ingredient_id"], {"ingredient": rec["name"], "unit": rec["unit"],
                                                  "on_hand": float(rec["current_stock"] or 0),
                                                  "expected_use": 0.0})
        n["expected_use"] += qty * float(rec["qty_per_unit"] or 0)
    rows = []
    for n in need.values():
        n["expected_use"] = round(n["expected_use"], 2)
        n["shortfall"] = round(max(0.0, n["expected_use"] - n["on_hand"]), 2)
        n["covered"] = n["shortfall"] == 0
        rows.append(n)
    rows.sort(key=lambda n: (n["covered"], -n["shortfall"], -n["expected_use"]))
    return {"available": True, "day": day.isoformat(), "weekday": day.strftime("%A"),
            "items": rows[:limit], "dishes_forecast": len(expected),
            "note": ("A usage forecast from each dish's typical sales on this weekday times its "
                     "recipe. It does not model sub-recipes or batch sizes.")}


# Far enough ahead that a guest-club send, a post or a staffing change can
# still be made; close enough that the forecast is about a real night.
OPPORTUNITY_LEAD_DAYS = 2


def quiet_night_ahead(restaurant_id, today=None, db_path=DB_PATH):
    """The night coming up that is reliably this restaurant's quietest, when
    there is still time to do something about it.

    Marketing and demand were the one area of the product that produced no
    notification at all: an owner had to go and look, and the whole point of
    a slow Tuesday is that it is knowable in advance. Same honesty as
    everything else here — a weekday with too little history is not called
    slow, and a restaurant whose days are all within normal variation gets
    nothing rather than a manufactured opportunity.
    """
    today = today or date.today()
    target = today + timedelta(days=OPPORTUNITY_LEAD_DAYS)
    weekday = target.strftime("%A")
    slow = slow_days(restaurant_id, db_path=db_path)
    if not slow.get("available"):
        return {"available": False, "reason": slow.get("reason", "not enough history")}
    match = next((d for d in slow.get("slow_days") or [] if d.get("day") == weekday), None)
    if not match:
        return {"available": False, "reason": f"{weekday} is not one of the quiet ones"}
    fc = forecast_day(restaurant_id, target, db_path=db_path)
    if not fc.get("available"):
        return {"available": False, "reason": fc.get("reason")}
    return {"available": True, "date": target.isoformat(), "weekday": weekday,
            "typical_sales": fc["typical_sales"], "samples": fc["samples"],
            "below_average_pct": abs(match.get("vs_average_pct") or 0)}

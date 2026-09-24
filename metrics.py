"""
metrics.py — one definition of each business number, measured over any window.

Goal tracking and outcome tracking both ask the same question — "what was
this number over that stretch of time?" — and if each answered it its own
way, "labor was 28% when you started and 26% now" could be two different
28%s. So both read from here.

Every metric returns (value, detail) where value is None when the number
cannot be known for that window. None is never 0: a restaurant that synced
no sales last month did not have $0 in sales, it has an unknown, and a goal
or an outcome built on a fabricated zero would report a triumph or a disaster
that never happened.

Windows are inclusive date strings (YYYY-MM-DD). Metric keys may carry a
parameter after a colon: "weekday_sales:Tuesday", "complaints:service".
"""
from datetime import date, timedelta

import models as _models_mod
from models import DB_PATH, REVIEW_TIME_AXIS_BARE


def get_conn(db_path=None):
    """models.get_conn resolved at call time (CLAUDE.md, bound imports)."""
    if db_path is None or db_path == DB_PATH:
        return _models_mod.get_conn()
    return _models_mod.get_conn(db_path)


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
        return None, "no days with both labor and sales in this window"
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


def _category_id(v) -> str:
    """"Food quality", "food-quality" and "food_quality" are one category:
    reviews store the underscore id, and a tracker started from a display
    label read 0% before and after — a false "no clear change"."""
    return "_".join(str(v or "").strip().lower().replace("-", " ").split())


def _complaints(rid, start, end, param, db_path):
    category = _category_id(param)
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
            cats = [_category_id(c) for c in json.loads(r["categories"] or "[]")]
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


def _loss_rate(kind):
    """comps / voids as a percentage of sales over the window.

    loss_detection has measured real dollars per day since it shipped
    (pos_loss_daily) and none of it was measurable as a metric, so an owner
    who tightened comp approval had no way to prove it worked — the one
    module whose numbers are already in dollars was the one module outcome
    tracking could not read.

    A RATE, not a total: comps fall on a quiet week without anything having
    changed. The denominator is the same labor_daily_history sales every
    other metric here uses, so "comps are 2% of sales" means the same 2%
    the labor percentage is measured against.
    """
    def fn(rid, start, end, param, db_path):
        conn = get_conn(db_path)
        try:
            row = conn.execute(
                "SELECT COALESCE(SUM(amount),0) AS amt, COALESCE(SUM(events),0) AS n "
                "FROM pos_loss_daily WHERE restaurant_id=? AND kind=? "
                "AND business_date>=? AND business_date<=?",
                (rid, kind, _d(start), _d(end))).fetchone()
            sales = conn.execute(
                "SELECT SUM(sales) AS s, COUNT(*) AS n FROM labor_daily_history "
                "WHERE restaurant_id=? AND date>=? AND date<=? AND sales IS NOT NULL AND sales > 0",
                (rid, _d(start), _d(end))).fetchone()
        finally:
            conn.close()
        # No synced loss rows at all is unknown, not zero: a POS that does
        # not report comps looks identical to a restaurant with none.
        if not row or not (row["n"] or 0):
            return None, f"no {kind} data synced in this window"
        if not sales or not _f(sales["s"]):
            return None, "no sales in this window to measure against"
        return round(_f(row["amt"]) / _f(sales["s"]) * 100, 2), \
            f"{int(row['n'])} {kind}s over {sales['n']} days of sales"
    return fn


def _food_cost_pct(rid, start, end, param, db_path):
    import cogs
    days = (date.fromisoformat(_d(end)) - date.fromisoformat(_d(start))).days + 1
    out = cogs.build_food_cost_pct(rid, days=days, db_path=db_path,
                                   today=date.fromisoformat(_d(end)))
    if not out or not out.get("ok"):
        why = "; ".join(m.get("why", "") for m in (out or {}).get("missing") or []) or "not computable"
        return None, why
    return out["pct"], out.get("basis") or ""


def _overtime_hours(rid, start, end, param, db_path):
    """Overtime hours per payroll week, from the restaurant's own shifts.

    Overtime is a WEEKLY quantity — hours past 40 in the employer's payroll
    week (labor.OVERTIME_THRESHOLD_HOURS, on labor.get_week_start_day's
    week) — so only payroll weeks that sit wholly inside the window are
    read. A week that has no shifts on file is unknown, not a week with no
    overtime: it is left out of the average rather than counted as zero. A
    week that HAS shifts and nobody past 40 is a measured zero."""
    import labor
    import models
    cd = models.get_client_data(rid, db_path=db_path)
    if not cd or not cd.get("shifts_csv"):
        return None, "no shifts uploaded"
    try:
        shifts = labor.load_shifts(csv_string=cd["shifts_csv"])
    except Exception as e:
        return None, f"shifts could not be read: {e}"
    r = models.get_restaurant(rid, db_path)
    wsd = int(getattr(r, "week_start_day", 0) or 0) if r else 0
    s, e = date.fromisoformat(_d(start)), date.fromisoformat(_d(end))
    hours, weeks, estimated = {}, set(), False
    for sh in shifts:
        day = str(sh.get("date") or "")[:10]
        try:
            date.fromisoformat(day)
        except ValueError:
            continue
        wk = date.fromisoformat(labor._week_key(day, wsd))
        # The whole payroll week must lie inside the window: a week cut in
        # half by the window edge reads as half its overtime.
        if wk < s or wk + timedelta(days=6) > e:
            continue
        weeks.add(wk)
        key = (sh.get("employee") or "Unknown", wk)
        hours[key] = hours.get(key, 0.0) + labor._shift_hours(sh)
        if not labor._has_actual_hours(sh):
            estimated = True
    if not weeks:
        return None, "no complete payroll week with shifts in this window"
    ot = sum(max(0.0, h - labor.OVERTIME_THRESHOLD_HOURS) for h in hours.values())
    detail = f"{ot:g} overtime hours over {len(weeks)} payroll week{'s' if len(weeks) != 1 else ''}"
    if estimated:
        detail += " (some from scheduled hours, not a timesheet)"
    return round(ot / len(weeks), 1), detail


def _response_hours(rid, start, end, param, db_path):
    """Median hours from a review being written to its reply being approved,
    over the reviews written in the window that have a reply.

    The same two timestamps models.get_review_stats averages (review time to
    approved_at), as a MEDIAN: one reply approved a week late should not
    make a month of same-day replies read as slow. Replies later than
    models.RESPONSE_TIME_CAP_HOURS say more about when data was imported
    than about how fast anyone answered, and are left out, as they are
    there. Unanswered reviews are named in the detail, never counted as a
    time: a review still waiting has no reply time yet."""
    from models import RESPONSE_TIME_CAP_HOURS
    conn = get_conn(db_path)
    try:
        rows = conn.execute(
            f"SELECT (julianday(approved_at) - julianday({REVIEW_TIME_AXIS_BARE})) * 24 AS h "
            f"FROM reviews WHERE restaurant_id=? AND deleted_at IS NULL "
            f"AND response_status IN ('approved','posted') AND approved_at IS NOT NULL "
            f"AND date({REVIEW_TIME_AXIS_BARE})>=? AND date({REVIEW_TIME_AXIS_BARE})<=?",
            (rid, _d(start), _d(end))).fetchall()
        waiting = conn.execute(
            f"SELECT COUNT(*) AS n FROM reviews WHERE restaurant_id=? AND deleted_at IS NULL "
            f"AND response_status IN ('pending','drafted') "
            f"AND date({REVIEW_TIME_AXIS_BARE})>=? AND date({REVIEW_TIME_AXIS_BARE})<=?",
            (rid, _d(start), _d(end))).fetchone()["n"] or 0
    finally:
        conn.close()
    vals = sorted(v for v in (_f(r["h"]) for r in rows)
                  if v is not None and 0 <= v <= RESPONSE_TIME_CAP_HOURS)
    if len(vals) < 5:
        return None, f"only {len(vals)} replies in this window"
    mid = len(vals) // 2
    med = vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2
    detail = f"median of {len(vals)} replies"
    if waiting:
        detail += f"; {waiting} review{'s' if waiting != 1 else ''} from this window not answered"
    return round(med, 1), detail


# key -> (function, label, unit, lower_is_better, noise, default window days)
#
# `noise` is the smallest move that counts as a move. Below it the honest
# reading is "no clear change", because weekly labor percentages wobble by
# half a point on their own and a rating wobbles by a tenth of a star.
_REGISTRY = {
    "labor_pct":     (_labor_pct,     "Labor %",           "%",    True,  0.5,  28),
    "food_cost_pct": (_food_cost_pct, "Food cost %",        "%",    True,  1.0,  28),
    "sales":         (_sales,         "Sales per day",      "$",    False, 0.05, 28),
    "weekday_sales": (_weekday_sales, "Sales on",           "$",    False, 0.08, 56),
    "avg_rating":    (_avg_rating,    "Average rating",     "★",    False, 0.1,  30),
    "complaints":    (_complaints,    "Complaint share",    "%",    True,  3.0,  60),
    "weekly_waste":  (_weekly_waste,  "Waste per week",     "$",    True,  0.10, 28),
    # Comps and voids as a share of sales. The noise band is wide on
    # purpose: a single large comp moves a small restaurant's week, and
    # this is the one metric where a false "improved" reads as an
    # accusation that someone was over-comping and stopped.
    "comp_rate":     (_loss_rate("comp"), "Comps",          "%",    True,  0.3,  28),
    "void_rate":     (_loss_rate("void"), "Voids",          "%",    True,  0.3,  28),
    # Overtime hours per payroll week (labor.py's 40-hour line). Relative
    # noise with a floor (_NOISE_FLOOR): 15% of the baseline, never less
    # than two hours a week.
    "overtime_hours": (_overtime_hours, "Overtime hours per week", "h", True, 0.15, 28),
    # Median hours from a review to its reply. No dollars (monthly_dollars
    # returns None): a faster reply is worth something, but nothing here
    # can say how much.
    "response_hours": (_response_hours, "Reply time", "h", True, 0.25, 30),
}

# Relative noise: these are fractions of the baseline, not absolute amounts.
_RELATIVE_NOISE = {"sales", "weekday_sales", "weekly_waste", "overtime_hours", "response_hours"}
# The smallest band a relative metric can have, in its own unit. A relative
# band on a baseline near zero is no band at all: 15% of half an overtime
# hour would call a half-hour move a result.
_NOISE_FLOOR = {"overtime_hours": 2.0, "response_hours": 2.0}

# Metric FAMILIES: numbers that measure the same money or the same guest
# experience, so one change moving both is one result, not two (rec-ROI
# audit #4). Waste is part of food cost; overtime is part of labor cost; one
# weekday's sales are part of sales; complaints and the average rating read
# the same reviews. outcomes.py counts one win per family per overlapping
# window, and refuses an automatic second tracker in a family already being
# measured.
FAMILIES = {
    "labor_pct": "labor_cost", "overtime_hours": "labor_cost",
    "food_cost_pct": "food_cost", "weekly_waste": "food_cost",
    "sales": "sales", "weekday_sales": "sales",
    "avg_rating": "guest_rating", "complaints": "guest_rating",
    "response_hours": "reply_speed",
    "comp_rate": "comps", "void_rate": "voids",
}
FAMILY_LABELS = {"labor_cost": "labor cost", "food_cost": "food cost", "sales": "sales",
                 "guest_rating": "guest rating", "reply_speed": "reply time",
                 "comps": "comps", "voids": "voids"}
# Measured per trading day (labor_daily_history): a day with no row is a day
# not measured. Every other metric is read over its window as a whole.
PER_DAY_METRICS = {"labor_pct", "sales", "weekday_sales"}
# Metrics a season moves: their baseline is matched by weekday and, where a
# year of history allows, adjusted by what the same weeks did last year
# (outcomes.record, audit #30).
SEASONAL_METRICS = {"labor_pct", "food_cost_pct", "sales"}

# ONE calendar. A per-day figure was being annualised at 30 days a month
# while a per-week figure used 52/12 weeks — which is 30.33 days. Two
# constants for one month meant a daily saving and a weekly saving of the
# same size came out different, and inventory.WEEKS_PER_MONTH (52/12) is
# the one the rest of the product already uses.
WEEKS_PER_MONTH = 52.0 / 12.0
DAYS_PER_MONTH = WEEKS_PER_MONTH * 7.0


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


def family(key) -> str:
    """The family a metric belongs to (FAMILIES) — the metric's own base key
    when it has none, so an unlisted metric is only ever its own family."""
    base, _ = parse(key)
    return FAMILIES.get(base, base)


def describe(key) -> dict:
    base, param = parse(key)
    fn, label, unit, lower, noise, window = _REGISTRY[base]
    if param:
        label = f"{label} {param.capitalize()}" if base == "weekday_sales" else f"{label} ({param})"
    return {"key": key, "label": label, "unit": unit, "lower_is_better": lower,
            "noise": noise, "relative_noise": base in _RELATIVE_NOISE,
            "noise_floor": _NOISE_FLOOR.get(base, 0.0), "family": FAMILIES.get(base, base),
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


def compare(key, before, after, band_scale=1.0):
    """How a move from `before` to `after` reads, honestly.

    Returns {"verdict", "delta", "delta_pct", "band", "multiple"} where
    verdict is one of improved | worsened | no_clear_change | unknown. A
    move inside the metric's noise band is "no_clear_change" even if the
    sign is right — claiming a win on a wobble is how an owner stops
    trusting the number. `band` is the noise band this comparison used and
    `multiple` how many bands the move covered (outcomes grades attribution
    on it). `band_scale` widens the band for a comparison whose baseline
    carries noise of its own: a baseline adjusted by last year's same weeks
    adds last year's wobble to this year's (outcomes.SEASONAL_BAND_SCALE).
    """
    if before is None or after is None:
        return {"verdict": "unknown", "delta": None, "delta_pct": None, "band": None, "multiple": None}
    info = describe(key)
    delta = round(after - before, 2)
    delta_pct = round(delta / before * 100, 1) if before else None
    threshold = info["noise"] * abs(before) if info["relative_noise"] else info["noise"]
    threshold = max(threshold, info["noise_floor"]) * float(band_scale or 1.0)
    if abs(delta) < threshold:
        verdict = "no_clear_change"
    else:
        better = (delta < 0) if info["lower_is_better"] else (delta > 0)
        verdict = "improved" if better else "worsened"
    multiple = round(abs(delta) / threshold, 2) if threshold else None
    return {"verdict": verdict, "delta": delta, "delta_pct": delta_pct,
            "band": round(threshold, 4), "multiple": multiple}


def _day_sql(base, param):
    sql = ("FROM labor_daily_history WHERE restaurant_id=? AND date>=? AND date<=? "
           "AND sales IS NOT NULL AND sales > 0")
    extra = []
    if base == "labor_pct":
        sql += " AND labor_cost IS NOT NULL"
    if base == "weekday_sales":
        sql += " AND day_of_week=?"
        extra.append((param or "").strip().capitalize())
    return sql, extra


def data_days(restaurant_id, key, start, end, db_path=DB_PATH):
    """The ISO dates in the window that carry this metric's data, for the
    per-day metrics (PER_DAY_METRICS); None for a metric read over its
    window as a whole. A 28-day window with 24 trading days measured 24
    days, and outcomes' cumulative accrual counts 24 (audit #14)."""
    base, param = parse(key)
    if base not in PER_DAY_METRICS:
        return None
    sql, extra = _day_sql(base, param)
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT DISTINCT date " + sql,
                            [restaurant_id, _d(start), _d(end)] + extra).fetchall()
    finally:
        conn.close()
    return sorted(str(r["date"])[:10] for r in rows)


def days_with_data(restaurant_id, key, start, end, db_path=DB_PATH):
    """len(data_days(...)), or None for a window metric."""
    days = data_days(restaurant_id, key, start, end, db_path)
    return None if days is None else len(days)


# The overtime premium: the extra half of time-and-a-half, the same
# (labor.OVERTIME_MULTIPLIER - 1) labor.analyse_shifts adds to the cost of
# every hour past 40. Only the premium is priced: an overtime hour moved to
# someone on straight time still costs its base wage, and an hour cut
# altogether shows in labor % (the same family), which is priced there.
OVERTIME_BASIS = ("the overtime premium only: the extra half of time-and-a-half on each hour past 40, "
                  "at the blended hourly wage")


def overtime_premium_per_hour(restaurant_id, db_path=DB_PATH):
    """Dollars the premium costs per overtime hour for this restaurant: the
    blended wage (models.compute_blended_rate over its shifts, per-role
    rates where set, else the restaurant's hourly rate) times
    (labor.OVERTIME_MULTIPLIER - 1). None when there is no restaurant."""
    import labor
    import models
    r = models.get_restaurant(restaurant_id, db_path)
    if not r:
        return None
    fallback = float(getattr(r, "hourly_rate", None) or labor.DEFAULT_HOURLY_RATE)
    rate = fallback
    try:
        cd = models.get_client_data(restaurant_id, db_path=db_path)
        if cd and cd.get("shifts_csv"):
            shifts = labor.load_shifts(csv_string=cd["shifts_csv"])
            rate = models.compute_blended_rate(shifts, models.get_role_rates(restaurant_id, db_path),
                                               fallback=fallback) or fallback
    except Exception as e:
        print(f"[metrics] blended wage unreadable for {restaurant_id}, using the hourly rate: {e}")
        rate = fallback
    return round(float(rate) * (labor.OVERTIME_MULTIPLIER - 1.0), 4)


def monthly_dollars(restaurant_id, key, delta, db_path=DB_PATH):
    """Rough monthly dollar value of a move, or None when it has no honest
    dollar reading. Always an estimate, and labelled as one by callers."""
    if delta is None:
        return None
    base, _ = parse(key)
    if base in ("labor_pct", "food_cost_pct", "comp_rate", "void_rate"):
        # A point of labor, food cost, comps or voids is worth a point of
        # monthly sales. Comps and voids are already a share of the same
        # sales denominator, so they convert identically.
        s = trailing(restaurant_id, "sales", days=28, db_path=db_path)["value"]
        if s is None:
            return None
        info = describe(key)
        sign = -1 if info["lower_is_better"] else 1
        return round(sign * delta / 100 * s * DAYS_PER_MONTH, 2)
    if base == "sales":
        return round(delta * DAYS_PER_MONTH, 2)
    if base == "weekday_sales":
        # One weekday recurs WEEKS_PER_MONTH times a month, not DAYS.
        return round(delta * WEEKS_PER_MONTH, 2)
    if base == "weekly_waste":
        return round(-delta * WEEKS_PER_MONTH, 2)
    if base == "overtime_hours":
        # Hours a week x the premium an overtime hour carries x weeks a
        # month. Lower is better: a fall is a saving.
        per_hour = overtime_premium_per_hour(restaurant_id, db_path)
        if per_hour is None:
            return None
        return round(-delta * per_hour * WEEKS_PER_MONTH, 2)
    return None

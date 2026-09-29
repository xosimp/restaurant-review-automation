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
from thresholds import RATING_MIN_REVIEWS


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
    # A rating built on a handful of reviews is noise; the one floor the
    # nightly report's rating reads too (thresholds.RATING_MIN_REVIEWS).
    if not row or (row["n"] or 0) < RATING_MIN_REVIEWS:
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


# A waste reading is priced only when nearly every event in it carries a
# unit cost. Waste logged on an ingredient with no cost used to price at $0
# (the column's DEFAULT), so a window of uncosted waste read as a measured
# "$0 a week" — and "$0 → $0" as worse (re-audit A9).
WASTE_MIN_COSTED_SHARE = 0.9


def _weekly_waste(rid, start, end, param, db_path):
    conn = get_conn(db_path)
    try:
        row = conn.execute(
            "SELECT COALESCE(SUM(CASE WHEN COALESCE(i.unit_cost,0) > 0 THEN e.qty * i.unit_cost END),0) AS cost, "
            "COUNT(*) AS n, SUM(CASE WHEN COALESCE(i.unit_cost,0) > 0 THEN 1 ELSE 0 END) AS costed "
            "FROM ingredient_stock_events e JOIN ingredients i ON i.id=e.ingredient_id "
            "AND i.restaurant_id=e.restaurant_id WHERE e.restaurant_id=? AND e.event_type='waste' "
            "AND e.event_date>=? AND e.event_date<=?", (rid, _d(start), _d(end))).fetchone()
    finally:
        conn.close()
    if not row or not row["n"]:
        return None, "no waste logged in this window"
    n, costed = int(row["n"]), int(row["costed"] or 0)
    if costed < WASTE_MIN_COSTED_SHARE * n:
        return None, (f"{n - costed} of {n} waste events are on ingredients with no unit cost, "
                      f"so the waste can't be priced")
    days = (date.fromisoformat(_d(end)) - date.fromisoformat(_d(start))).days + 1
    detail = f"{n} waste events, per week"
    if costed < n:
        detail += f" ({n - costed} without a unit cost not priced)"
    return round(_f(row["cost"]) / max(days, 1) * 7, 2), detail


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

    Numerator and denominator are read over the SAME days: the days the POS
    was asked about comps (loss_detection writes a row for every day it
    asks, zero included). A comp sync that stopped ten days into a window
    while sales kept syncing divided ten days of comps by the whole
    window's sales — a fall that never happened (re-audit A3). A day asked
    about with no comps is a measured zero; no day asked at all is unknown.
    """
    def fn(rid, start, end, param, db_path):
        conn = get_conn(db_path)
        try:
            row = conn.execute(
                "SELECT COALESCE(SUM(p.amount),0) AS amt, COALESCE(SUM(p.events),0) AS n, "
                "COUNT(*) AS days, SUM(l.sales) AS s FROM pos_loss_daily p "
                "JOIN labor_daily_history l ON l.restaurant_id=p.restaurant_id AND l.date=p.business_date "
                "AND l.sales IS NOT NULL AND l.sales > 0 "
                "WHERE p.restaurant_id=? AND p.kind=? AND p.business_date>=? AND p.business_date<=?",
                (rid, kind, _d(start), _d(end))).fetchone()
            asked = conn.execute(
                "SELECT COUNT(*) AS n FROM pos_loss_daily WHERE restaurant_id=? AND kind=? "
                "AND business_date>=? AND business_date<=?", (rid, kind, _d(start), _d(end))).fetchone()
        finally:
            conn.close()
        # No synced loss rows at all is unknown, not zero: a POS that does
        # not report comps looks identical to a restaurant with none.
        if not asked or not (asked["n"] or 0):
            return None, f"no {kind} data synced in this window"
        if not row or not (row["days"] or 0) or not _f(row["s"]):
            return None, f"no sales on the days {kind}s were synced to measure against"
        return round(_f(row["amt"]) / _f(row["s"]) * 100, 2), \
            f"{int(row['n'])} {kind}s over {int(row['days'])} days of sales"
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
_NOISE_FLOOR = {"overtime_hours": 2.0, "response_hours": 2.0,
                # $10 a week: a relative band on a near-zero waste baseline
                # called a cent's move "worse" (re-audit A9).
                "weekly_waste": 10.0}

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
# The broader reading of a family first (re-audit A7): labor % already holds
# the overtime premium in its labor dollars, food cost % holds the waste,
# sales holds every weekday, the average rating the complaints. Where two
# readings of one family overlap, the broader one is the family's money —
# netting an overtime loss against a labor % win subtracted the premium a
# second time.
BREADTH = {"overtime_hours": 1, "weekly_waste": 1, "weekday_sales": 1, "complaints": 1}
# Measured per trading day (labor_daily_history): a day with no row is a day
# not measured. Every other metric is read over its window as a whole.
PER_DAY_METRICS = {"labor_pct", "sales", "weekday_sales"}
# Metrics a season moves: their baseline is matched by weekday and, where a
# year of history allows, adjusted by what the same weeks did last year
# (outcomes.record, audit #30).
SEASONAL_METRICS = {"labor_pct", "food_cost_pct", "sales"}
# Metrics whose dollars are a share of sales, so a month of them is a month
# of TRADING days, not 30.33 calendar days (re-audit A1): a restaurant
# closed Mondays trades 26 days a month, and pricing it on 30.33 overstated
# every labor, food-cost, comp and sales result by a sixth.
SALES_PRICED = {"labor_pct", "food_cost_pct", "comp_rate", "void_rate", "sales"}
# Metrics whose reading depends on which weekdays a window holds (a Friday
# is not a Tuesday): their tracker windows are whole weeks (re-audit A21).
WEEKDAY_MIX_METRICS = {"labor_pct", "food_cost_pct", "comp_rate", "void_rate", "sales", "weekly_waste"}
# Metrics whose window coverage can be counted in trading days, and so
# carry a coverage floor in outcome tracking (re-audit A2).
COVERAGE_METRICS = {"labor_pct", "sales", "weekday_sales", "comp_rate", "void_rate"}
# How far back the trading weekdays are read from when a window's coverage
# is judged: eight weeks, so a closed day is told apart from a missed sync.
TRADING_REFERENCE_DAYS = 56

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


def normalize(key):
    """One spelling per metric key (re-audit A17). "weekday_sales:tuesday",
    "weekday_sales: Tuesday" and "weekday_sales:Tuesday" are one number, and
    the one-tracker-per-number rule compared the raw strings — three
    trackers on Tuesdays' sales at once. Complaint categories fold the same
    way reviews store them ("food quality" -> "food_quality")."""
    base, param = parse(str(key or "").strip())
    base = base.strip()
    if param is None:
        return base
    param = param.strip()
    if base == "weekday_sales":
        param = param.capitalize()
    elif base == "complaints":
        param = _category_id(param)
    return f"{base}:{param}" if param else base


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


def fixed_band(key, before):
    """The metric's stated noise band at `before` (_REGISTRY's `noise`, a
    fraction of the baseline for a relative metric, never below its
    _NOISE_FLOOR) — the FLOOR every estimated band is held above."""
    info = describe(key)
    threshold = info["noise"] * abs(before or 0) if info["relative_noise"] else info["noise"]
    return max(threshold, info["noise_floor"])


def compare(key, before, after, band_scale=1.0, band=None):
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

    `band` (optional) is THIS restaurant's own band for the comparison, in
    the metric's unit (noise_band()["band"]). The stated band is its floor:
    the threshold is max(stated, own) — a restaurant whose weeks swing more
    than the stated band gets a wider one, never a narrower one (CA2 #3).
    """
    if before is None or after is None:
        return {"verdict": "unknown", "delta": None, "delta_pct": None, "band": None, "multiple": None}
    delta = round(after - before, 2)
    delta_pct = round(delta / before * 100, 1) if before else None
    threshold = fixed_band(key, before)
    try:
        own = float(band) if band is not None else 0.0
    except (TypeError, ValueError):
        own = 0.0
    threshold = max(threshold, own) * float(band_scale or 1.0)
    info = describe(key)
    # A move of nothing is never a move: with a relative band on a zero
    # baseline the band is zero too, and "$0 -> $0" read as "worsened"
    # (re-audit A9).
    if delta == 0 or abs(delta) < threshold:
        verdict = "no_clear_change"
    else:
        better = (delta < 0) if info["lower_is_better"] else (delta > 0)
        verdict = "improved" if better else "worsened"
    multiple = round(abs(delta) / threshold, 2) if threshold else None
    return {"verdict": verdict, "delta": delta, "delta_pct": delta_pct,
            "band": round(threshold, 4), "multiple": multiple}


# ── this restaurant's own noise band (CA2 #3, CA1 O10) ──────────────────────
# The stated bands above are the same for every restaurant, so what a
# verdict's false-alarm rate is depends on how much a restaurant's weeks
# swing: with no real change, 8% of steady restaurants' 28-day labor windows
# read "moved" and 66% of volatile ones' did (CA2 probe L). The band below is
# estimated from the restaurant's OWN history: the spread (sample standard
# deviation) of the metric over its non-overlapping historical windows of
# the comparison's length, times BAND_K, widened for the comparison of two
# noisy readings, and never below the stated band.
#
#   band = max(stated, BAND_K × sigma_L × sqrt(1 + L / B))
#
# L is the window compared, B the baseline's length (a baseline of the same
# length doubles the variance of the difference). BAND_K = 1.645 is the
# two-sided 10% point of the normal: with no real change a reading lands
# outside the band about one time in ten, and the floor only makes that
# rarer. `false_alarm_rate` states the rate the band actually gives.
BAND_K = 1.645
BAND_HISTORY_DAYS = 364        # a year of the restaurant's own windows, at most
MIN_BAND_WINDOWS = 4           # same-length windows before their spread is used
MIN_BAND_WEEKS = 6             # weekly windows before a scaled weekly spread is used
# Weekly scaling (the "weekly, scaled" method, day 42 to day 111 of history):
# weeks are NOT independent — a restaurant's level carries from one week to
# the next — so scaling a weekly spread by sqrt(7 / L) understated a 28-day
# window's spread and the band was crossed by 28.7% of do-nothing trackers
# while stating 10% (re-audit B2 #6, probe p2 S3). The weekly series' lag-1
# autocorrelation (bias-corrected, held to [AUTOCORR_FLOOR, AUTOCORR_MAX])
# corrects both the weekly spread (a persistent series' sample variance runs
# low) and its scaling to L days (the AR(1) variance of a mean).
# AUTOCORR_FLOOR is the least persistence assumed: a dozen weeks cannot
# measure a slow level (a level carrying 0.85 week to week under day-to-day
# noise reads a weekly lag-1 correlation near 0.45, and at 0 the corrected
# band was still crossed 24% of the time; at 0.5, 12.8% against a stated
# 10% — tests/test_confidence_round2_q.py replays it).
AUTOCORR_MAX = 0.95
AUTOCORR_FLOOR = 0.5


def _phi(z):
    import math
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


# ── Student's t, pure Python (no SciPy on the platform) ─────────────────────
# For a figure estimated from a handful of values — a slope's significance
# (waste_trend.trend_strength), a prediction range from eight nights
# (demand.forecast_day) — the normal's quantiles overstate what a few values
# can show; these read the t distribution at the right degrees of freedom.

def _betacf(a, b, x):
    """Continued fraction for the regularized incomplete beta (Lentz)."""
    tiny = 1e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    d = 1.0 / (d if abs(d) > tiny else tiny)
    h = d
    for m in range(1, 300):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c
        c = c if abs(c) > tiny else tiny
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c
        c = c if abs(c) > tiny else tiny
        de = d * c
        h *= de
        if abs(de - 1.0) < 1e-12:
            break
    return h


def _betainc(a, b, x):
    """The regularized incomplete beta I_x(a, b). Pure."""
    import math
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    bt = math.exp(math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log(1.0 - x))
    if x < (a + 1.0) / (a + b + 2.0):
        return bt * _betacf(a, b, x) / a
    return 1.0 - bt * _betacf(b, a, 1.0 - x) / b


def t_cdf(t, df):
    """P(T <= t) for Student's t with `df` (> 0, may be fractional) degrees
    of freedom; the normal when df is None. Pure."""
    if df is None:
        return _phi(t)
    df = float(df)
    t = float(t)
    tail = 0.5 * _betainc(df / 2.0, 0.5, df / (df + t * t))
    return 1.0 - tail if t >= 0 else tail


def t_ppf(q, df):
    """The q-quantile of Student's t (bisection on t_cdf); the normal's when
    df is None. Pure."""
    lo, hi = -1000.0, 1000.0
    for _ in range(100):
        mid = (lo + hi) / 2.0
        if t_cdf(mid, df) < q:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def false_alarm_rate(threshold, sigma_diff):
    """The two-sided chance that a move of nothing crosses `threshold` when
    the difference being read has standard deviation `sigma_diff` — or None
    when the spread is unknown (a stated band has no stated rate)."""
    if not sigma_diff or threshold is None:
        return None
    return round(2.0 * (1.0 - _phi(float(threshold) / float(sigma_diff))), 3)


def lag1_autocorrelation(vals):
    """The lag-1 autocorrelation of a series, bias-corrected for its length
    (Kendall's + (1 + 3r) / n) and held to [0, AUTOCORR_MAX] — 0.0 below 3
    values. Pure."""
    n = len(vals)
    if n < 3:
        return 0.0
    m = sum(vals) / n
    den = sum((v - m) ** 2 for v in vals)
    if not den:
        return 0.0
    r = sum((vals[i] - m) * (vals[i - 1] - m) for i in range(1, n)) / den
    r = r + (1.0 + 3.0 * r) / n
    return max(0.0, min(AUTOCORR_MAX, r))


def ar1_mean_variance_factor(rho, m):
    """Var(mean of m consecutive values) / (Var(one value) / m) for an AR(1)
    series with lag-1 correlation rho: 1 + 2 Σ_{k<m} (1 − k/m) ρ^k. Pure."""
    m = max(1, int(m))
    return 1.0 + 2.0 * sum((1.0 - k / m) * rho ** k for k in range(1, m))


def ar1_sample_variance_bias(rho, n):
    """E[s²] / σ² for n values of an AR(1) series: a persistent series'
    sample variance understates its spread. Pure."""
    n = int(n)
    if n < 2:
        return 1.0
    return 1.0 - (2.0 / (n * (n - 1))) * sum((n - k) * rho ** k for k in range(1, n))


def _sd(vals):
    n = len(vals)
    if n < 2:
        return None
    m = sum(vals) / n
    return (sum((v - m) ** 2 for v in vals) / (n - 1)) ** 0.5


def window_spread(restaurant_id, key, window_days, end, db_path=DB_PATH, history_days=BAND_HISTORY_DAYS):
    """(sigma of the metric over one window of `window_days`, method, n) from
    this restaurant's own history ending on or before `end` — or (None,
    "stated", n) when there is too little (window_spread_detail)."""
    d = window_spread_detail(restaurant_id, key, window_days, end, db_path=db_path, history_days=history_days)
    return d["sigma"], d["method"], d["n"]


def window_spread_detail(restaurant_id, key, window_days, end, db_path=DB_PATH, history_days=BAND_HISTORY_DAYS):
    """{sigma, method, n, rho}: the spread of the metric over one window
    of `window_days`, from this restaurant's own history ending on `end`.

    method "own windows": the spread of MIN_BAND_WINDOWS or more
    non-overlapping windows of the same length. method "weekly, scaled": for
    a per-day metric with too few whole windows, MIN_BAND_WEEKS or more whole
    weeks corrected for their persistence ρ (see the note at AUTOCORR_MAX):
    sigma_L = s_7 / sqrt(E[s²]/σ²) × sqrt(VIF(ρ, L/7) / (L/7)). method
    "stated": too little history (sigma None). The stated band floors every
    estimate."""
    e = date.fromisoformat(_d(end))
    L = max(1, int(window_days))

    def _windows(length):
        out = []
        n = max(0, int(history_days) // length)
        for i in range(n):
            we = e - timedelta(days=i * length)
            ws = we - timedelta(days=length - 1)
            v, _ = measure(restaurant_id, key, ws.isoformat(), we.isoformat(), db_path)
            if v is None:
                continue
            cov = coverage(restaurant_id, key, ws.isoformat(), we.isoformat(), db_path)
            if cov and cov["expected"] and cov["share"] < 0.7:
                continue
            out.append(float(v))
        return out
    vals = _windows(L)
    if len(vals) >= MIN_BAND_WINDOWS:
        return {"sigma": _sd(vals), "method": "own windows", "n": len(vals), "rho": None}
    base, _ = parse(key)
    if base in PER_DAY_METRICS and L > 7:
        weeks = list(reversed(_windows(7)))          # oldest first
        if len(weeks) >= MIN_BAND_WEEKS:
            sd7 = _sd(weeks)
            if sd7 is None:
                return {"sigma": None, "method": "weekly, scaled", "n": len(weeks), "rho": None}
            n = len(weeks)
            rho = max(AUTOCORR_FLOOR, lag1_autocorrelation(weeks))
            m = L / 7.0
            bias = max(0.2, ar1_sample_variance_bias(rho, n))
            sigma = (sd7 / bias ** 0.5) * (ar1_mean_variance_factor(rho, int(round(m))) / m) ** 0.5
            return {"sigma": sigma, "method": "weekly, scaled", "n": n, "rho": round(rho, 3)}
    return {"sigma": None, "method": "stated", "n": len(vals), "rho": None}


def band_from_sigma(sigma, window_days, baseline_days=None):
    """(band, sigma of the difference) for comparing a `window_days`
    reading with a `baseline_days` baseline, given the spread of one
    window: BAND_K × sigma × sqrt(1 + L / B). Pure."""
    L = float(window_days)
    B = float(baseline_days or window_days)
    sd_diff = float(sigma) * (1.0 + L / B) ** 0.5
    return BAND_K * sd_diff, sd_diff


def noise_band(restaurant_id, key, window_days=None, end=None, baseline_days=None, before=None,
               db_path=DB_PATH) -> dict:
    """This restaurant's noise band for comparing a `window_days` reading
    with a `baseline_days` baseline (default the same length), from its own
    history ending on `end` (default today). Returns
      {band, sigma, sigma_diff, k, false_alarm_rate, method, n_windows,
       stated, basis}
    `band` is the estimated band (None when the history is too thin — then
    the stated band is all there is and `false_alarm_rate` is None);
    compare(..., band=...) holds the stated band as its floor. Never raises.

    Callers with a 7-day window (the weekly review) pass window_days=7: the
    band is estimated for THAT length, not the 28-day one (CA1 red flag 21)."""
    info = describe(key)
    L = int(window_days or info["default_window_days"])
    B = int(baseline_days or L)
    e = date.fromisoformat(_d(end)) if end else date.today()
    out = {"band": None, "sigma": None, "sigma_diff": None, "k": BAND_K, "false_alarm_rate": None,
           "method": "stated", "n_windows": 0, "window_days": L, "baseline_days": B,
           "stated": round(fixed_band(key, before), 4) if before is not None else None}
    try:
        sp = window_spread_detail(restaurant_id, key, L, e.isoformat(), db_path=db_path)
    except Exception as ex:
        print(f"[metrics] noise band unavailable for {restaurant_id} {key}: {ex}")
        sp = {"sigma": None, "method": "stated", "n": 0, "rho": None}
    sigma, method, n = sp["sigma"], sp["method"], sp["n"]
    out["method"], out["n_windows"], out["rho"] = method, n, sp.get("rho")
    if sigma is None:
        out["basis"] = (f"the stated band for {info['label'].lower()} — too little history here "
                        f"({n} windows of {L} days) to measure this restaurant's own")
        return out
    band, sd_diff = band_from_sigma(sigma, L, B)
    out.update(band=round(band, 4), sigma=round(sigma, 4), sigma_diff=round(sd_diff, 4))
    threshold = max(band, out["stated"] or 0.0)
    out["false_alarm_rate"] = false_alarm_rate(threshold, sd_diff)
    out["basis"] = (f"this restaurant's own spread over {n} {'weeks' if method == 'weekly, scaled' else 'windows'} "
                    f"of history; a move of nothing crosses it about "
                    f"{round((out['false_alarm_rate'] or 0) * 100)}% of the time")
    return out


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


# ── trading days (re-audit A1, A2) ──────────────────────────────────────────

def sales_days(restaurant_id, start, end, db_path=DB_PATH):
    """ISO dates in [start, end] with sales on them — the days traded."""
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT DISTINCT date FROM labor_daily_history WHERE restaurant_id=? AND date>=? "
                            "AND date<=? AND sales IS NOT NULL AND sales > 0",
                            (restaurant_id, _d(start), _d(end))).fetchall()
    finally:
        conn.close()
    return sorted(str(r["date"])[:10] for r in rows)


def _weekday(iso):
    return date.fromisoformat(iso).weekday()


def trading_weekdays(restaurant_id, start, end, db_path=DB_PATH) -> set:
    """The weekdays (0 = Monday) the restaurant traded on in [start, end]."""
    return {_weekday(d) for d in sales_days(restaurant_id, start, end, db_path)}


def _closures(restaurant_id, db_path):
    """(closed weekday numbers, closed ISO dates) the owner has stated
    (schedule_rules.closures) — empty when none or unreadable."""
    try:
        import models
        import schedule_rules
        rest = models.get_restaurant(restaurant_id, db_path)
        if rest is None:
            return set(), set()
        c = schedule_rules.closures(rest)
        return {_WEEKDAYS.index(d) for d in c["closed_weekdays"] if d in _WEEKDAYS}, set(c["closed_dates"])
    except Exception as ex:
        print(f"[metrics] closures unreadable for {restaurant_id}: {ex}")
        return set(), set()


def _trading_set(restaurant_id, start, end, db_path):
    """The weekdays the restaurant trades on: those with sales in [start,
    end], less any weekday the owner has said it is closed."""
    closed_weekdays, _dates = _closures(restaurant_id, db_path)
    return trading_weekdays(restaurant_id, start, end, db_path) - closed_weekdays


def days_per_month(restaurant_id, key, start=None, end=None, db_path=DB_PATH):
    """How many of a metric's days make a month, stated once.

    A sales-priced metric (SALES_PRICED) is worth its per-trading-day figure
    on each TRADING day: the weekdays the restaurant trades on times
    WEEKS_PER_MONTH — 26 for a restaurant closed Mondays, not 30.33. The
    weekdays are read over the window and the TRADING_REFERENCE_DAYS ending
    with it (default the 28 days ending today), the same reference coverage()
    uses, so four Mondays that failed to sync are a gap, not a closure. One
    weekday's sales recur WEEKS_PER_MONTH times a month. Everything else is
    a calendar figure. None when a sales-priced metric has no trading day."""
    base, _ = parse(key)
    if base == "weekday_sales":
        return WEEKS_PER_MONTH
    if base not in SALES_PRICED:
        return DAYS_PER_MONTH
    e = date.fromisoformat(_d(end)) if end is not None else date.today()
    s = date.fromisoformat(_d(start)) if start is not None else e - timedelta(days=27)
    ref_start = min(s, e - timedelta(days=TRADING_REFERENCE_DAYS - 1))
    n = len(_trading_set(restaurant_id, ref_start, e, db_path))
    return n * WEEKS_PER_MONTH if n else None


def accrual_days(restaurant_id, key, start, end, db_path=DB_PATH):
    """The ISO dates in [start, end] a move on this metric was measured on,
    for cumulative accrual: a per-day metric's days with data; a comp or
    void rate's days the POS was asked about AND that traded; food cost's
    trading days. None for a metric read over calendar days (waste per
    week, overtime per payroll week, ratings, reply time)."""
    base, param = parse(key)
    if base in PER_DAY_METRICS:
        return data_days(restaurant_id, key, start, end, db_path)
    if base in ("comp_rate", "void_rate"):
        conn = get_conn(db_path)
        try:
            rows = conn.execute(
                "SELECT DISTINCT p.business_date AS d FROM pos_loss_daily p JOIN labor_daily_history l "
                "ON l.restaurant_id=p.restaurant_id AND l.date=p.business_date AND l.sales IS NOT NULL "
                "AND l.sales > 0 WHERE p.restaurant_id=? AND p.kind=? AND p.business_date>=? "
                "AND p.business_date<=?", (restaurant_id, base.split("_")[0], _d(start), _d(end))).fetchall()
        finally:
            conn.close()
        return sorted(str(r["d"])[:10] for r in rows)
    if base == "food_cost_pct":
        return sales_days(restaurant_id, start, end, db_path)
    return None


def coverage(restaurant_id, key, start, end, db_path=DB_PATH):
    """{measured, expected, share} for a window of a metric counted in
    trading days (COVERAGE_METRICS), else None.

    `expected` is the window's dates on the weekdays the restaurant trades
    (read from the TRADING_REFERENCE_DAYS ending with the window, so a
    closed Monday is not a missed Monday), less the dates the owner marked
    closed (schedule_rules.closures). One day of data in a 28-day window was
    a verdict, a grade and a monthly figure (re-audit A2); outcome tracking
    reads a window only when enough of it was measured."""
    base, param = parse(key)
    if base not in COVERAGE_METRICS:
        return None
    s, e = date.fromisoformat(_d(start)), date.fromisoformat(_d(end))
    if e < s:
        return None
    ref_start = min(s, e - timedelta(days=TRADING_REFERENCE_DAYS - 1))
    closed_weekdays, closed = _closures(restaurant_id, db_path)
    traded = trading_weekdays(restaurant_id, ref_start, e, db_path) - closed_weekdays
    if base == "weekday_sales":
        day = (param or "").strip().capitalize()
        wanted = {_WEEKDAYS.index(day)} & traded if day in _WEEKDAYS else set()
    else:
        wanted = traded
    expected = 0
    d = s
    while d <= e:
        if d.weekday() in wanted and d.isoformat() not in closed:
            expected += 1
        d += timedelta(days=1)
    days = accrual_days(restaurant_id, key, s.isoformat(), e.isoformat(), db_path) or []
    measured = len([x for x in days if x not in closed])
    share = round(measured / expected, 3) if expected else 0.0
    return {"measured": measured, "expected": expected, "share": share}


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


def monthly_dollars(restaurant_id, key, delta, db_path=DB_PATH, window=None):
    """Rough monthly dollar value of a move, or None when it has no honest
    dollar reading. Always an estimate, and labelled as one by callers.

    `window` is the (start, end) the move was read over; a sales-priced
    move is priced on that window's sales per trading day and its trading
    days (days_per_month), default the 28 days ending today."""
    if delta is None:
        return None
    base, _ = parse(key)
    start, end = (window or (None, None))
    if base in ("labor_pct", "food_cost_pct", "comp_rate", "void_rate"):
        # A point of labor, food cost, comps or voids is worth a point of
        # monthly sales: sales per trading day x trading days a month. Comps
        # and voids are already a share of the same sales denominator, so
        # they convert identically.
        if window:
            s = measure(restaurant_id, "sales", _d(start), _d(end), db_path)[0]
        else:
            s = trailing(restaurant_id, "sales", days=28, db_path=db_path)["value"]
        per = days_per_month(restaurant_id, key, start, end, db_path)
        if s is None or per is None:
            return None
        info = describe(key)
        sign = -1 if info["lower_is_better"] else 1
        return round(sign * delta / 100 * s * per, 2)
    if base == "sales":
        # Sales per trading day, over the trading days of a month.
        per = days_per_month(restaurant_id, key, start, end, db_path)
        if per is None:
            return None
        return round(delta * per, 2)
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

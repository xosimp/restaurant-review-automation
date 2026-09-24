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

import models as _models_mod
from models import DB_PATH


def get_conn(db_path=None):
    """models.get_conn, resolved at call time (CLAUDE.md, bound imports): a
    test that redirects models.get_conn reaches every read here too."""
    return _models_mod.get_conn(db_path) if db_path is not None else _models_mod.get_conn()

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


# The stated range around a forecast is an 80% PREDICTION range for the next
# night from the same weekday's own history, and only once there are
# RANGE_MIN_SAMPLES of them. It used to be the min and max of as few as three
# nights (CA2 #6), then the 10th-90th percentile of the eight on file — which
# describes those eight nights, not the next one: it covered 61-64% of next
# nights, not 80% (confidence re-audit B2 #13, probe p7). Now: on the log
# scale (sales are multiplicative), mean ± t(RANGE_COVERAGE, n − 1) × sd ×
# sqrt(1 + 1/n) — the textbook prediction interval, whose t quantile and
# sqrt(1 + 1/n) pay for estimating the spread from eight nights. Replayed:
# 80% on steady nights, 79% with a drifting weekly level, 76% with 1.5%/week
# growth (tests/test_confidence_round2_q.py). demand_accuracy measures the
# coverage it actually gets (inside_range_pct). Below the floor the range is
# withheld and says why.
RANGE_LOW_PCTL = 10            # the stated coverage's two tails (80% between them)
RANGE_HIGH_PCTL = 90
RANGE_COVERAGE = (RANGE_HIGH_PCTL - RANGE_LOW_PCTL) / 100.0
RANGE_MIN_SAMPLES = 8


def _percentile(vals, q):
    """Linear-interpolated percentile (q in 0-100) of a non-empty list."""
    s = sorted(vals)
    if len(s) == 1:
        return s[0]
    pos = (len(s) - 1) * q / 100.0
    lo = int(pos)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def prediction_range(hist):
    """(low, high): the RANGE_COVERAGE prediction range for the next value
    of a positive series from its own history (see the note at
    RANGE_MIN_SAMPLES), or None below RANGE_MIN_SAMPLES values. Pure."""
    import math
    import metrics
    vals = [float(v) for v in hist or [] if v is not None and float(v) > 0]
    n = len(vals)
    if n < RANGE_MIN_SAMPLES:
        return None
    logs = [math.log(v) for v in vals]
    m = sum(logs) / n
    sd = (sum((x - m) ** 2 for x in logs) / (n - 1)) ** 0.5
    half = metrics.t_ppf(1.0 - (1.0 - RANGE_COVERAGE) / 2.0, n - 1) * sd * (1.0 + 1.0 / n) ** 0.5
    return math.exp(m - half), math.exp(m + half)


def forecast_day(restaurant_id, day=None, db_path=DB_PATH):
    """Typical sales for `day` (default today), from its own weekday history.

    `low`/`high` are the 80% prediction range for the night (prediction_range)
    once there are RANGE_MIN_SAMPLES past nights; None before that, with
    `range_note` saying so."""
    day = day or date.today()
    weekday = day.strftime("%A")
    hist = _weekday_history(restaurant_id, weekday, day, db_path=db_path)
    if len(hist) < MIN_SAMPLES:
        return {"available": False, "day": day.isoformat(), "weekday": weekday,
                "reason": f"only {len(hist)} past {weekday}s with sales on file"}
    out = {"available": True, "day": day.isoformat(), "weekday": weekday,
           "typical_sales": round(_median(hist), 2), "samples": len(hist),
           "claim_kind": "forecast", "low": None, "high": None, "range_note": None}
    rng = prediction_range(hist) if len(hist) >= RANGE_MIN_SAMPLES else None
    if rng is not None:
        out.update({"low": round(rng[0], 2), "high": round(rng[1], 2),
                    "range_coverage_pct": int(round(RANGE_COVERAGE * 100)),
                    "range_basis": (f"where 8 in 10 nights should land, from the spread of the last "
                                    f"{len(hist)} {weekday}s")})
    else:
        out["range_note"] = (f"range not yet measurable — {len(hist)} past {weekday}s, "
                             f"needs {RANGE_MIN_SAMPLES}")
    return out


# ── how good the forecast has been ──────────────────────────────────────────

# Nights of nightly-report history read, and the fewest scored nights before
# an accuracy figure is stated.
ACCURACY_WINDOW_DAYS = 56
ACCURACY_MIN_NIGHTS = 7


def demand_accuracy(restaurant_id, today=None, days=ACCURACY_WINDOW_DAYS, db_path=DB_PATH) -> dict:
    """How far forecast_day has been off, out of sample (contract K8).

    Every nightly report stores the night's own forecast next to what the
    night did — dsr_metrics `sales.forecast_net` and `sales.vs_forecast_pct`
    (dsr/block_sales.py), the forecast taken strictly from the nights BEFORE
    it — and nothing ever aggregated them, so staffing advice rested on a
    demand forecast whose error nobody had measured (CA2 #6).

    {"available", "n_nights", "mean_error_pct" (mean |actual vs forecast|),
     "bias_pct" (mean signed: + means nights ran ABOVE the forecast),
     "inside_range_pct" (share of nights inside the stated 80% range, over
     the nights that had one), "n_ranged", "window_days", "reason",
     "skill_vs_last_pct", "skill_vs_mean_pct", "skill_pct", "n_skill"}.
     Nothing below ACCURACY_MIN_NIGHTS scored nights.

    The skill figures (confidence re-audit B2 #11) compare the forecast's
    mean absolute error with two naive forecasts for the same nights — the
    same weekday a week before, and the mean of the NAIVE_WEEKS same
    weekdays before it (labor_daily_history) — as 1 − MAE(forecast) /
    MAE(naive), whole percent, over the nights that have each; `skill_pct`
    is the lower (the forecast must beat both), None below
    ACCURACY_MIN_NIGHTS such nights. The evidence input for a
    recommendation built on the demand forecast."""
    today = today or date.today()
    start = (today - timedelta(days=days)).isoformat()
    end = (today - timedelta(days=1)).isoformat()
    base = {"available": False, "n_nights": 0, "mean_error_pct": None, "bias_pct": None,
            "inside_range_pct": None, "n_ranged": 0, "window_days": days, "claim_kind": "measured",
            "skill_vs_last_pct": None, "skill_vs_mean_pct": None, "skill_pct": None, "n_skill": 0}
    conn = get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT business_date, metric, value FROM dsr_metrics WHERE restaurant_id=? "
            "AND business_date BETWEEN ? AND ? AND metric IN "
            "('sales.vs_forecast_pct','sales.net','sales.forecast_low','sales.forecast_high',"
            "'sales.forecast_net') "
            "AND value IS NOT NULL", (restaurant_id, start, end)).fetchall()
    except Exception:
        rows = []
    finally:
        conn.close()
    nights = {}
    for r in rows:
        nights.setdefault(r["business_date"], {})[r["metric"]] = float(r["value"])
    pcts = [n["sales.vs_forecast_pct"] for n in nights.values() if "sales.vs_forecast_pct" in n]
    ranged = [n for n in nights.values()
              if all(k in n for k in ("sales.net", "sales.forecast_low", "sales.forecast_high"))]
    base["n_nights"] = len(pcts)
    if len(pcts) < ACCURACY_MIN_NIGHTS:
        base["reason"] = (f"only {len(pcts)} nights scored against their forecast in the last {days} days "
                          f"— needs {ACCURACY_MIN_NIGHTS}")
        return base
    inside = [n for n in ranged if n["sales.forecast_low"] <= n["sales.net"] <= n["sales.forecast_high"]]
    base.update({
        "available": True,
        "mean_error_pct": round(sum(abs(p) for p in pcts) / len(pcts), 1),
        "bias_pct": round(sum(pcts) / len(pcts), 1),
        "n_ranged": len(ranged),
        "inside_range_pct": round(len(inside) / len(ranged) * 100) if ranged else None,
        "basis": (f"{len(pcts)} nights in the last {days} days, each forecast from the nights before it"),
    })
    try:
        base.update(_forecast_skill(restaurant_id, nights, db_path=db_path))
    except Exception as e:
        print(f"[demand] forecast skill unavailable for {restaurant_id}: {e}")
    return base


NAIVE_WEEKS = 8


def _forecast_skill(restaurant_id, nights, db_path=DB_PATH) -> dict:
    """The skill figures of demand_accuracy (see its docstring) over
    {business_date: {metric: value}}."""
    dated = []
    for d, n in nights.items():
        a = n.get("sales.net")
        f = n.get("sales.forecast_net")
        if f is None and a is not None and n.get("sales.vs_forecast_pct") is not None:
            f = a / (1.0 + n["sales.vs_forecast_pct"] / 100.0) if n["sales.vs_forecast_pct"] > -100 else None
        if a and a > 0 and f is not None:
            try:
                dated.append((date.fromisoformat(str(d)[:10]), float(a), float(f)))
            except ValueError:
                continue
    out = {"skill_vs_last_pct": None, "skill_vs_mean_pct": None, "skill_pct": None, "n_skill": 0}
    if not dated:
        return out
    first = min(x[0] for x in dated) - timedelta(weeks=NAIVE_WEEKS)
    last = max(x[0] for x in dated)
    conn = get_conn(db_path)
    try:
        hist = {str(r["date"])[:10]: float(r["sales"]) for r in conn.execute(
            "SELECT date, sales FROM labor_daily_history WHERE restaurant_id=? AND date>=? AND date<? "
            "AND sales IS NOT NULL AND sales > 0", (restaurant_id, first.isoformat(), last.isoformat())).fetchall()}
    finally:
        conn.close()
    pairs_last, pairs_mean = [], []
    for d, a, f in dated:
        prev = [hist.get((d - timedelta(weeks=k)).isoformat()) for k in range(1, NAIVE_WEEKS + 1)]
        if prev[0] is not None:
            pairs_last.append((abs(f - a) / a, abs(prev[0] - a) / a))
        got = [v for v in prev if v is not None]
        if len(got) >= 3:
            pairs_mean.append((abs(f - a) / a, abs(sum(got) / len(got) - a) / a))
    for key, pairs in (("skill_vs_last_pct", pairs_last), ("skill_vs_mean_pct", pairs_mean)):
        out["n_skill"] = max(out["n_skill"], len(pairs))
        if len(pairs) < ACCURACY_MIN_NIGHTS:
            continue
        mae_f = sum(p[0] for p in pairs) / len(pairs)
        mae_n = sum(p[1] for p in pairs) / len(pairs)
        if mae_n > 0:
            out[key] = int(round(100.0 * (1.0 - mae_f / mae_n)))
    got = [v for v in (out["skill_vs_last_pct"], out["skill_vs_mean_pct"]) if v is not None]
    out["skill_pct"] = min(got) if got else None
    return out


def week_projection(restaurant_id, week_dates, db_path=DB_PATH) -> dict:
    """The week's sales, projected day by day from forecast_day — the
    figure a published schedule is built against. {"total", "days":
    [dates it covers], "by_day", "missing": [dates with no forecast]}."""
    by_day, missing = {}, []
    for d in week_dates or []:
        try:
            day = d if isinstance(d, date) else date.fromisoformat(str(d)[:10])
        except ValueError:
            continue
        fc = forecast_day(restaurant_id, day, db_path=db_path)
        if fc.get("available"):
            by_day[day.isoformat()] = fc["typical_sales"]
        else:
            missing.append(day.isoformat())
    return {"total": round(sum(by_day.values()), 2) if by_day else None,
            "days": sorted(by_day), "by_day": by_day, "missing": missing}


def freeze_week_projection(restaurant_id, week_start, db_path=DB_PATH) -> dict:
    """Freeze the week's sales projection when its schedule is published, so
    it can be scored once the week closes (forecast_log kind revenue_week,
    insert-once: republishing the same week never moves it). Only the days
    that had a forecast are projected, and only those are summed back when
    it is scored. Never raises."""
    try:
        import forecast_log
        start = week_start if isinstance(week_start, date) else date.fromisoformat(str(week_start)[:10])
        dates = [start + timedelta(days=i) for i in range(7)]
        proj = week_projection(restaurant_id, dates, db_path=db_path)
        if proj["total"] is None:
            return {"recorded": False, "reason": "no weekday has enough history to project"}
        return forecast_log.record(
            restaurant_id, "revenue_week", proj["total"], period_of=start,
            basis={"days": proj["days"], "method": "forecast_day median per weekday, frozen at publish"},
            db_path=db_path)
    except Exception as e:
        print(f"[demand] week projection not frozen rid={restaurant_id}: {e}")
        return {"recorded": False, "reason": str(e)}


def week_projection_accuracy(restaurant_id, db_path=DB_PATH) -> dict:
    """The frozen weekly projections' record (forecast_log.accuracy)."""
    import forecast_log
    return forecast_log.accuracy(restaurant_id, "revenue_week", db_path=db_path)


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


# "Reliably" slow (the word Ask and the quiet-night push use) needs more than
# a low median: at least this share of that weekday's own nights must sit
# under the restaurant's typical day. A weekday whose median is low because
# of two dead nights among six ordinary ones is not reliably anything.
RELIABLY_SLOW_SHARE = 0.75


def slow_days(restaurant_id, db_path=DB_PATH):
    """Weekdays that run materially — and consistently — below this
    restaurant's typical day. Each carries `consistency`: the share of its
    own nights under the typical day."""
    import labor
    fc = labor.build_demand_forecast(restaurant_id, weeks=LOOKBACK_WEEKS, db_path=db_path)
    if not fc.get("ok"):
        return {"available": False, "reason": fc.get("reason", "not enough history")}
    overall = fc.get("overall_median") or fc.get("typical_day")
    slow = []
    for d in fc.get("days", []):
        if not (d["vs_average_pct"] <= -SLOW_DAY_PCT and d["samples"] >= MIN_SAMPLES):
            continue
        consistency = None
        try:
            hist = _weekday_history(restaurant_id, d["day"], date.today() + timedelta(days=1),
                                    db_path=db_path)
            typical = float(overall) if overall else (d.get("median_sales") or 0) / (1 + d["vs_average_pct"] / 100.0)
            if hist and typical:
                consistency = round(sum(1 for v in hist if v < typical) / len(hist), 2)
        except Exception:
            consistency = None
        if consistency is None or consistency < RELIABLY_SLOW_SHARE:
            continue
        slow.append(dict(d, consistency=consistency))
    return {"available": True, "slow_days": slow, "all_days": fc.get("days", []),
            "threshold_pct": SLOW_DAY_PCT, "consistency_floor": RELIABLY_SLOW_SHARE}


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


# ── the holidays ahead, said from this restaurant's own history ─────────────

HOLIDAY_LOOKAHEAD_DAYS = 21
HOLIDAY_GENERIC_LABEL = "Holiday — check your own history"


def upcoming_holidays(restaurant_id, now=None, days=HOLIDAY_LOOKAHEAD_DAYS, db_path=DB_PATH) -> list:
    """The holidays in the next `days`, for the Labor tab's banner on web and
    iOS (one list for both).

    The banner used to say "Expect elevated covers… your busiest Sundays
    follow this pattern" for every holiday on a generic calendar, whether or
    not this restaurant had ever been busy on it (CA1 L30). Each entry now
    carries what THIS restaurant's own sales showed on that holiday last
    year (schedule_economics.holiday_lift — its sales against the same
    weekday either side), or the generic label and nothing else:
    [{"name", "date", "date_str" (M/D/YY), "days_away", "lift_pct",
      "based_on", "label", "claim_kind"}]."""
    import re
    from datetime import datetime
    from time_utils import mdy
    now = now or datetime.now()
    out = []
    try:
        from marketing import get_upcoming_holidays
        hol_str = get_upcoming_holidays(now) or ""
    except Exception:
        hol_str = ""
    for chunk in hol_str.split(", "):
        m = re.search(r'\((\w+ \d+)\)$', chunk)
        if not m:
            continue
        try:
            hdate = datetime.strptime(m.group(1) + " " + str(now.year), "%b %d %Y")
        except ValueError:
            continue
        if hdate.date() < now.date():
            hdate = hdate.replace(year=now.year + 1)
        away = (hdate.date() - now.date()).days
        if not 0 <= away <= days:
            continue
        iso = hdate.date().isoformat()
        entry = {"name": chunk[:chunk.rfind("(")].strip(), "date": iso, "date_str": mdy(iso),
                 "days_away": away, "lift_pct": None, "based_on": None,
                 "label": HOLIDAY_GENERIC_LABEL, "claim_kind": None}
        try:
            import schedule_economics
            own = (schedule_economics.holiday_lift(restaurant_id, [iso], db_path=db_path) or {}).get(iso) or {}
            if own.get("lift_pct") is not None and own.get("based_on"):
                lift = int(own["lift_pct"])
                entry.update({
                    "lift_pct": lift, "based_on": own["based_on"], "claim_kind": "measured",
                    "label": (f"Last year {abs(lift)}% {'above' if lift >= 0 else 'below'} a typical "
                              f"{hdate.strftime('%A')} here — {own['based_on']}")})
        except Exception:
            pass
        out.append(entry)
    return out

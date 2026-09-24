"""forecast_log.py — every forecast the product states, frozen once and scored
after the period it predicts has closed.

The `forecast_log` table (models.init_db) existed for the waste and prime-cost
forecasts, and the confidence audit (9/24/26, CA2 findings 5 and 6) found
three ways its record overstated itself:

  * the waste forecast was keyed on `today + 7`, so every day the Food Cost
    insight was opened wrote another row — eight daily rows across two ISO
    weeks read as "8 scored weeks" (probe F);
  * a forecast was scored as soon as `horizon_end <= today`, so a week still
    in progress could be scored against a partial actual;
  * the two error figures used different denominators (|error| over the
    prediction, signed error over the actual), so the reading and the bias
    correction disagreed about the same forecasts.

This module is the one place those rules live, for every kind:

  record()        insert-once per period (an ISO week, Monday-Sunday, or a
                  calendar month). The first prediction for a period is the
                  forecast; a later, better-informed one never replaces it.
  score_due()     fills `actual` only for periods that have CLOSED (the
                  period's last day is before today), through one resolver
                  per kind, and never scores against a missing measurement.
  accuracy()      the record for one kind: one row per period, the last
                  ACCURACY_WINDOW of them, mean absolute error and signed
                  bias over the SAME denominator (the actual), a reading, and
                  `withheld` when the record reads "often wide" — a forecast
                  with that record is not shown.
  calibration() / calibrated()
                  the bias correction, from the same rows.

Kinds (KINDS): waste_week and profitability_month (Food Cost), revenue_week
(the week's sales projection, frozen when its schedule is published),
labor_week, marketing_reach_week and review_rating_week (written by the
labor, marketing and review insights). Each kind states its unit and what
its actual is; a caller predicting in any other unit is predicting something
the resolver cannot score.

Deterministic, no model calls. Never raises out of accuracy()/calibration();
record() raises ValueError on an unknown kind so a typo is a failing test,
not a silent row nobody scores.
"""
import json
from datetime import date, timedelta

import models as _models_mod
from models import DB_PATH


def get_conn(db_path=None):
    """models.get_conn, resolved at call time (CLAUDE.md, bound imports)."""
    return _models_mod.get_conn(db_path) if db_path is not None else _models_mod.get_conn()


# kind -> period ("week" = ISO Monday-Sunday, "month" = calendar month), the
# unit of `predicted`, and what the actual is.
KINDS = {
    "waste_week": {"period": "week", "unit": "$",
                   "actual": "the ISO week's waste dollars (waste_trend's weekly series)"},
    "profitability_month": {"period": "month", "unit": "%",
                            "actual": "food cost % + labor % over the closed month (metrics.measure)"},
    "revenue_week": {"period": "week", "unit": "$",
                     "actual": "net sales summed over the days the projection covered (labor_daily_history); "
                               "unscored unless every one of those days has sales on file"},
    "labor_week": {"period": "week", "unit": "%",
                   "actual": "labor % of sales over the ISO week (metrics.measure labor_pct)"},
    "marketing_reach_week": {"period": "week", "unit": "reach",
                             "actual": "summed reach of posts published in the ISO week (marketing_content_log)"},
    "review_rating_week": {"period": "week", "unit": "★",
                           "actual": "average star rating of reviews posted in the ISO week "
                                     "(metrics.measure avg_rating — unscored under its 5-review floor)"},
}

# How many closed periods the record reads, and how many it needs before it
# says anything. Two is the floor the Food Cost accuracy line always used.
ACCURACY_WINDOW = 8
MIN_SCORED_FOR_READING = 2
# Mean absolute error bands for the reading. "often wide" withholds the next
# forecast of that kind: a module that has been 40% out four times running
# does not get to state a fifth figure as if it had not.
READING_CLOSE_PCT = 15.0
READING_ROUGH_PCT = 30.0
WITHHOLD_READING = "often wide"
# The bias correction: at least three scored periods leaning the same way by
# at least this much on average.
CALIBRATION_MIN_SCORED = 3
CALIBRATION_MIN_BIAS_PCT = 10.0


def _f(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _day(d) -> date:
    if isinstance(d, date):
        return d
    return date.fromisoformat(str(d)[:10])


def week_bounds(d):
    """(Monday, Sunday) of the ISO week `d` falls in."""
    d = _day(d)
    start = d - timedelta(days=d.weekday())
    return start, start + timedelta(days=6)


def month_bounds(d):
    d = _day(d)
    first = d.replace(day=1)
    nxt = (first + timedelta(days=32)).replace(day=1)
    return first, nxt - timedelta(days=1)


def period_bounds(kind: str, d):
    """(first, last) day of the period of `kind` that `d` falls in."""
    return month_bounds(d) if KINDS[kind]["period"] == "month" else week_bounds(d)


def period_end(kind: str, d) -> str:
    """The ISO date a forecast of `kind` for the period containing `d` is keyed
    on — the period's last day, so every prediction for one period shares one
    key and insert-once holds."""
    return period_bounds(kind, d)[1].isoformat()


def is_closed(kind: str, horizon_end, today=None) -> bool:
    """True once the whole period containing `horizon_end` is in the past.
    Read through the period, not the stored date: legacy waste rows were
    keyed on arbitrary weekdays, and a Wednesday horizon's week is still
    open on Thursday."""
    today = _day(today or date.today())
    try:
        return period_bounds(kind, horizon_end)[1] < today
    except Exception:
        return False


# ── writing ─────────────────────────────────────────────────────────────────

def record(restaurant_id: int, kind: str, predicted, period_of=None, basis=None,
           db_path: str = DB_PATH) -> dict:
    """Freeze a forecast for the period containing `period_of` (default:
    next week for a week kind, this month for a month kind). Insert-once:
    a second prediction for the same period is ignored and says so.

    `basis` is free text, or a dict (stored as JSON) — revenue_week keeps
    the dates it covered there so the actual sums the same days."""
    if kind not in KINDS:
        raise ValueError(f"unknown forecast kind {kind!r} — add it to forecast_log.KINDS")
    p = _f(predicted)
    if p is None:
        return {"recorded": False, "reason": "no prediction"}
    if period_of is None:
        period_of = date.today() + (timedelta(days=7) if KINDS[kind]["period"] == "week" else timedelta(0))
    horizon = period_end(kind, period_of)
    if isinstance(basis, (dict, list)):
        basis = json.dumps(basis)
    conn = get_conn(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO forecast_log (restaurant_id, kind, horizon_end, predicted, basis) VALUES (?,?,?,?,?) "
            "ON CONFLICT(restaurant_id, kind, horizon_end) DO NOTHING",
            (restaurant_id, kind, horizon, round(p, 2), basis))
        conn.commit()
        written = (cur.rowcount or 0) > 0
    finally:
        conn.close()
    if not written:
        return {"recorded": False, "horizon_end": horizon, "reason": "already frozen for this period"}
    return {"recorded": True, "horizon_end": horizon, "predicted": round(p, 2)}


def frozen(restaurant_id: int, kind: str, period_of, db_path: str = DB_PATH):
    """The frozen row for the period containing `period_of`, or None."""
    horizon = period_end(kind, period_of)
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT * FROM forecast_log WHERE restaurant_id=? AND kind=? AND horizon_end=?",
                           (restaurant_id, kind, horizon)).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


# ── the actuals, one resolver per kind ──────────────────────────────────────

def _waste_actual(restaurant_id, start, end, row, db_path):
    from waste_trend import load_waste_history
    weeks, _t = load_waste_history(restaurant_id, None, db_path=db_path,
                                   since=start.isoformat(), until=end.isoformat())
    for w in weeks:
        try:
            if week_bounds(w["week_end"])[1] == end:
                return _f(w.get("waste"), 0.0)
        except Exception:
            continue
    return None


def _prime_actual(restaurant_id, start, end, row, db_path):
    import metrics as _m
    fc, _ = _m.measure(restaurant_id, "food_cost_pct", start.isoformat(), end.isoformat(), db_path)
    lb, _ = _m.measure(restaurant_id, "labor_pct", start.isoformat(), end.isoformat(), db_path)
    return (float(fc) + float(lb)) if fc is not None and lb is not None else None


def _labor_actual(restaurant_id, start, end, row, db_path):
    import metrics as _m
    v, _ = _m.measure(restaurant_id, "labor_pct", start.isoformat(), end.isoformat(), db_path)
    return _f(v)


def _rating_actual(restaurant_id, start, end, row, db_path):
    import metrics as _m
    v, _ = _m.measure(restaurant_id, "avg_rating", start.isoformat(), end.isoformat(), db_path)
    return _f(v)


def _reach_actual(restaurant_id, start, end, row, db_path):
    conn = get_conn(db_path)
    try:
        r = conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(COALESCE(reach,0)),0) AS reach FROM marketing_content_log "
            "WHERE restaurant_id=? AND post_id IS NOT NULL AND TRIM(post_id) != '' "
            "AND date(created_at) BETWEEN ? AND ?", (restaurant_id, start.isoformat(), end.isoformat())).fetchone()
    finally:
        conn.close()
    return _f(r["reach"]) if r is not None else None


def _revenue_actual(restaurant_id, start, end, row, db_path):
    """Net sales over exactly the days the projection covered. A day in that
    list with no sales on file leaves the week unscored — a sum missing a
    night is not the week's sales."""
    days = None
    try:
        b = json.loads(row.get("basis") or "")
        days = b.get("days") if isinstance(b, dict) else None
    except Exception:
        days = None
    if not days:
        days = [(start + timedelta(days=i)).isoformat() for i in range(7)]
    conn = get_conn(db_path)
    try:
        got = {r["date"]: _f(r["sales"]) for r in conn.execute(
            "SELECT date, sales FROM labor_daily_history WHERE restaurant_id=? AND date BETWEEN ? AND ? "
            "AND sales IS NOT NULL AND sales > 0", (restaurant_id, start.isoformat(), end.isoformat())).fetchall()}
    finally:
        conn.close()
    if any(d not in got for d in days):
        return None
    return round(sum(got[d] for d in days), 2)


_RESOLVERS = {
    "waste_week": _waste_actual,
    "profitability_month": _prime_actual,
    "revenue_week": _revenue_actual,
    "labor_week": _labor_actual,
    "marketing_reach_week": _reach_actual,
    "review_rating_week": _rating_actual,
}


def register_resolver(kind: str, fn, period: str = "week", unit: str = None, actual: str = None):
    """Add or replace a kind's resolver: fn(restaurant_id, start, end, row,
    db_path) -> float | None. None means "not measurable" and the row stays
    unscored."""
    KINDS.setdefault(kind, {"period": period, "unit": unit or "", "actual": actual or ""})
    _RESOLVERS[kind] = fn


def actual_for(restaurant_id: int, kind: str, horizon_end, row=None, db_path: str = DB_PATH):
    start, end = period_bounds(kind, horizon_end)
    fn = _RESOLVERS.get(kind)
    if fn is None:
        return None
    try:
        return fn(restaurant_id, start, end, row or {}, db_path)
    except Exception:
        return None


# ── scoring ─────────────────────────────────────────────────────────────────

def errors(predicted, actual):
    """(abs_error_pct, signed_error_pct), both over the ACTUAL — one
    denominator, so the reading and the bias describe the same thing.
    (None, None) when the actual is zero: a percentage of nothing is not an
    error figure."""
    p, a = _f(predicted), _f(actual)
    if p is None or a is None or a <= 0:
        return None, None
    signed = round((p - a) / a * 100, 1)
    return abs(signed), signed


def score_due(restaurant_id: int, today=None, db_path: str = DB_PATH) -> dict:
    """Fill in the actual for every forecast whose period has closed.
    Returns {"scored": n, "unmeasurable": k}."""
    today = _day(today or date.today())
    conn = get_conn(db_path)
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, kind, horizon_end, predicted, basis FROM forecast_log "
            "WHERE restaurant_id=? AND actual IS NULL AND horizon_end < ?",
            (restaurant_id, today.isoformat())).fetchall()]
    finally:
        conn.close()
    scored = unmeasurable = 0
    updates = []
    for r in rows:
        if r["kind"] not in KINDS or not is_closed(r["kind"], r["horizon_end"], today):
            continue
        actual = actual_for(restaurant_id, r["kind"], r["horizon_end"], row=r, db_path=db_path)
        if actual is None:
            unmeasurable += 1
            continue
        err, signed = errors(r["predicted"], actual)
        updates.append((round(actual, 2), err, signed, r["id"]))
    if updates:
        conn = get_conn(db_path)
        try:
            for u in updates:
                conn.execute("UPDATE forecast_log SET actual=?, error_pct=?, signed_error_pct=?, "
                             "scored_at=datetime('now') WHERE id=?", u)
                scored += 1
            conn.commit()
        finally:
            conn.close()
    return {"scored": scored, "unmeasurable": unmeasurable}


def restaurants_due(today=None, db_path: str = DB_PATH) -> list:
    """Restaurant ids holding at least one unscored forecast dated before
    today — the set the nightly scoring pass walks, whatever modules they
    have on (a labor or review forecast is scored with no Food Cost)."""
    today = _day(today or date.today())
    conn = get_conn(db_path)
    try:
        return [r[0] for r in conn.execute(
            "SELECT DISTINCT restaurant_id FROM forecast_log WHERE actual IS NULL AND horizon_end < ? "
            "ORDER BY restaurant_id", (today.isoformat(),)).fetchall()]
    finally:
        conn.close()


# ── reading the record ──────────────────────────────────────────────────────

def _scored_periods(restaurant_id, kind, db_path, limit=ACCURACY_WINDOW):
    """One scored row per period, newest period first. Legacy rows written
    once a day for the same week collapse to the FIRST prediction made for
    that week — the one that was a forecast rather than a later reading."""
    conn = get_conn(db_path)
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT horizon_end, predicted, actual, error_pct, signed_error_pct, created_at FROM forecast_log "
            "WHERE restaurant_id=? AND kind=? AND actual IS NOT NULL ORDER BY created_at ASC, id ASC",
            (restaurant_id, kind)).fetchall()]
    except Exception:
        rows = []
    finally:
        conn.close()
    by_period = {}
    for r in rows:
        try:
            key = period_bounds(kind, r["horizon_end"])[1]
        except Exception:
            continue
        by_period.setdefault(key, r)
    ordered = [by_period[k] for k in sorted(by_period, reverse=True)]
    return ordered[:limit]


def _period_word(kind, n):
    w = "month" if KINDS.get(kind, {}).get("period") == "month" else "week"
    return w if n == 1 else w + "s"


def accuracy(restaurant_id: int, kind: str, db_path: str = DB_PATH) -> dict:
    """How this kind of forecast has held up here (contract K8).

    {"available", "kind", "scored", "n_periods", "n_weeks" | "n_months",
     "mean_error_pct", "bias_pct", "reading", "withheld", "reason"}.
    Never raises."""
    base = {"available": False, "kind": kind, "scored": 0, "n_periods": 0,
            "mean_error_pct": None, "bias_pct": None, "reading": None, "withheld": False}
    unit_key = "n_months" if KINDS.get(kind, {}).get("period") == "month" else "n_weeks"
    base[unit_key] = 0
    try:
        rows = _scored_periods(restaurant_id, kind, db_path)
    except Exception:
        rows = []
    errs = [_f(r["error_pct"]) for r in rows if _f(r["error_pct"]) is not None]
    signed = [_f(r["signed_error_pct"]) for r in rows if _f(r["signed_error_pct"]) is not None]
    base.update({"scored": len(errs), "n_periods": len(errs), unit_key: len(errs)})
    if len(errs) < MIN_SCORED_FOR_READING:
        base["reason"] = (f"not enough scored forecasts yet to say how accurate these are — "
                          f"{len(errs)} closed {_period_word(kind, len(errs))} scored, needs {MIN_SCORED_FOR_READING}")
        return base
    mean_err = sum(errs) / len(errs)
    reading = ("close" if mean_err <= READING_CLOSE_PCT else
               "roughly right" if mean_err <= READING_ROUGH_PCT else WITHHOLD_READING)
    base.update({
        "available": True,
        "mean_error_pct": round(mean_err, 1),
        "bias_pct": round(sum(signed) / len(signed), 1) if signed else None,
        "reading": reading,
        "withheld": reading == WITHHOLD_READING,
        "reason": (f"past forecasts here missed by {mean_err:.0f}% on average over {len(errs)} "
                   f"{_period_word(kind, len(errs))}, so the next one is not shown"
                   if reading == WITHHOLD_READING else None),
    })
    return base


def calibration(restaurant_id: int, kind: str, db_path: str = DB_PATH) -> dict:
    """The bias correction: with CALIBRATION_MIN_SCORED+ scored periods
    leaning one way by CALIBRATION_MIN_BIAS_PCT+, the factor that would have
    made them land. Absent until the record exists. Never raises."""
    try:
        rows = _scored_periods(restaurant_id, kind, db_path)
    except Exception:
        rows = []
    errs = [_f(r["signed_error_pct"]) for r in rows if _f(r["signed_error_pct"]) is not None]
    if len(errs) < CALIBRATION_MIN_SCORED:
        return {"available": False, "scored": len(errs)}
    bias = sum(errs) / len(errs)
    if abs(bias) < CALIBRATION_MIN_BIAS_PCT:
        return {"available": True, "scored": len(errs), "bias_pct": round(bias, 1), "factor": 1.0,
                "reading": "no consistent lean"}
    factor = round(1.0 / (1.0 + bias / 100.0), 4)
    return {"available": True, "scored": len(errs), "bias_pct": round(bias, 1), "factor": factor,
            "reading": (f"ran {abs(bias):.0f}% {'high' if bias > 0 else 'low'} across the last "
                        f"{len(errs)} scored {_period_word(kind, len(errs))}")}


def calibrated(restaurant_id: int, kind: str, predicted, db_path: str = DB_PATH):
    """(corrected_value, calibration) — unchanged when there is no record to
    correct it with."""
    cal = calibration(restaurant_id, kind, db_path=db_path)
    if not cal.get("available") or cal.get("factor", 1.0) == 1.0 or predicted is None:
        return predicted, cal
    return round(float(predicted) * cal["factor"], 2), cal


# ── the waste forecast itself ───────────────────────────────────────────────

# Weeks averaged for next week's waste. Probe W (CA2 #5, replayed in
# tests/test_forecast_log.py): on a flat $400 ± $60 series the old
# latest-week-plus-last-change forecast missed by ~$101 a week, last week
# alone by ~$62, and a four-week mean by ~$53 — momentum was worse than
# saying nothing new. A mean of the recent weeks is the forecast.
WASTE_FORECAST_WEEKS = 4
WASTE_FORECAST_MIN_WEEKS = 3


def waste_next_week(values, anomalies=()):
    """Next week's waste from the weekly series (oldest first): the mean of
    the last WASTE_FORECAST_WEEKS weeks, leaving out any week the series'
    own anomaly test flagged (an outlier is not a level). None with fewer
    than WASTE_FORECAST_MIN_WEEKS usable weeks. Pure."""
    vals = [_f(v) for v in (values or [])]
    n = len(vals)
    skip = set(anomalies or ())
    recent = [vals[i] for i in range(max(0, n - WASTE_FORECAST_WEEKS), n)
              if vals[i] is not None and i not in skip]
    if len(recent) < WASTE_FORECAST_MIN_WEEKS:
        return None
    return round(max(0.0, sum(recent) / len(recent)), 2)

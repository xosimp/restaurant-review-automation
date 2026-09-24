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
                  bias over the SAME denominator (the actual), a reading, its
                  SKILL against two naive forecasts (the last closed period,
                  the mean of up to 8 — each as it stood when the forecast
                  was frozen, stored at scoring), and `withheld` when the
                  record reads "often wide" or does worse than either naive
                  forecast — a forecast with that record is not shown. No
                  reading and no withholding before MIN_SCORED_FOR_READING
                  (4) periods are scored (confidence re-audit B2 #11, B1 L2).
  A period still unmeasurable UNSCORABLE_AFTER_DAYS after it closed is
  marked `unscorable_at` and never retried (re-audit B6 #11).
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
    """models.get_conn, resolved at call time (CLAUDE.md, bound imports).
    The functions here default db_path to the import-time DB_PATH, so that
    value means "the default database" and resolves through models at call
    time too — data_freshness's pattern (re-audit B6 low: it was passed on
    explicitly, and a patched models.get_conn never reached it)."""
    if db_path is None or db_path == DB_PATH:
        return _models_mod.get_conn()
    return _models_mod.get_conn(db_path)


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
# says anything — a reading ("close") or a verdict (withheld). Two periods
# said "close" about a record two coin flips could make (re-audit B1 L2).
ACCURACY_WINDOW = 8
MIN_SCORED_FOR_READING = 4
# Skill against the naive forecasts (re-audit B2 #11): 1 − MAE(forecast) /
# MAE(naive) over the scored periods that carry the naive figure, at least
# MIN_SCORED_FOR_SKILL of them. `skill` is the lower of the two (vs the last
# period, vs the mean of up to NAIVE_MEAN_PERIODS); below 0 the forecast has
# done worse than saying nothing new, and the next one is withheld.
MIN_SCORED_FOR_SKILL = 4
NAIVE_MEAN_PERIODS = 8
NAIVE_MEAN_MIN = 3             # closed periods before the naive mean exists
NO_SKILL_READING = "no better than a simple average"
# A closed period whose actual still cannot be measured this many days after
# it closed is given up on (`unscorable_at`), not retried every night.
UNSCORABLE_AFTER_DAYS = 21
# Scored rows whose naive figures are filled in per pass (older rows).
NAIVE_BACKFILL_PER_PASS = 50
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


def _shift_months(d, k):
    """The first of the month k months before d's month."""
    y, m = d.year, d.month - k
    while m <= 0:
        m += 12
        y -= 1
    return date(y, m, 1)


def _prior_periods(kind, known_on, n=NAIVE_MEAN_PERIODS):
    """[(first, last)] of the n periods of `kind` that had CLOSED before
    `known_on` (the day the forecast was frozen), newest first."""
    known_on = _day(known_on)
    first_open = period_bounds(kind, known_on)[0]
    out = []
    for k in range(1, n + 1):
        if KINDS[kind]["period"] == "month":
            out.append(month_bounds(_shift_months(first_open, k)))
        else:
            out.append(week_bounds(first_open - timedelta(days=7 * k)))
    # The period holding known_on closes only after it: never known then.
    return [p for p in out if p[1] < known_on]


def naive_forecasts(restaurant_id, kind, row, db_path=DB_PATH) -> dict:
    """{"last", "mean", "n"}: what two naive forecasts would have said for
    the period of `row` when it was frozen — the latest closed period's
    actual, and the mean of up to NAIVE_MEAN_PERIODS closed periods (None
    below NAIVE_MEAN_MIN of them). Read through the kind's own resolver; a
    revenue week sums the same weekdays it projected, shifted back whole
    weeks. Never raises."""
    out = {"last": None, "mean": None, "n": 0}
    try:
        known_on = _day(str(row.get("created_at") or row.get("horizon_end"))[:10])
        target_first = period_bounds(kind, row["horizon_end"])[0]
        try:
            basis = json.loads(row.get("basis") or "")
        except Exception:
            basis = None
        vals = []
        for first, last in _prior_periods(kind, known_on):
            prow = dict(row)
            if kind == "revenue_week" and isinstance(basis, dict) and basis.get("days"):
                shift = target_first - first
                prow["basis"] = json.dumps({"days": [(_day(d) - shift).isoformat() for d in basis["days"]]})
            v = actual_for(restaurant_id, kind, last, row=prow, db_path=db_path)
            if v is not None:
                vals.append(float(v))
        out["n"] = len(vals)
        if vals:
            out["last"] = round(vals[0], 2)
        if len(vals) >= NAIVE_MEAN_MIN:
            out["mean"] = round(sum(vals) / len(vals), 2)
    except Exception as e:
        print(f"[forecast_log] naive forecasts unavailable for {restaurant_id}/{kind}: {e}")
    return out


def score_due(restaurant_id: int, today=None, db_path: str = DB_PATH) -> dict:
    """Fill in the actual for every forecast whose period has closed, with
    the two naive forecasts it is judged against (naive_forecasts). A row
    still unmeasurable UNSCORABLE_AFTER_DAYS after its period closed is
    marked `unscorable_at` and never read again (re-audit B6 #11). Scored
    rows from before the naive figures existed get them, a bounded number
    per pass. Returns {"scored": n, "unmeasurable": k, "gave_up": g}."""
    today = _day(today or date.today())
    conn = get_conn(db_path)
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, kind, horizon_end, predicted, basis, created_at FROM forecast_log "
            "WHERE restaurant_id=? AND actual IS NULL AND unscorable_at IS NULL AND horizon_end < ?",
            (restaurant_id, today.isoformat())).fetchall()]
        backfill = [dict(r) for r in conn.execute(
            "SELECT id, kind, horizon_end, predicted, basis, created_at FROM forecast_log "
            "WHERE restaurant_id=? AND actual IS NOT NULL AND naive_n IS NULL "
            "ORDER BY horizon_end DESC LIMIT ?", (restaurant_id, NAIVE_BACKFILL_PER_PASS)).fetchall()]
    finally:
        conn.close()
    scored = unmeasurable = gave_up = 0
    updates, give_up, naive = [], [], []
    for r in rows:
        if r["kind"] not in KINDS or not is_closed(r["kind"], r["horizon_end"], today):
            continue
        actual = actual_for(restaurant_id, r["kind"], r["horizon_end"], row=r, db_path=db_path)
        if actual is None:
            unmeasurable += 1
            closed_on = period_bounds(r["kind"], r["horizon_end"])[1]
            if (today - closed_on).days > UNSCORABLE_AFTER_DAYS:
                give_up.append((today.isoformat(), r["id"]))
            continue
        err, signed = errors(r["predicted"], actual)
        nv = naive_forecasts(restaurant_id, r["kind"], r, db_path=db_path)
        updates.append((round(actual, 2), err, signed, nv["last"], nv["mean"], nv["n"], r["id"]))
    for r in backfill:
        if r["kind"] not in KINDS:
            continue
        nv = naive_forecasts(restaurant_id, r["kind"], r, db_path=db_path)
        naive.append((nv["last"], nv["mean"], nv["n"], r["id"]))
    if updates or give_up or naive:
        conn = get_conn(db_path)
        try:
            for u in updates:
                conn.execute("UPDATE forecast_log SET actual=?, error_pct=?, signed_error_pct=?, naive_last=?, "
                             "naive_mean=?, naive_n=?, scored_at=datetime('now') WHERE id=?", u)
                scored += 1
            for g in give_up:
                conn.execute("UPDATE forecast_log SET unscorable_at=? WHERE id=? AND actual IS NULL", g)
                gave_up += 1
            for n_ in naive:
                conn.execute("UPDATE forecast_log SET naive_last=?, naive_mean=?, naive_n=? WHERE id=?", n_)
            conn.commit()
        finally:
            conn.close()
    return {"scored": scored, "unmeasurable": unmeasurable, "gave_up": gave_up}


def restaurants_due(today=None, db_path: str = DB_PATH) -> list:
    """Restaurant ids holding at least one unscored forecast dated before
    today that has not been given up on (unscorable_at) — the set the
    nightly scoring pass walks, whatever modules they have on (a labor or
    review forecast is scored with no Food Cost)."""
    today = _day(today or date.today())
    conn = get_conn(db_path)
    try:
        return [r[0] for r in conn.execute(
            "SELECT DISTINCT restaurant_id FROM forecast_log WHERE actual IS NULL AND unscorable_at IS NULL "
            "AND horizon_end < ? ORDER BY restaurant_id", (today.isoformat(),)).fetchall()]
    finally:
        conn.close()


# ── reading the record ──────────────────────────────────────────────────────

def _scored_periods(restaurant_id, kind, db_path, limit=ACCURACY_WINDOW):
    """One scored row per period, newest period first. Legacy rows written
    once a day for the same week collapse to the FIRST prediction made for
    that week — the one that was a forecast rather than a later reading."""
    conn = get_conn(db_path)
    try:
        rows = None
        for cols in (", naive_last, naive_mean", ""):
            try:
                rows = [dict(r) for r in conn.execute(
                    "SELECT horizon_end, predicted, actual, error_pct, signed_error_pct, created_at" + cols +
                    " FROM forecast_log WHERE restaurant_id=? AND kind=? AND actual IS NOT NULL "
                    "ORDER BY created_at ASC, id ASC", (restaurant_id, kind)).fetchall()]
                break
            except Exception:
                continue
        rows = rows or []
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


def skill(rows) -> dict:
    """{"skill_vs_last", "skill_vs_mean", "skill", "skill_pct", "n_skill"}
    over scored rows carrying the naive figures: 1 − MAE(forecast) /
    MAE(naive), each over the rows that have that naive figure, with at
    least MIN_SCORED_FOR_SKILL of them; `skill` is the lower of the two
    (the forecast must beat both), `skill_pct` it as a whole percent. Pure."""
    out = {"skill_vs_last": None, "skill_vs_mean": None, "skill": None, "skill_pct": None, "n_skill": 0}
    for name, col in (("skill_vs_last", "naive_last"), ("skill_vs_mean", "naive_mean")):
        pairs = [(_f(r.get("predicted")), _f(r.get("actual")), _f(r.get(col))) for r in rows]
        pairs = [p for p in pairs if None not in p]
        out["n_skill"] = max(out["n_skill"], len(pairs))
        if len(pairs) < MIN_SCORED_FOR_SKILL:
            continue
        mae_f = sum(abs(p - a) for p, a, _n in pairs) / len(pairs)
        mae_n = sum(abs(nv - a) for _p, a, nv in pairs) / len(pairs)
        if mae_n > 0:
            out[name] = round(1.0 - mae_f / mae_n, 3)
        elif mae_f == 0:
            out[name] = 0.0
        else:
            out[name] = -1.0              # the naive forecast was exact and this one was not
    got = [v for v in (out["skill_vs_last"], out["skill_vs_mean"]) if v is not None]
    if got:
        out["skill"] = min(got)
        out["skill_pct"] = int(round(100 * out["skill"]))
    return out


def accuracy(restaurant_id: int, kind: str, db_path: str = DB_PATH) -> dict:
    """How this kind of forecast has held up here (contract K8).

    {"available", "kind", "scored", "n_periods", "n_weeks" | "n_months",
     "mean_error_pct", "bias_pct", "reading", "withheld", "reason",
     "skill_vs_last", "skill_vs_mean", "skill", "skill_pct", "n_skill",
     "beats_naive"}.
    `skill` (see skill()) is the evidence input for a recommendation built on
    this forecast; `beats_naive` is True/False once it is measured, else
    None. Withheld when the record reads "often wide" or its skill is below
    0 (reading NO_SKILL_READING). Nothing below MIN_SCORED_FOR_READING.
    Never raises."""
    base = {"available": False, "kind": kind, "scored": 0, "n_periods": 0,
            "mean_error_pct": None, "bias_pct": None, "reading": None, "withheld": False,
            "skill_vs_last": None, "skill_vs_mean": None, "skill": None, "skill_pct": None, "n_skill": 0,
            "beats_naive": None}
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
    sk = skill(rows)
    base.update(sk)
    reason = None
    if reading == WITHHOLD_READING:
        reason = (f"past forecasts here missed by {mean_err:.0f}% on average over {len(errs)} "
                  f"{_period_word(kind, len(errs))}, so the next one is not shown")
    elif sk["skill"] is not None and sk["skill"] < 0:
        reading = NO_SKILL_READING
        what = ("the last " + _period_word(kind, 1) + "'s figure"
                if sk["skill_vs_last"] is not None and sk["skill_vs_last"] == sk["skill"]
                else f"the average of the {_period_word(kind, 2)} before")
        reason = (f"past forecasts here missed by more than simply repeating {what} did, over "
                  f"{sk['n_skill']} {_period_word(kind, sk['n_skill'])}, so the next one is not shown")
    base.update({
        "available": True,
        "mean_error_pct": round(mean_err, 1),
        "bias_pct": round(sum(signed) / len(signed), 1) if signed else None,
        "reading": reading,
        "withheld": reason is not None,
        "reason": reason,
        "beats_naive": (sk["skill"] > 0) if sk["skill"] is not None else None,
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

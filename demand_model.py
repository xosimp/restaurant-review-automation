"""
demand_model.py — the schedule-grade sales forecast: trend and year-over-year
seasonality on top of the weekday median, chosen per restaurant by a
backtest against that median, with intervals calibrated on its own misses
(scheduler audit 10/10/26 RM-19, LEARNING-19, MARKET-11).

The schedule's demand used to be the 8-week median of the same weekday
(schedule_economics.date_demand's base), moved only by dated facts. At every
season turn that median lags 4-8 weeks — a growing restaurant is staffed for
last month, a patio spring is staffed like March — and a consistent lean the
nightly report measured (demand.forecast_calibration) never reached the
schedule.

Three estimators, each "as of" an origin (only nights before it are read):

  naive     the median of the same weekday over NAIVE_WEEKS weeks — the
            figure every other surface states (demand.forecast_day).
  trend     naive moved by the restaurant's own level trend: the Theil-Sen
            slope of each night's log ratio to its weekday median over
            TREND_WEEKS weeks, carried from the middle of the naive window
            to the date. Bounded (TREND_MAX_WEEKLY, TREND_BOUNDS).
  seasonal  naive times last year's own move from the same window to the
            same week (the same weekday's nights a year back, +/- a week,
            against that year's naive window), averaged over the years on
            file and shrunk toward 1 (one year counts SEASON_ONE_YEAR).
            Needs SEASON_MIN_DAYS of history.

backtest() forecasts past nights from the nights before them (origin = the
night minus a lead), over the last BACKTEST_DAYS nights.

  selection    at HORIZON_DAYS: each estimator's mean absolute error
               against naive's on the same nights; the estimator kept is the
               lowest error IF it beats naive by MIN_SKILL over at least
               BACKTEST_MIN nights, otherwise naive, and it says so (never a
               fancier figure that is worse);
  calibration  CROSS-FITTED (blind re-audit 10/11/26 LEARNING-26 —
               choosing the best of three and calibrating its interval on
               the same nights made the interval narrower than the truth):
               the nights are split into alternate halves (date ordinal even
               and odd, so a season turn reaches both); each half's errors
               are those of the estimator the OTHER half would have chosen,
               so no night's error is read by the choice it was made by,
               each AT THE DATE'S OWN LEAD (the date minus
               today — a weekly draft is made up to ~2 weeks before its last
               night, and a 7-day backtest understated the far nights'
               spread). The interval is those errors' empirical quantiles
               (conformal, log(actual / forecast)) so "80% between low and
               high" is what nights that far out did; the median error is
               the bias correction, bounded to BIAS_MAX_PCT. Below
               BACKTEST_MIN nights no interval is stated.

Weather and events are NOT in here: they are measured per daypart and
applied by schedule_economics.date_demand (demand_drivers), and the ratio
this module hands it is forecast / naive — how far the trend and the season
move a typical night — so nothing is counted twice.

Reads labor_daily_history (final nights). Never calls a model. Never raises
from forecast_dates (an empty dict means "the median stands").
"""
import math
from datetime import date, timedelta

import models as _models_mod
from models import DB_PATH

NAIVE_WEEKS = 8
TREND_WEEKS = 12
TREND_MIN_POINTS = 28
TREND_MAX_WEEKLY = 0.05          # a level moving more than 5% a week is an event, not a trend
TREND_BOUNDS = (0.8, 1.25)
SEASON_MIN_DAYS = 371            # a year and a week of history before last year can say anything
SEASON_BOUNDS = (0.6, 1.6)
SEASON_ONE_YEAR = 0.5            # one year's move counts half; two years' three quarters
SEASON_TWO_YEARS = 0.75
HISTORY_DAYS = 2 * 371 + NAIVE_WEEKS * 7 + 14
BACKTEST_DAYS = 84
BACKTEST_MIN = 14
HORIZON_DAYS = 7                 # the selection's lead; the interval reads each date's own lead
MAX_LEAD_DAYS = 21
MIN_SKILL = 0.03                 # beat the median by 3% of its error, or keep the median
BIAS_MAX_PCT = 15.0
QUANTILES = (0.1, 0.25, 0.5, 0.7, 0.75, 0.9)
BUSY_QUANTILE = 0.7              # what a busy shift is staffed to (schedule_requirements)
ESTIMATORS = ("naive", "trend", "seasonal")


def get_conn(db_path=None):
    """models.get_conn, resolved at call time (CLAUDE.md, bound imports)."""
    if db_path is None or db_path == DB_PATH:
        return _models_mod.get_conn()
    return _models_mod.get_conn(db_path)


def _median(vals):
    s = sorted(vals)
    if not s:
        return None
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def _quantile(vals, q):
    s = sorted(vals)
    if not s:
        return None
    if len(s) == 1:
        return s[0]
    pos = (len(s) - 1) * q
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def daily_sales(restaurant_id, start, end, db_path=DB_PATH) -> dict:
    """{date (date): sales} for the final nights in [start, end) with sales."""
    from canonical_facts import FINAL_SQL
    conn = get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT date, sales FROM labor_daily_history WHERE restaurant_id=? AND sales IS NOT NULL AND sales > 0 "
            f"AND date >= ? AND date < ? AND {FINAL_SQL}",
            (restaurant_id, start.isoformat(), end.isoformat())).fetchall()
    except Exception:
        return {}
    finally:
        conn.close()
    out = {}
    for r in rows:
        try:
            out[date.fromisoformat(str(r["date"])[:10])] = float(r["sales"])
        except (TypeError, ValueError):
            continue
    return out


# ── the estimators (pure over a {date: sales} series) ─────────────────────

def _naive(series, d, origin):
    """(median, [dates]) of d's weekday in the NAIVE_WEEKS before origin."""
    got = [(x, v) for x, v in series.items() if x < origin and (origin - x).days <= NAIVE_WEEKS * 7
           and x.weekday() == d.weekday()]
    if len(got) < 3:
        return None, []
    return _median([v for _x, v in got]), [x for x, _v in got]


def _theil_sen(points):
    """The median pairwise slope of [(x, y)] — robust to the odd event night."""
    slopes = []
    pts = sorted(points)
    n = len(pts)
    # At most ~90 points: n^2/2 pairs is a few thousand.
    for i in range(n):
        xi, yi = pts[i]
        for j in range(i + 1, n):
            xj, yj = pts[j]
            if xj != xi:
                slopes.append((yj - yi) / float(xj - xi))
    return _median(slopes) if slopes else None


def trend_factor(series, d, origin, used_dates):
    """exp(slope x days from the naive window's middle to d), bounded; None
    below TREND_MIN_POINTS nights."""
    start = origin - timedelta(weeks=TREND_WEEKS)
    window = {x: v for x, v in series.items() if start <= x < origin}
    if len(window) < TREND_MIN_POINTS or not used_dates:
        return None
    by_wd = {}
    for x, v in window.items():
        by_wd.setdefault(x.weekday(), []).append(v)
    med = {wd: _median(vs) for wd, vs in by_wd.items() if len(vs) >= 3}
    pts = [((x - origin).days, math.log(v / med[x.weekday()])) for x, v in window.items()
           if x.weekday() in med and med[x.weekday()] > 0]
    if len(pts) < TREND_MIN_POINTS:
        return None
    b = _theil_sen(pts)
    if b is None:
        return None
    b = max(-TREND_MAX_WEEKLY / 7.0, min(TREND_MAX_WEEKLY / 7.0, b))
    mid = sum((x - origin).days for x in used_dates) / float(len(used_dates))
    f = math.exp(b * ((d - origin).days - mid))
    return max(TREND_BOUNDS[0], min(TREND_BOUNDS[1], f))


def season_factor(series, d, origin):
    """Last year's move from the naive window to d's week (see the module
    note), shrunk toward 1; None without a year of history behind it."""
    if not series or (origin - min(series)).days < SEASON_MIN_DAYS:
        return None
    idx = []
    for k in (1, 2):
        ly, lo = d - timedelta(days=364 * k), origin - timedelta(days=364 * k)
        near = [series.get(ly + timedelta(days=o)) for o in (-7, 0, 7)]
        near = [v for v in near if v]
        base, _used = _naive(series, ly, lo)
        if len(near) >= 2 and base:
            idx.append(_median(near) / base)
    if not idx:
        return None
    s = _median(idx)
    s = max(SEASON_BOUNDS[0], min(SEASON_BOUNDS[1], s))
    w = SEASON_ONE_YEAR if len(idx) == 1 else SEASON_TWO_YEARS
    return 1.0 + (s - 1.0) * w


def estimate(series, d, origin, estimator) -> float | None:
    """One estimator's point forecast of d as of origin (pure)."""
    base, used = _naive(series, d, origin)
    if base is None:
        return None
    if estimator == "naive":
        return base
    if estimator == "trend":
        f = trend_factor(series, d, origin, used)
        return base * f if f is not None else None
    if estimator == "seasonal":
        f = season_factor(series, d, origin)
        return base * f if f is not None else None
    return None


def backtest(series, today, days=BACKTEST_DAYS, horizon=HORIZON_DAYS, parity=None, estimators=ESTIMATORS) -> dict:
    """{estimator: {"n", "mae", "log_errors": [log(actual/forecast)]}} over
    the nights in the `days` before today (only those whose date ordinal
    has `parity` 0 or 1, when given), each forecast as of the night minus
    `horizon` days. Pure."""
    out = {e: {"n": 0, "abs": [], "log_errors": []} for e in estimators}
    for d in sorted(x for x in series if today - timedelta(days=days) <= x < today
                    and (parity is None or x.toordinal() % 2 == parity)):
        actual = series[d]
        origin = d - timedelta(days=horizon)
        for e in estimators:
            f = estimate(series, d, origin, e)
            if f and f > 0:
                out[e]["abs"].append((d, abs(f - actual) / actual))
                out[e]["log_errors"].append((d, math.log(actual / f)))
    for e, v in out.items():
        v["n"] = len(v["abs"])
    return out


def choose(bt) -> dict:
    """{"estimator", "skill", "n", "mae", "naive_mae", "reason"} — the
    estimator with the lowest error on the nights naive was also scored on,
    kept only past MIN_SKILL over BACKTEST_MIN nights (skill = 1 - MAE /
    naive MAE, demand._forecast_skill's measure). Pure."""
    naive = dict(bt.get("naive", {}).get("abs") or [])
    best = {"estimator": "naive", "skill": 0.0, "n": len(naive),
            "mae": (sum(naive.values()) / len(naive)) if naive else None,
            "naive_mae": (sum(naive.values()) / len(naive)) if naive else None, "reason": None}
    if len(naive) < BACKTEST_MIN:
        best["reason"] = (f"only {len(naive)} past nights to test against — the weekday median stands "
                          f"(needs {BACKTEST_MIN})")
        return best
    for e in ESTIMATORS:
        if e == "naive":
            continue
        mine = dict(bt.get(e, {}).get("abs") or [])
        common = [d for d in mine if d in naive]
        if len(common) < BACKTEST_MIN:
            continue
        mae = sum(mine[d] for d in common) / len(common)
        nmae = sum(naive[d] for d in common) / len(common)
        if nmae <= 0:
            continue
        skill = 1.0 - mae / nmae
        if skill >= MIN_SKILL and skill > best["skill"]:
            best = {"estimator": e, "skill": round(skill, 3), "n": len(common), "mae": round(mae, 4),
                    "naive_mae": round(nmae, 4), "reason": None}
    if best["estimator"] == "naive" and best["reason"] is None:
        best["reason"] = "neither the trend nor last year's season beat the weekday median on the past nights"
    return best


def interval(log_errors) -> dict | None:
    """{q: multiplier} from the backtested log errors (conformal), the
    median bounded to BIAS_MAX_PCT; None below BACKTEST_MIN errors."""
    errs = [e for _d, e in log_errors or []]
    if len(errs) < BACKTEST_MIN:
        return None
    out = {}
    cap = math.log(1.0 + BIAS_MAX_PCT / 100.0)
    for q in QUANTILES:
        v = _quantile(errs, q)
        if q == 0.5:
            v = max(-cap, min(cap, v))
        out[q] = math.exp(v)
    return out


def forecast_from_series(series, dates, today) -> dict:
    """forecast_dates over an in-memory series (pure; the tests' entry): the
    estimator chosen on the older backtest nights, each date's interval
    from the newer nights at that date's own lead (module doc)."""
    bt = backtest(series, today)
    pick = choose(bt)
    est = pick["estimator"]
    # Cross-fitting: each half's errors come from the estimator the other
    # half chose (module doc).
    other = {p: choose(backtest(series, today, parity=1 - p))["estimator"] for p in (0, 1)}
    by_lead = {}

    def mult_for(lead):
        if lead not in by_lead:
            errs = []
            for p in (0, 1):
                cal = backtest(series, today, horizon=lead, parity=p, estimators=(other[p],))
                errs += cal.get(other[p], {}).get("log_errors") or []
            by_lead[lead] = interval(errs)
        return by_lead[lead]
    out = {}
    for d in dates:
        naive, _used = _naive(series, d, today)
        if naive is None:
            continue
        lead = max(1, min(MAX_LEAD_DAYS, (d - today).days))
        mult = mult_for(lead)
        point = estimate(series, d, today, est) or naive
        e = {"naive": round(naive, 2), "point": round(point, 2), "estimator": est, "skill": pick["skill"],
             "backtest_n": pick["n"], "reason": pick["reason"], "lead_days": lead}
        if mult:
            e["quantiles"] = {q: round(point * m, 2) for q, m in mult.items()}
            e["p50"] = e["quantiles"][0.5]
        else:
            e["quantiles"] = None
            e["p50"] = round(point, 2)
        e["ratio"] = round(e["p50"] / naive, 4) if naive else 1.0
        busy = (e["quantiles"] or {}).get(BUSY_QUANTILE)
        e["busy_ratio"] = round(busy / naive, 4) if busy and naive else e["ratio"]
        out[d.isoformat()] = e
    return {"dates": out, "choice": pick}


def forecast_dates(restaurant_id, dates, db_path=DB_PATH, today=None) -> dict:
    """{"dates": {iso: {"naive", "point", "p50", "quantiles": {q: sales} |
    None, "ratio" (p50 / naive), "busy_ratio" (the BUSY_QUANTILE / naive),
    "estimator", "skill", "backtest_n", "reason"}}, "choice": choose()} —
    {} on any failure (the weekday median then stands). `today` is the
    restaurant's own date (demand.local_today)."""
    try:
        if today is None:
            import demand
            today = demand.local_today(restaurant_id)
        days = []
        for d in dates or []:
            try:
                days.append(d if isinstance(d, date) else date.fromisoformat(str(d)[:10]))
            except ValueError:
                continue
        if not days:
            return {}
        series = daily_sales(restaurant_id, today - timedelta(days=HISTORY_DAYS), today, db_path=db_path)
        if not series:
            return {}
        return forecast_from_series(series, days, today)
    except Exception as e:
        print(f"[demand_model] forecast unavailable for {restaurant_id}: {e}")
        return {}


def basis_words(entry) -> str:
    """The owner-facing words for what moved a date off its typical night."""
    if not entry:
        return ""
    est = entry.get("estimator")
    pct = int(round((float(entry.get("ratio") or 1.0) - 1.0) * 100))
    if est == "trend":
        what = "this restaurant's recent trend"
    elif est == "seasonal":
        what = "how this week ran against the weeks before it last year"
    else:
        what = "the forecast's own record of its misses"
    skill = entry.get("skill")
    tested = (f" (tested on {entry.get('backtest_n')} past nights: {int(round(float(skill) * 100))}% less error "
              "than the weekday median)") if skill else ""
    return f"{what} {pct:+d}%{tested}"

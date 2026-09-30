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
# Final nights only (canonical_facts): a night the POS had not closed when it
# was read is provisional, never a data point in a median (memory audit
# 9/29/26, QUALITY-10).
from canonical_facts import FINAL_SQL


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


def local_today(restaurant_id):
    """The restaurant's own calendar date (time_utils.restaurant_now_by_id)
    — the day a forecast is for and the edge of every window here. The
    server's date.today() is UTC on the host: after 7pm Central "today"
    was tomorrow (DH1-14)."""
    try:
        from time_utils import restaurant_now_by_id
        return restaurant_now_by_id(restaurant_id).date()
    except Exception:
        return date.today()


def _weekday_history_dated(restaurant_id, weekday, before, weeks=LOOKBACK_WEEKS, db_path=DB_PATH):
    """[(ISO date, sales)] on `weekday` in the `weeks` before `before`
    (exclusive), oldest first."""
    start = (before - timedelta(weeks=weeks)).isoformat()
    conn = get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT date, sales FROM labor_daily_history WHERE restaurant_id=? AND day_of_week=? "
            f"AND date>=? AND date<? AND sales IS NOT NULL AND sales > 0 AND {FINAL_SQL} ORDER BY date",
            (restaurant_id, weekday, start, before.isoformat())).fetchall()
    finally:
        conn.close()
    return [(str(r["date"])[:10], float(r["sales"])) for r in rows]


def _weekday_history(restaurant_id, weekday, before, weeks=LOOKBACK_WEEKS, db_path=DB_PATH):
    """Sales on `weekday` in the `weeks` before `before` (exclusive), so a day
    is never compared with a baseline that contains itself."""
    return [v for _d, v in _weekday_history_dated(restaurant_id, weekday, before, weeks, db_path=db_path)]


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


# The newest night a day forecast rests on may be at most this many days
# before the day it is for. After the POS stops, "a typical Saturday" could
# come from three Saturdays six to eight weeks back and read like this
# week's expectation (DH1-18); past this it is withheld and says why.
STALE_SAMPLE_DAYS = 14


def forecast_day(restaurant_id, day=None, db_path=DB_PATH, effects=True, calibrate=True):
    """Typical sales for `day` (default the restaurant's today), from its own
    weekday history — the POS sync's final daily totals.

    `low`/`high` are the 80% prediction range for the night (prediction_range)
    once there are RANGE_MIN_SAMPLES past nights; None before that, with
    `range_note` saying so. `newest_sample` is the newest night it rests on,
    named in `range_basis` / `range_note`; a forecast whose newest night is
    more than STALE_SAMPLE_DAYS before `day` is withheld (DH1-18).

    `effects` (default): the measured effects of what is known about the date
    — a listed event, the holiday, a campaign aimed at it, the 1st or 15th —
    from this restaurant's own nights (event_memory.effects_for_day, behind
    its sample floor), applied to the weekday median and said: `base_sales`
    is the plain median, `effects` what moved it, `effect_basis` the
    sentence. False reads the plain weekday median ("a typical Tuesday").

    `calibrate` (default, with `effects`): corrected by this restaurant's own
    measured misses (forecast_calibration — a consistent lean over enough
    scored nights, bounded; memory re-audit 9/29/26, LOOPS-13): `raw_sales`
    is the figure before it, the one the nightly report records and the
    correction is measured against, so it never feeds on itself;
    `calibration` / `calibration_note` say it."""
    day = day or local_today(restaurant_id)
    weekday = day.strftime("%A")
    dated = _weekday_history_dated(restaurant_id, weekday, day, db_path=db_path)
    out = _forecast_from(dated, day, weekday)
    if effects and out.get("available"):
        _apply_effects(restaurant_id, day, out, db_path)
        if calibrate:
            _apply_calibration(restaurant_id, day, out, db_path)
    return out


def forecast_net(restaurant_id, day=None, db_path=DB_PATH, effects=True, calibrate=True):
    """forecast_day on the nightly report's OWN basis (canonical_facts
    .BASIS_DSR) — the only forecast a report's net may be set beside and the
    one its predictions are graded against (memory audit 9/29/26, net_basis).

    Where the POS's daily total is built as the report's net (RPOWER), or its
    basis is unknown, that is forecast_day itself. For any other POS (Toast's
    businessDay netSales counts service charges and refunds its own way) the
    counting gap was read as "N% below a typical Tuesday" and learned as
    forecast bias: here the forecast rests on the report's own past nights
    (and an imported DSR workbook), and until MIN_SAMPLES of the weekday
    exist it is withheld and says why. `basis` is always BASIS_DSR."""
    import canonical_facts as cf
    day = day or local_today(restaurant_id)
    weekday = day.strftime("%A")
    if cf.pos_basis(cf._current_provider(restaurant_id)) != cf.BASIS_POS:
        fc = (forecast_day(restaurant_id, day, db_path=db_path) if effects and calibrate
              else forecast_day(restaurant_id, day, db_path=db_path, calibrate=False) if effects
              else forecast_day(restaurant_id, day, db_path=db_path, effects=False))
        return dict(fc, basis=cf.BASIS_DSR) if isinstance(fc, dict) else fc
    start = day - timedelta(weeks=LOOKBACK_WEEKS)
    series = cf.net_series(restaurant_id, start, day - timedelta(days=1), db_path=db_path, pos=cf.POS_SAME_BASIS)
    dated = [(d, x["net"]) for d, x in series.items()
             if date.fromisoformat(d).strftime("%A") == weekday and x["net"] and x["net"] > 0]
    out = _forecast_from(dated, day, weekday, noun="nightly reports")
    out["basis"] = cf.BASIS_DSR
    if not out.get("available") and not out.get("stale"):
        out["reason"] = (f"only {len(dated)} past {weekday} nightly report{'s' if len(dated) != 1 else ''} on file "
                         f"(needs {MIN_SAMPLES}) — the POS's own daily total counts some things differently, so "
                         "tonight's net is not set beside a forecast built from it")
    if effects and out.get("available"):
        _apply_effects(restaurant_id, day, out, db_path)
        if calibrate:
            _apply_calibration(restaurant_id, day, out, db_path)
    return out


def _forecast_from(dated, day, weekday, noun="with sales on file"):
    """The forecast dict from [(ISO date, sales)] of `weekday`, oldest first
    (forecast_day's rules: MIN_SAMPLES, the stale newest night, the 80%
    prediction range from RANGE_MIN_SAMPLES)."""
    from time_utils import mdy as _mdy
    hist = [v for _d, v in dated]
    if len(hist) < MIN_SAMPLES:
        return {"available": False, "day": day.isoformat(), "weekday": weekday,
                "reason": f"only {len(hist)} past {weekday}s {noun}"}
    newest = dated[-1][0]
    try:
        newest_age = (day - date.fromisoformat(newest)).days
    except ValueError:
        newest_age = None
    if newest_age is None or newest_age > STALE_SAMPLE_DAYS:
        return {"available": False, "day": day.isoformat(), "weekday": weekday, "newest_sample": newest,
                "stale": True,
                "reason": (f"the newest {weekday} with sales on file is {_mdy(newest)}, more than "
                           f"{STALE_SAMPLE_DAYS} days before {_mdy(day.isoformat())} — sales have not "
                           "synced recently, so a typical night can't be stated")}
    out = {"available": True, "day": day.isoformat(), "weekday": weekday,
           "typical_sales": round(_median(hist), 2), "samples": len(hist),
           "newest_sample": newest,
           "claim_kind": "forecast", "low": None, "high": None, "range_note": None}
    rng = prediction_range(hist) if len(hist) >= RANGE_MIN_SAMPLES else None
    if rng is not None:
        out.update({"low": round(rng[0], 2), "high": round(rng[1], 2),
                    "range_coverage_pct": int(round(RANGE_COVERAGE * 100)),
                    "range_basis": (f"where 8 in 10 nights should land, from the spread of the last "
                                    f"{len(hist)} {weekday}s, the newest {_mdy(newest)}")})
    else:
        out["range_basis"] = f"the last {len(hist)} {weekday}s, the newest {_mdy(newest)}"
        out["range_note"] = (f"range not yet measurable — {len(hist)} past {weekday}s, "
                             f"needs {RANGE_MIN_SAMPLES}")
    return out


def _apply_effects(restaurant_id, day, out, db_path=DB_PATH):
    """Scale an available forecast by the measured effects known before the
    night (event_memory.effects_for_day). The range moves with it. Never
    raises: a forecast without its effects is still the weekday median."""
    try:
        import event_memory
        eff = event_memory.effects_for_day(restaurant_id, day, db_path=db_path)
    except Exception as e:
        print(f"[demand] measured effects unavailable for {restaurant_id}: {e}")
        eff = None
    if not eff or not eff.get("pct"):
        return out
    factor = 1.0 + float(eff["pct"]) / 100.0
    out["base_sales"] = out["typical_sales"]
    out["typical_sales"] = round(out["typical_sales"] * factor, 2)
    for k in ("low", "high"):
        if out.get(k) is not None:
            out[k] = round(out[k] * factor, 2)
    out["effect_pct"] = eff["pct"]
    out["effects"] = eff["applied"]
    # Labels whose lift the applied ones already carry (measured on the same
    # nights — event_memory QUALITY-4): said, never multiplied in.
    if eff.get("subsumed"):
        out["effects_subsumed"] = eff["subsumed"]
    out["effect_basis"] = eff["basis"]
    return out


# ── the forecast corrected by its own misses (LOOPS-13) ─────────────────────
#
# demand_accuracy measured the lean ("nights ran 12% above the forecast") and
# only displayed it: a restaurant running 12% above for two months was
# forecast low every night — the prep list, the DSR's range, the brief. The
# rule is forecast_log's (a consistent lean over enough scored periods,
# bounded, the raw figure kept for scoring), over nights: the nightly report
# records each night's RAW forecast (sales.forecast_raw_net), and the lean is
# measured against that, never against a corrected figure.

CALIBRATION_MIN_NIGHTS = 14          # scored same-basis nights before any correction
CALIBRATION_MIN_LEAN_PCT = 5.0       # a lean smaller than this is day-to-day noise
CALIBRATION_MIN_SHARE = 2 / 3.0      # of the nights on the lean's side: "consistent"
CALIBRATION_MAX_PCT = 15.0           # the correction is bounded either way
CALIBRATION_WINDOW_DAYS = 56         # the nights read: demand_accuracy's own window


def _scored_nights(restaurant_id, start, end, db_path=DB_PATH):
    """({business_date: {metric: value}}, excluded_other_basis) — the nightly
    reports' forecast metrics in [start, end], same basis only (memory audit
    9/29/26, net_basis): a night scored against a forecast built from a POS
    total counted another way learned the counting gap as forecast bias. A
    night records whether its forecast rested on the report's own basis
    (sales.forecast_same_basis); a night from before that record counts
    unless its POS is known to build a different figure."""
    conn = get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT business_date, metric, value, source FROM dsr_metrics WHERE restaurant_id=? "
            "AND business_date BETWEEN ? AND ? AND metric IN "
            "('sales.vs_forecast_pct','sales.net','sales.forecast_low','sales.forecast_high',"
            "'sales.forecast_net','sales.forecast_raw_net','sales.forecast_same_basis') "
            "AND value IS NOT NULL", (restaurant_id, start, end)).fetchall()
    except Exception:
        rows = []
    finally:
        conn.close()
    nights, sources = {}, {}
    for r in rows:
        nights.setdefault(r["business_date"], {})[r["metric"]] = float(r["value"])
        if r["metric"] == "sales.net":
            sources[r["business_date"]] = r["source"]
    import canonical_facts as _cf
    other_basis = [d for d, n in nights.items()
                   if not (n.get("sales.forecast_same_basis") == 1.0
                           or ("sales.forecast_same_basis" not in n
                               and _cf.pos_basis(sources.get(d)) != _cf.BASIS_POS))]
    for d in other_basis:
        nights.pop(d, None)
    return nights, len(other_basis)


def forecast_calibration(restaurant_id, before=None, days=CALIBRATION_WINDOW_DAYS, db_path=DB_PATH) -> dict:
    """{"available", "factor", "lean_pct", "n", "share", "reading", "reason"}
    — how the RAW day forecast has missed over the `days` before `before`
    (default the restaurant's today): actual ÷ raw forecast − 1 per scored
    same-basis night (a night recorded before the raw figure existed stored
    its raw forecast as sales.forecast_net — nothing corrected it then).
    A correction (`factor` ≠ 1) needs CALIBRATION_MIN_NIGHTS nights, a mean
    lean of CALIBRATION_MIN_LEAN_PCT or more, and CALIBRATION_MIN_SHARE of
    the nights on that side; it is bounded to ±CALIBRATION_MAX_PCT. Never
    raises."""
    out = {"available": False, "factor": 1.0, "lean_pct": None, "n": 0, "share": None, "reading": None,
           "reason": None}
    try:
        before = before or local_today(restaurant_id)
        start = (before - timedelta(days=days)).isoformat()
        end = (before - timedelta(days=1)).isoformat()
        nights, _other = _scored_nights(restaurant_id, start, end, db_path=db_path)
    except Exception as e:
        out["reason"] = f"forecast record unreadable: {e}"
        return out
    errs = []
    for n in nights.values():
        actual = n.get("sales.net")
        raw = n.get("sales.forecast_raw_net", n.get("sales.forecast_net"))
        if actual and actual > 0 and raw and raw > 0:
            errs.append((actual / raw - 1.0) * 100.0)
    out["n"] = len(errs)
    if len(errs) < CALIBRATION_MIN_NIGHTS:
        out["reason"] = (f"only {len(errs)} nights scored against their forecast in the last {days} days — "
                         f"a correction needs {CALIBRATION_MIN_NIGHTS}")
        return out
    lean = sum(errs) / len(errs)
    share = sum(1 for e in errs if (e > 0) == (lean > 0) and e != 0) / len(errs)
    out.update(available=True, lean_pct=round(lean, 1), share=round(share, 2))
    if abs(lean) < CALIBRATION_MIN_LEAN_PCT or share < CALIBRATION_MIN_SHARE:
        out["reading"] = "no consistent lean"
        return out
    bounded = max(-CALIBRATION_MAX_PCT, min(CALIBRATION_MAX_PCT, lean))
    out["factor"] = round(1.0 + bounded / 100.0, 4)
    out["reading"] = (f"nights here ran {abs(lean):.0f}% {'above' if lean > 0 else 'below'} the forecast across the "
                      f"last {len(errs)} scored nights")
    return out


def _apply_calibration(restaurant_id, day, out, db_path=DB_PATH):
    """Correct an available forecast by forecast_calibration, measured over
    the nights before `day` (and never past the restaurant's today): the
    range moves with it, `raw_sales` keeps the figure before it. Never
    raises."""
    out["raw_sales"] = out.get("typical_sales")
    try:
        today = local_today(restaurant_id)
        cal = forecast_calibration(restaurant_id, before=min(day, today), db_path=db_path)
    except Exception as e:
        print(f"[demand] forecast calibration unavailable for {restaurant_id}: {e}")
        return out
    if not cal.get("available") or cal.get("factor", 1.0) == 1.0:
        return out
    f = float(cal["factor"])
    out["typical_sales"] = round(out["typical_sales"] * f, 2)
    for k in ("low", "high"):
        if out.get(k) is not None:
            out[k] = round(out[k] * f, 2)
    out["calibration"] = {"factor": f, "lean_pct": cal["lean_pct"], "n": cal["n"], "reading": cal["reading"]}
    out["calibration_note"] = (f"corrected {(f - 1) * 100:+.0f}% because {cal['reading']} — before and after, "
                               "not proof")
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
     "actual_vs_forecast_pct" (mean signed: + means nights ran ABOVE the
     forecast, i.e. the forecast ran LOW), "bias_pct" (the same value, kept
     for shipped clients — NOT forecast_log's bias_pct, whose sign is the
     opposite), "bias_direction" (above_forecast | below_forecast |
     on_forecast), "bias_reading" (the sentence),
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
    today = today or local_today(restaurant_id)
    start = (today - timedelta(days=days)).isoformat()
    end = (today - timedelta(days=1)).isoformat()
    base = {"available": False, "n_nights": 0, "mean_error_pct": None, "bias_pct": None, "actual_vs_forecast_pct": None,
            "inside_range_pct": None, "n_ranged": 0, "window_days": days, "claim_kind": "measured",
            "skill_vs_last_pct": None, "skill_vs_mean_pct": None, "skill_pct": None, "n_skill": 0}
    nights, n_other = _scored_nights(restaurant_id, start, end, db_path=db_path)
    base["excluded_other_basis"] = n_other
    pcts = [n["sales.vs_forecast_pct"] for n in nights.values() if "sales.vs_forecast_pct" in n]
    ranged = [n for n in nights.values()
              if all(k in n for k in ("sales.net", "sales.forecast_low", "sales.forecast_high"))]
    base["n_nights"] = len(pcts)
    if len(pcts) < ACCURACY_MIN_NIGHTS:
        base["reason"] = (f"only {len(pcts)} nights scored against their forecast in the last {days} days "
                          f"— needs {ACCURACY_MIN_NIGHTS}")
        return base
    inside = [n for n in ranged if n["sales.forecast_low"] <= n["sales.net"] <= n["sales.forecast_high"]]
    avf = round(sum(pcts) / len(pcts), 1)
    base.update({
        "available": True,
        "mean_error_pct": round(sum(abs(p) for p in pcts) / len(pcts), 1),
        # actual ÷ forecast − 1, averaged: + means the nights ran ABOVE the
        # forecast (the forecast ran LOW). Named for what it is (T5, B6#2):
        # the web read `bias_pct` as "running X% high" — backwards — and
        # forecast_log's `bias_pct` is the opposite sign under the same
        # name. `bias_pct` stays, the same value, for shipped clients.
        "actual_vs_forecast_pct": avf,
        "bias_pct": avf,
        "bias_direction": ("above_forecast" if avf >= 1 else "below_forecast" if avf <= -1 else "on_forecast"),
        "bias_reading": (f"nights ran {abs(avf):.0f}% above the forecast on average — the forecast runs low"
                         if avf >= 1 else
                         f"nights ran {abs(avf):.0f}% below the forecast on average — the forecast runs high"
                         if avf <= -1 else "no consistent lean either way"),
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
    # The naive forecasts on the SAME basis as the nights they are scored
    # against (canonical_facts.net_series: the report's own net, an imported
    # workbook, a POS total on the same basis) — memory audit 9/29/26,
    # net_basis.
    import canonical_facts as _cf
    hist = {d: x["net"] for d, x in _cf.net_series(restaurant_id, first, last - timedelta(days=1),
                                                   db_path=db_path).items() if x["net"] and x["net"] > 0}
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
    [dates it covers], "by_day", "missing": [dates with no forecast],
    "modelled": {date: [event_memory labels whose measured effect the day's
    forecast already applied]}} — so a scored week's error is never blamed
    on an event the projection carried (forecast_log.explained_by)."""
    by_day, missing, modelled = {}, [], {}
    for d in week_dates or []:
        try:
            day = d if isinstance(d, date) else date.fromisoformat(str(d)[:10])
        except ValueError:
            continue
        # Uncorrected (calibrate=False): the frozen weekly projection has its
        # own record and its own correction (forecast_log revenue_week), and
        # a day figure already corrected would be corrected twice.
        fc = forecast_day(restaurant_id, day, db_path=db_path, calibrate=False)
        if fc.get("available"):
            by_day[day.isoformat()] = fc["typical_sales"]
            labels = [e.get("label") for e in (fc.get("effects") or []) + (fc.get("effects_subsumed") or [])
                      if e.get("label")]
            if labels:
                modelled[day.isoformat()] = labels
        else:
            missing.append(day.isoformat())
    return {"total": round(sum(by_day.values()), 2) if by_day else None,
            "days": sorted(by_day), "by_day": by_day, "missing": missing, "modelled": modelled}


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
            basis={"days": proj["days"], "method": "forecast_day median per weekday, frozen at publish",
                   "modelled": proj.get("modelled") or {}},
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
    today = today or local_today(restaurant_id)
    y = today - timedelta(days=1)
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT sales FROM labor_daily_history WHERE restaurant_id=? AND date=? "
                           f"AND sales IS NOT NULL AND sales > 0 AND {FINAL_SQL}", (restaurant_id, y.isoformat())).fetchone()
    finally:
        conn.close()
    if not row:
        return {"available": False, "reason": "no sales recorded for yesterday yet"}
    # "A typical <weekday>": the plain weekday median, not the forecast with
    # the night's measured effects applied.
    fc = forecast_day(restaurant_id, y, db_path=db_path, effects=False)
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
            hist = _weekday_history(restaurant_id, d["day"], local_today(restaurant_id) + timedelta(days=1),
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
            # The typical day each weekday is measured against, so a reader
            # quoting it says one figure (the Marketing Opportunity Feed).
            "typical_day": (round(float(overall), 2) if overall else None),
            "threshold_pct": SLOW_DAY_PCT, "consistency_floor": RELIABLY_SLOW_SHARE}


_WEEKDAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def weekday_gaps(restaurant_id, today=None, db_path=DB_PATH) -> dict:
    """Every weekday's typical night against the typical day, from ONE
    window (re-audit OPP-16): the LOOKBACK_WEEKS weeks of nights before
    `today` — today left out, its sales are still coming in — finished
    nights only (a night stored final=0 is one the POS has not closed; an
    older row with no flag counts as final). The same medians labor's
    forecast uses (each weekday's median; the typical day is the median of
    those), but on the restaurant's own date and one set of nights, so a
    card's % and its $ can never come from two windows.

    {"available", "typical_day", "start", "end" (ISO, end exclusive),
     "days": [{"day", "median_sales", "samples", "vs_typical_pct", "gap"
     (typical day minus the weekday's median), "under" (its nights under
     the typical day), "newest" (ISO)}], "reason"}. Never raises."""
    today = today or local_today(restaurant_id)
    start = today - timedelta(weeks=LOOKBACK_WEEKS)
    try:
        conn = get_conn(db_path)
        try:
            rows = conn.execute(
                "SELECT date, sales FROM labor_daily_history WHERE restaurant_id=? AND date>=? AND date<? "
                f"AND sales IS NOT NULL AND sales > 0 AND {FINAL_SQL} ORDER BY date",
                (restaurant_id, start.isoformat(), today.isoformat())).fetchall()
        finally:
            conn.close()
    except Exception as e:
        return {"available": False, "reason": f"sales history unreadable: {e}"}
    by_day = {}
    for r in rows:
        try:
            d = date.fromisoformat(str(r["date"])[:10])
        except ValueError:
            continue
        by_day.setdefault(_WEEKDAY_NAMES[d.weekday()], []).append((d.isoformat(), float(r["sales"])))
    # labor.build_demand_forecast's floor: three weekdays with two nights each.
    usable = {k: v for k, v in by_day.items() if len(v) >= 2}
    if len(usable) < 3:
        return {"available": False, "reason": "not enough finished nights of sales in the last "
                                              f"{LOOKBACK_WEEKS} weeks"}
    medians = {k: _median([s for _d, s in v]) for k, v in usable.items()}
    typical = _median(list(medians.values()))
    if not typical or typical <= 0:
        return {"available": False, "reason": "no usable sales figures"}
    days = []
    for k in _WEEKDAY_NAMES:
        if k not in usable:
            continue
        vals = [s for _d, s in usable[k]]
        med = medians[k]
        days.append({"day": k, "median_sales": round(med, 2), "samples": len(vals),
                     "vs_typical_pct": int(round((med / typical - 1) * 100)),
                     "gap": round(typical - med, 2), "under": sum(1 for v in vals if v < typical),
                     "newest": usable[k][-1][0]})
    return {"available": True, "typical_day": round(typical, 2), "start": start.isoformat(),
            "end": today.isoformat(), "days": days}


def reliably_slow_nights(restaurant_id, today=None, db_path=DB_PATH) -> dict:
    """The weekdays that run reliably under the typical day, slowest first —
    weekday_gaps' one window under slow_days' own rules: SLOW_DAY_PCT under,
    MIN_SAMPLES nights, RELIABLY_SLOW_SHARE of them under the typical day,
    the newest no more than STALE_SAMPLE_DAYS old. The one reading of "a
    slow night" the Marketing Opportunity Feed, the morning brief and the
    weekly digest all say (one owner, one key: slow_day:<Weekday>).

    {"available", "typical_day", "slow": [weekday_gaps day + "consistency"],
     "reason"}."""
    today = today or local_today(restaurant_id)
    g = weekday_gaps(restaurant_id, today=today, db_path=db_path)
    if not g.get("available"):
        return {"available": False, "reason": g.get("reason")}
    slow = []
    for d in g["days"]:
        n = d["samples"]
        if n < MIN_SAMPLES or d["vs_typical_pct"] > -SLOW_DAY_PCT:
            continue
        share = d["under"] / n
        if share < RELIABLY_SLOW_SHARE:
            continue
        try:
            if (today - date.fromisoformat(d["newest"])).days > STALE_SAMPLE_DAYS:
                continue
        except ValueError:
            continue
        slow.append(dict(d, consistency=round(share, 2)))
    slow.sort(key=lambda d: (d["vs_typical_pct"], d["day"]))
    return {"available": True, "typical_day": g["typical_day"], "slow": slow,
            "threshold_pct": SLOW_DAY_PCT, "consistency_floor": RELIABLY_SLOW_SHARE}


def prep_list(restaurant_id, day=None, db_path=DB_PATH, limit=15):
    """Expected ingredient usage for `day` against what's on hand.

    Built from each dish's typical quantity sold on that weekday (menu item
    sales) times its recipe. This is a USAGE forecast — it does not model
    sub-recipes or batch sizes, and says so — but it answers the question the
    kitchen asks the night before: what are we going to run out of.
    """
    day = day or (local_today(restaurant_id) + timedelta(days=1))
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
    # The measured effects of what is known about the date — a game night
    # measured at +30% here, the holiday, a guest text aimed at it — scale
    # the usage as they scale the sales forecast (event_memory.effects_for_day;
    # memory re-audit 9/29/26, CROSSMODULE-11): a game night used to prep a
    # normal Sunday.
    try:
        import event_memory
        eff = event_memory.effects_for_day(restaurant_id, day, db_path=db_path)
    except Exception:
        eff = None
    factor = 1.0 + float(eff["pct"]) / 100.0 if eff and eff.get("pct") else 1.0
    if factor != 1.0:
        expected = {mid: q * factor for mid, q in expected.items()}

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
    out = {"available": True, "day": day.isoformat(), "weekday": day.strftime("%A"),
           "items": rows[:limit], "dishes_forecast": len(expected),
           "promoted": promoted_dishes(restaurant_id, day, db_path=db_path),
           "note": ("A usage forecast from each dish's typical sales on this weekday times its "
                    "recipe. It does not model sub-recipes or batch sizes.")}
    if factor != 1.0:
        out.update(effect_pct=eff["pct"], effects=eff["applied"], effect_basis=eff["basis"])
        out["note"] += (f" Scaled {eff['pct']:+.0f}% for what is known about the date, as measured on nights "
                        "like it here — before and after, not proof.")
    return out


def promoted_dishes(restaurant_id, day, db_path=DB_PATH) -> list:
    """The dishes a post promotes on `day` (demand_signals source "post"
    with a menu item — memory audit 9/29/26, mkt_to_staffing): the kitchen
    prepped a normal weekday for a dish the owner was advertising. The
    usage forecast above is a weekday median and does not include any lift;
    this says which dish to prep above it, with the dish's own typical
    sales on that weekday when there are some."""
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT s.label, s.menu_item_id, m.name FROM demand_signals s LEFT JOIN menu_items m "
                            "ON m.id = s.menu_item_id WHERE s.restaurant_id=? AND s.date=? AND s.source='post' "
                            "AND s.menu_item_id IS NOT NULL", (restaurant_id, day.isoformat())).fetchall()
    except Exception:
        return []
    finally:
        conn.close()
    out = []
    for r in rows:
        out.append({"dish": r["name"] or r["label"], "menu_item_id": r["menu_item_id"],
                    "why": f"{r['label']} — prep above a usual {day.strftime('%A')}"})
    return out


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
    today = today or local_today(restaurant_id)
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
    [{"name", "display_name" (the name without the calendar's hint),
      "approximate" (a date the calendar only estimates — never measured),
      "date", "date_str" (M/D/YY), "days_away", "lift_pct", "based_on",
      "last_year_date", "last_year_weekday" (the night the lift was read
      from), "label", "claim_kind"}]."""
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
        name = chunk[:chunk.rfind("(")].strip()
        display = holiday_display_name(name)
        approximate = display in APPROXIMATE_HOLIDAYS
        entry = {"name": name, "display_name": display, "approximate": approximate,
                 "date": iso, "date_str": mdy(iso),
                 "days_away": away, "lift_pct": None, "based_on": None,
                 "label": HOLIDAY_GENERIC_LABEL, "claim_kind": None,
                 "last_year_date": None, "last_year_weekday": None}
        try:
            import schedule_economics
            # A holiday whose date is only approximated (the Super Bowl) has
            # no "last year's night" to read: the calendar's guess for last
            # year may not have been the game at all (re-audit OPP-1).
            own = {} if approximate else \
                ((schedule_economics.holiday_lift(restaurant_id, [iso], db_path=db_path) or {}).get(iso) or {})
            if own.get("lift_pct") is not None and own.get("based_on"):
                lift = int(own["lift_pct"])
                # Last year's holiday is measured against ITS weekday (Veterans
                # Day 2025 was a Tuesday), not this year's (re-audit OPP-2).
                ly = own.get("date")
                try:
                    ly_wd = datetime.strptime(str(ly)[:10], "%Y-%m-%d").strftime("%A") if ly else None
                except ValueError:
                    ly_wd = None
                entry.update({
                    "lift_pct": lift, "based_on": own["based_on"], "claim_kind": "measured",
                    "last_year_date": (str(ly)[:10] if ly_wd else None), "last_year_weekday": ly_wd,
                    "label": (f"Last year {abs(lift)}% {'above' if lift >= 0 else 'below'} a typical "
                              f"{ly_wd or 'night'} here — {own['based_on']}")})
        except Exception:
            pass
        out.append(entry)
    return out


# The calendar's names carry hints for the model ("Halloween — great for
# themed specials", "Veterans Day — many restaurants offer free/discounted
# meals for veterans"); an owner-facing card, and a goal typed into the
# Campaign Studio, say only the holiday's own name (re-audit OPP-1). The
# Super Bowl's date is the calendar's approximation (marketing.py), never
# stated as fact.
APPROXIMATE_HOLIDAYS = ("Super Bowl Sunday",)


def holiday_display_name(name) -> str:
    """The holiday's own name, without the calendar's hint after " — "."""
    return str(name or "").split(" — ", 1)[0].strip()

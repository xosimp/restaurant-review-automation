"""Level 1 → 3: Restaurant DNA — each restaurant's operational profile,
week by week, across the dimensions its own tables can measure
(Benchmarking audit 9/24/26, BM4 §5; Top-50 #24).

What it is for, in order:

  1. The restaurant's OWN profile (profile()): how steady its sales are, how
     its labor follows them, how regularly it logs waste, how often it acts
     on advice — each with the figure, the trend against four weeks ago, how
     it was measured, and for an unmeasured one, what data would measure it.
     Meaningful at any platform size; this is the Level 1 value.
  2. Similarity (distance()): a Gower-style, missing-aware distance over the
     STRUCTURAL dimensions, so peers can later be "restaurants shaped like
     yours" rather than one category key. Refuses (None) unless the two
     share at least MIN_SHARED_WEIGHT of the stated weight and
     MIN_SHARED_STRUCTURAL structural dimensions.
  3. The input to prediction (intelligence.predict): neighbours by distance.

The rules this module never bends:

  * Ratios, rates, shares, counts and bands only — NEVER dollars. The
    volume dimension stores a band index from stated edges, never the sales.
    Every stored row passes privacy.assert_anonymous (keys and values).
  * A dimension below its minimum data is None — never 0 — and carries what
    it needs ("needs 28 sales days, has 9").
  * No "personality" labels ("a steady-demand powerhouse"): a profile is a
    list of measured figures, never a type the model or the code names.
  * Normalisation is stated: below MIN_ROBUST_N restaurants measuring a
    dimension, z = (raw − centre) ÷ scale against the stated ANCHORS; from
    MIN_ROBUST_N up, the robust z (raw − median) ÷ (1.4826 × MAD) over the
    platform's latest rows. The centre and scale used ride with each value
    (`norm`), so a z is reproducible. Clipped to ±Z_CLIP.

Computed in the bounded nightly features pass (jobs.run_features), one
connection per restaurant, beside its feature row.
"""
import json
import math
from datetime import date, timedelta

import models as _models_mod
from models import DB_PATH
from . import privacy
from .stats import median, percentile


def get_conn(db_path=None):
    """models.get_conn, resolved at call time (CLAUDE.md, bound imports)."""
    if db_path is None or db_path == DB_PATH:
        return _models_mod.get_conn()
    return _models_mod.get_conn(db_path)


DNA_VERSION = 1
MIN_ROBUST_N = 30          # restaurants measuring a dimension before the robust z replaces the anchors
MAD_K = 1.4826
Z_CLIP = 3.0
TREND_WEEKS = 4            # profile(): the change against this many weeks ago
TREND_STEADY_SCALE = 0.25  # a move under this share of the stated scale reads "steady"

# Similarity (BM4 §5.2). Stated, not fitted: they become fitted only once
# the admin ordering check can judge them (≥200 counted results across ≥30
# restaurants). A test holds that they sum to 1.
STRUCTURAL_WEIGHTS = {
    "volume_band": 0.30,           # S1
    "weekend_share": 0.20,         # S2
    "daypart_mix": 0.15,           # S3
    "service_type": 0.15,          # S6
    "beverage_share": 0.10,        # S5 (dormant: a mapped daily sales report)
    "seasonality": 0.10,           # S4 (12 months of sales)
}
MIN_SHARED_WEIGHT = 0.60
MIN_SHARED_STRUCTURAL = 4
# A categorical mismatch (a different service type) counts as a difference
# of this many standard deviations — Gower's 0/1, on the z scale.
CATEGORICAL_MISMATCH_Z = 2.0
# Prediction (BM4 §5.3): the structural weights plus the target metric's
# baseline at this weight, renormalised.
PREDICTION_BASELINE_WEIGHT = 0.25

# Stated S1 band edges on mean daily net sales (the dollars are read to
# pick a band and never stored). Platform quintiles would need every
# restaurant's dollars in one place; the band index is all that leaves.
VOLUME_BAND_EDGES = (1500.0, 3000.0, 6000.0, 12000.0)

FAMILIES = (("sales", "Sales"), ("labor", "Labor and staffing"), ("guests", "Guests"),
            ("food", "Food, inventory and loss"), ("marketing", "Marketing"),
            ("loop", "Acting on recommendations"))

# key: code, family, label, unit, kind (numeric | categorical), transform
# (None | "log10"), anchor (centre, scale) in the transformed space, module
# (permissions.MODULE_VIEW_PERMISSIONS key that gates it; None = any login),
# needs (what measures it), structural, buildable (False = dormant: no source
# on the platform yet).
DIMENSIONS = {
    "volume_band": {"code": "S1", "family": "sales", "label": "Sales volume band", "unit": "band",
                    "anchor": (2.0, 1.0), "module": "labor", "structural": True,
                    "needs": "28 days of sales in the last 8 weeks"},
    "weekend_share": {"code": "S2", "family": "sales", "label": "Share of sales Friday to Sunday", "unit": "share",
                      "anchor": (0.45, 0.10), "module": "labor", "structural": True,
                      "needs": "14 days of sales in the last 4 weeks"},
    "daypart_mix": {"code": "S3", "family": "sales", "label": "Share of sales before 4pm", "unit": "share",
                    "anchor": (0.35, 0.10), "module": "labor", "structural": True,
                    "needs": "28 days of hourly POS sales in the last 8 weeks"},
    "seasonality": {"code": "S4", "family": "sales", "label": "Busiest month vs quietest", "unit": "ratio",
                    "transform": "log10", "anchor": (0.12, 0.08), "module": "labor", "structural": True,
                    "needs": "12 months of sales"},
    "beverage_share": {"code": "S5", "family": "sales", "label": "Bar and beverage share of sales", "unit": "share",
                       "anchor": (0.25, 0.10), "module": "labor", "structural": True, "buildable": False,
                       "needs": "a daily sales report with its departments mapped"},
    "service_type": {"code": "S6", "family": "sales", "label": "Restaurant type", "unit": "type",
                     "kind": "categorical", "module": None, "structural": True,
                     "needs": "your restaurant type set under Account"},
    "sales_volatility": {"code": "B1", "family": "sales", "label": "Day-to-day sales swing (after weekday pattern)",
                         "unit": "share", "anchor": (0.20, 0.10), "module": "labor",
                         "needs": "42 days of sales in the last 8 weeks"},
    "growth": {"code": "B2", "family": "sales", "label": "Sales trend", "unit": "pct_per_week",
               "anchor": (0.0, 1.0), "module": "labor", "needs": "13 complete weeks of sales"},
    "demand_predictability": {"code": "B3", "family": "sales", "label": "Forecast skill vs a naive guess",
                              "unit": "skill", "anchor": (0.0, 0.3), "module": "labor",
                              "needs": "6 scored weekly sales forecasts"},
    "labor_pct": {"code": "B4", "family": "labor", "label": "Labor %", "unit": "pct",
                  "anchor": (30.0, 3.0), "module": "labor", "better": "lower",
                  "needs": "14 costed days in the last 4 weeks"},
    "labor_hours_per_1k": {"code": "B4", "family": "labor", "label": "Labor hours per $1k of sales", "unit": "h",
                           "anchor": (11.0, 3.0), "module": "labor", "better": "lower",
                           "needs": "14 days with hours and sales in the last 4 weeks"},
    "labor_flex": {"code": "B5", "family": "labor", "label": "How closely hours follow sales", "unit": "elasticity",
                   "anchor": (0.5, 0.25), "module": "labor",
                   "needs": "28 days with hours and sales in the last 8 weeks"},
    "labor_swing": {"code": "B6", "family": "labor", "label": "Day-to-day labor % swing", "unit": "pts",
                    "anchor": (5.0, 2.0), "module": "labor", "better": "lower",
                    "needs": "14 costed days in the last 4 weeks"},
    "overtime_intensity": {"code": "B7", "family": "labor", "label": "Overtime share of hours", "unit": "share",
                           "anchor": (0.03, 0.03), "module": "labor", "better": "lower",
                           "needs": "4 whole payroll weeks of shifts"},
    "schedule_publish_rate": {"code": "B8", "family": "labor", "label": "Weeks with a published schedule",
                              "unit": "share", "anchor": (0.75, 0.20), "module": "labor",
                              "needs": "4 weeks since your first schedule in Cavnar"},
    "staffing_issues": {"code": "B9", "family": "labor", "label": "Coverage and no-show issues per 100 shifts",
                        "unit": "per100", "anchor": (3.0, 3.0), "module": "labor", "better": "lower",
                        "needs": "4 published weeks with their outcomes recorded"},
    "retention": {"code": "B10", "family": "labor", "label": "Staff who stopped appearing (90 days)",
                  "unit": "share", "anchor": (0.30, 0.15), "module": "labor", "better": "lower",
                  "needs": "180 days of shift history and 10 names with a last-seen date"},
    "tenure_depth": {"code": "B11", "family": "labor", "label": "Median days on the team", "unit": "days",
                     "transform": "log10", "anchor": (2.56, 0.30), "module": "labor",
                     "needs": "10 people seen in the last 45 days"},
    "rating_level": {"code": "B12", "family": "guests", "label": "Average rating (30 days)", "unit": "stars",
                     "anchor": (4.3, 0.3), "module": "reviews", "better": "higher",
                     "needs": "5 reviews in the last 30 days"},
    "negative_share": {"code": "B12", "family": "guests", "label": "Negative reviews (30 days)", "unit": "share",
                       "anchor": (0.15, 0.10), "module": "reviews", "better": "lower",
                       "needs": "5 analysed reviews in the last 30 days"},
    "rating_momentum": {"code": "B13", "family": "guests", "label": "Rating change vs the 60 days before",
                        "unit": "stars", "anchor": (0.0, 0.2), "module": "reviews", "better": "higher",
                        "needs": "5 reviews in the last 30 days and 5 in the 60 before"},
    "wait_service_complaints": {"code": "B14", "family": "guests",
                                "label": "Reviews complaining about wait or service (90 days)", "unit": "share",
                                "anchor": (0.05, 0.05), "module": "reviews", "better": "lower",
                                "needs": "20 analysed reviews in the last 90 days"},
    "reply_rate": {"code": "B15", "family": "guests", "label": "Reviews answered", "unit": "share",
                   "anchor": (0.6, 0.25), "module": "reviews", "better": "higher",
                   "needs": "5 reviews in the last 30 days"},
    "reply_within_day": {"code": "B15", "family": "guests", "label": "Replies within a day", "unit": "share",
                         "anchor": (0.5, 0.25), "module": "reviews", "better": "higher",
                         "needs": "5 timed replies in the last 30 days"},
    "food_cost_level": {"code": "B16", "family": "food", "label": "Food cost %", "unit": "pct",
                        "anchor": (30.0, 3.0), "module": "inventory", "better": "lower",
                        "needs": "counts and deliveries covering the last 4 weeks, and 14 sales days"},
    "food_cost_stability": {"code": "B17", "family": "food", "label": "Week-to-week food cost % swing",
                            "unit": "pts", "anchor": (1.5, 1.0), "module": "inventory", "better": "lower",
                            "needs": "6 measured weeks of food cost in the last 8"},
    "waste_rate": {"code": "B18", "family": "food", "label": "Waste as % of sales", "unit": "pct",
                   "anchor": (2.0, 1.0), "module": "inventory", "better": "lower",
                   "needs": "waste logged in at least 6 of the last 8 weeks"},
    "waste_logging_regularity": {"code": "F3", "family": "food", "label": "Weeks with waste logged (last 8)",
                                 "unit": "share", "anchor": (0.5, 0.3), "module": "inventory", "better": "higher",
                                 "needs": "8 weeks of inventory in Cavnar"},
    "count_discipline": {"code": "B19", "family": "food", "label": "Median days between counts", "unit": "days",
                         "transform": "log10", "anchor": (0.85, 0.30), "module": "inventory", "better": "lower",
                         "needs": "3 counts in the last 180 days"},
    "comp_rate": {"code": "B20", "family": "food", "label": "Comps as % of sales", "unit": "pct",
                  "anchor": (1.5, 1.0), "module": "inventory", "better": "lower",
                  "needs": "14 days of comp data from the POS in the last 4 weeks"},
    "void_rate": {"code": "B20", "family": "food", "label": "Voids as % of sales", "unit": "pct",
                  "anchor": (1.0, 0.7), "module": "inventory", "better": "lower",
                  "needs": "14 days of void data from the POS in the last 4 weeks"},
    "post_cadence": {"code": "B21", "family": "marketing", "label": "Posts in the last 4 weeks", "unit": "count",
                     "anchor": (4.0, 3.0), "module": "marketing", "needs": "a first post published through Cavnar"},
    "rec_uptake": {"code": "B24", "family": "loop", "label": "Recommendations taken", "unit": "share",
                   "anchor": (0.4, 0.2), "module": None, "needs": "10 recommendations answered or expired in 90 days"},
    "follow_through": {"code": "B25", "family": "loop", "label": "Accepted changes actually made", "unit": "share",
                       "anchor": (0.5, 0.25), "module": None, "needs": "5 accepted recommendations in 180 days"},
    "improvement_rate": {"code": "B26", "family": "loop", "label": "Measured results that improved",
                         "unit": "share", "anchor": (0.4, 0.2), "module": None, "better": "higher",
                         "needs": "5 measured results in 90 days"},
    "data_hygiene": {"code": "B27", "family": "loop", "label": "Data completeness × health", "unit": "share",
                     "anchor": (0.5, 0.25), "module": None, "better": "higher",
                     "needs": "a feature row and a Data Health reading this week"},
}
for _k, _v in DIMENSIONS.items():
    _v.setdefault("kind", "numeric")
    _v.setdefault("transform", None)
    _v.setdefault("structural", False)
    _v.setdefault("buildable", True)
    _v.setdefault("better", None)

STRUCTURAL = tuple(k for k, v in DIMENSIONS.items() if v["structural"])


# ── measuring ───────────────────────────────────────────────────────────────

def _m(raw, n, basis):
    return {"raw": raw, "n": n, "basis": basis}


def _need(n, have_text=""):
    return {"raw": None, "n": n, "basis": have_text}


def _d(x):
    return str(x or "")[:10]


def _sales_days(conn, rid, since):
    """[(date, sales, hours, labor_pct)] — final days with sales, oldest first."""
    try:
        rows = conn.execute("SELECT date, sales, total_hours, labor_pct FROM labor_daily_history WHERE restaurant_id=? "
                            "AND date >= ? AND sales > 0 AND COALESCE(final, 1) = 1 ORDER BY date",
                            (rid, since.isoformat())).fetchall()
    except Exception:
        rows = conn.execute("SELECT date, sales, total_hours, labor_pct FROM labor_daily_history WHERE restaurant_id=? "
                            "AND date >= ? AND sales > 0 ORDER BY date", (rid, since.isoformat())).fetchall()
    out, seen = [], set()
    for r in rows:
        d = _d(r["date"])
        if d in seen:
            continue
        seen.add(d)
        try:
            out.append((date.fromisoformat(d), float(r["sales"]),
                        float(r["total_hours"]) if r["total_hours"] else None,
                        float(r["labor_pct"]) if r["labor_pct"] is not None else None))
        except (TypeError, ValueError):
            continue
    return out


def _sd(xs):
    if len(xs) < 2:
        return None
    m = sum(xs) / len(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def _volume_band(days, today):
    recent = [s for d, s, _h, _p in days if d >= today - timedelta(days=56)]
    if len(recent) < 28:
        return _need(len(recent), f"{len(recent)} sales days in the last 8 weeks")
    mean = sum(recent) / len(recent)
    band = sum(1 for e in VOLUME_BAND_EDGES if mean >= e)
    return _m(band, len(recent), f"band {band} of 0–4 on stated edges, from {len(recent)} sales days")


def _weekend_share(days, today):
    recent = [(d, s) for d, s, _h, _p in days if d >= today - timedelta(days=28)]
    if len(recent) < 14:
        return _need(len(recent), f"{len(recent)} sales days in the last 4 weeks")
    tot = sum(s for _d2, s in recent)
    wk = sum(s for d, s in recent if d.weekday() >= 4)
    return _m(round(wk / tot, 3), len(recent), f"Friday to Sunday share over {len(recent)} sales days")


def _daypart_mix(conn, rid, days, today):
    """Share of the day's net sales rung before 16:00, from the POS's hourly
    running total (pos_intraday) against the day's final sales. The
    schedule_outcomes split is NOT used: it divides the day by a morning
    share that falls back to a stated 0.4 — not a measurement."""
    since = today - timedelta(days=56)
    try:
        rows = conn.execute("SELECT business_date, MAX(CASE WHEN captured_hour <= 16 THEN net_sales END) AS early "
                            "FROM pos_intraday WHERE restaurant_id=? AND business_date >= ? GROUP BY business_date",
                            (rid, since.isoformat())).fetchall()
    except Exception:
        rows = []
    final = {d.isoformat(): s for d, s, _h, _p in days}
    early = tot = 0.0
    n = 0
    for r in rows:
        f = final.get(_d(r["business_date"]))
        e = r["early"]
        if not f or e is None or float(e) < 0 or float(e) > f * 1.05:
            continue
        early += min(float(e), f)
        tot += f
        n += 1
    if n < 28 or not tot:
        return _need(n, f"{n} days of hourly POS sales in the last 8 weeks")
    return _m(round(early / tot, 3), n, f"sales rung before 4pm over {n} days (POS hourly totals)")


def _seasonality(days, today):
    """log10(busiest ÷ quietest month) over the last 12 complete months, each
    with at least 20 sales days."""
    first_of_month = today.replace(day=1)
    months = {}
    for d, s, _h, _p in days:
        if d >= first_of_month:
            continue
        months.setdefault((d.year, d.month), []).append(s)
    keys = sorted(months)[-12:]
    good = [sum(months[k]) / len(months[k]) for k in keys if len(months[k]) >= 20]
    if len(keys) < 12 or len(good) < 12:
        return _need(len(good), f"{len(good)} complete months of sales")
    lo, hi = min(good), max(good)
    if lo <= 0:
        return _need(len(good), "a month with no sales")
    return _m(round(hi / lo, 3), 12, "busiest month's daily sales ÷ quietest month's, last 12 months")


def _service_type(restaurant):
    """S6: the service model the owner set when the profile carries one,
    else the restaurant type — only when the owner SET it. A type guessed
    from the name never places a restaurant among peers (BM1-3, BM2-2)."""
    if restaurant is None:
        return _need(0, "no restaurant")
    for attr in ("service_model", "service_format"):
        v = "_".join(str(getattr(restaurant, attr, None) or "").strip().lower().replace("-", " ").split())
        v = "".join(ch for ch in v if ch.isalnum() or ch == "_")
        if v:
            return _m(f"service:{v}"[:40], 1, "the service model you set")
    try:
        from . import categories
        cat, src = categories.category_for(restaurant)
    except Exception:
        cat, src = None, None
    if cat and src == "set" and cat != "other":
        return _m(cat, 1, "the restaurant type you set")
    if cat and src == "inferred":
        return _need(0, "the type was only guessed from the name")
    return _need(0, "no restaurant type set")


def _volatility(days, today):
    recent = [(d, s) for d, s, _h, _p in days if d >= today - timedelta(days=56)]
    if len(recent) < 42:
        return _need(len(recent), f"{len(recent)} sales days in the last 8 weeks")
    by_wd = {}
    for d, s in recent:
        by_wd.setdefault(d.weekday(), []).append(s)
    means = {wd: sum(v) / len(v) for wd, v in by_wd.items()}
    resid = [s / means[d.weekday()] - 1.0 for d, s in recent if means.get(d.weekday())]
    sd = _sd(resid)
    if sd is None:
        return _need(len(recent), "no spread")
    return _m(round(sd, 3), len(recent), f"residual swing after each weekday's own average, {len(recent)} days")


def _growth(days, today):
    """13-week log-slope of weekly sales, % per week, over complete weeks
    with at least 5 sales days (a partial week is not a lower week)."""
    this_monday = today - timedelta(days=today.weekday())
    start = this_monday - timedelta(weeks=13)
    weeks = {}
    for d, s, _h, _p in days:
        if start <= d < this_monday:
            weeks.setdefault((d - start).days // 7, []).append(s)
    pts = [(k, math.log(sum(v) / len(v) * 7)) for k, v in sorted(weeks.items()) if len(v) >= 5 and sum(v) > 0]
    if len(pts) < 13:
        return _need(len(pts), f"{len(pts)} complete weeks of sales in the last 13")
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    den = sum((x - mx) ** 2 for x in xs)
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den if den else 0.0
    return _m(round((math.exp(slope) - 1) * 100, 2), len(pts), "13-week trend of weekly sales, % per week")


def _predictability(conn, rid):
    try:
        rows = conn.execute("SELECT error_pct, naive_last, actual FROM forecast_log WHERE restaurant_id=? "
                            "AND kind='revenue_week' AND actual IS NOT NULL AND error_pct IS NOT NULL "
                            "AND naive_last IS NOT NULL ORDER BY horizon_end DESC LIMIT 26", (rid,)).fetchall()
    except Exception:
        rows = []
    errs, naive = [], []
    for r in rows:
        try:
            a = float(r["actual"])
            if a <= 0:
                continue
            errs.append(abs(float(r["error_pct"])))
            naive.append(abs(float(r["naive_last"]) - a) / a * 100.0)
        except (TypeError, ValueError):
            continue
    if len(errs) < 6:
        return _need(len(errs), f"{len(errs)} scored weekly forecasts")
    mn = median(naive)
    if not mn:
        return _need(len(errs), "the naive guess was exact every week")
    return _m(round(1.0 - median(errs) / mn, 3), len(errs),
              f"1 − median forecast error ÷ median naive error, {len(errs)} scored weeks")


def _labor_flex(days, today):
    pts = [(s, h) for d, s, h, _p in days if d >= today - timedelta(days=56) and h]
    if len(pts) < 28:
        return _need(len(pts), f"{len(pts)} days with hours and sales in the last 8 weeks")
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    den = sum((x - mx) ** 2 for x in xs)
    if not den or not my:
        return _need(len(pts), "sales did not vary")
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den
    return _m(round(slope * mx / my, 3), len(pts),
              f"how much hours move when sales move (0 fixed, 1 proportional), {len(pts)} days")


def _overtime(rid, days, today, db_path):
    import re
    try:
        import metrics
        start = today - timedelta(days=42)
        val, detail = metrics.measure(rid, "overtime_hours", start.isoformat(), today.isoformat(), db_path)
    except Exception:
        return _need(0, "shifts could not be read")
    m = re.search(r"over (\d+) payroll week", str(detail or ""))
    weeks = int(m.group(1)) if m else 0
    if val is None or weeks < 4:
        return _need(weeks, f"{weeks} whole payroll weeks of shifts")
    hrs = [h for d, _s, h, _p in days if d >= today - timedelta(days=42) and h]
    if len(hrs) < 14:
        return _need(weeks, f"{len(hrs)} days with hours on file")
    per_week = sum(hrs) / (len(hrs) / 7.0)
    if not per_week:
        return _need(weeks, "no hours on file")
    return _m(round(min(1.0, float(val) / per_week), 3), weeks,
              f"overtime hours ÷ all hours, {weeks} payroll weeks")


def _publish_rate(conn, rid, today):
    try:
        first = conn.execute("SELECT MIN(substr(generated_at,1,10)) FROM schedule_history WHERE restaurant_id=?",
                             (rid,)).fetchone()[0]
    except Exception:
        return _need(0, "no schedule history")
    if not first:
        return _need(0, "no schedule built in Cavnar yet")
    try:
        age_weeks = (today - date.fromisoformat(str(first)[:10])).days // 7
    except ValueError:
        return _need(0, "no schedule history")
    if age_weeks < 4:
        return _need(age_weeks, f"{age_weeks} weeks since the first schedule")
    window = min(8, age_weeks)
    since = today - timedelta(weeks=window)
    pub = conn.execute("SELECT COUNT(DISTINCT week_start) FROM schedule_history WHERE restaurant_id=? "
                       "AND published_at IS NOT NULL AND week_start >= ?", (rid, since.isoformat())).fetchone()[0] or 0
    return _m(round(min(1.0, pub / float(window)), 3), window, f"{pub} of the last {window} weeks published")


def _staffing_issues(conn, rid, today):
    try:
        r = conn.execute("SELECT COUNT(DISTINCT history_id) AS w, SUM(issues) AS i, SUM(people) AS p "
                         "FROM schedule_outcomes WHERE restaurant_id=? AND date >= ?",
                         (rid, (today - timedelta(days=56)).isoformat())).fetchone()
    except Exception:
        return _need(0, "no schedule outcomes")
    weeks = int(r["w"] or 0) if r else 0
    if weeks < 4 or not (r["p"] or 0):
        return _need(weeks, f"{weeks} published weeks with outcomes in the last 8")
    return _m(round(float(r["i"] or 0) / float(r["p"]) * 100.0, 2), weeks,
              f"coverage and no-show issues per 100 people-shifts, {weeks} weeks")


def _staff_rows(conn, rid):
    try:
        return [dict(r) for r in conn.execute("SELECT first_seen, last_seen, shifts_seen, updated_at "
                                              "FROM staff_first_seen WHERE restaurant_id=?", (rid,)).fetchall()]
    except Exception:
        return []


def _retention(staff, today):
    """Of the names with at least 4 shifts seen who were working 91–180
    days ago, the share not seen for 45+ days (BM4-8). Dormant until
    last_seen fills: a row without it is left out."""
    with_last = [s for s in staff if s.get("last_seen")]
    firsts = sorted(_d(s["first_seen"]) for s in staff if s.get("first_seen"))
    if not firsts or firsts[0] > (today - timedelta(days=180)).isoformat():
        return _need(len(with_last), "under 180 days of shift history")
    lo, hi = (today - timedelta(days=180)).isoformat(), (today - timedelta(days=91)).isoformat()
    base = [s for s in with_last if int(s.get("shifts_seen") or 0) >= 4 and _d(s["first_seen"]) <= hi
            and _d(s["last_seen"]) >= lo]
    if len(base) < 10:
        return _need(len(base), f"{len(base)} names with a last-seen date")
    gone = sum(1 for s in base if _d(s["last_seen"]) < (today - timedelta(days=45)).isoformat())
    return _m(round(gone / float(len(base)), 3), len(base), f"{gone} of {len(base)} not seen in 45 days")


def _tenure(staff, today):
    cut = (today - timedelta(days=45)).isoformat()
    days = []
    for s in staff:
        seen = _d(s.get("last_seen") or s.get("updated_at"))
        if not seen or seen < cut or not s.get("first_seen"):
            continue
        try:
            days.append(max(0, (today - date.fromisoformat(_d(s["first_seen"]))).days))
        except ValueError:
            continue
    if len(days) < 10:
        return _need(len(days), f"{len(days)} people seen in the last 45 days")
    return _m(int(median(days)), len(days), f"median days since first shift, {len(days)} active people")


def _review_mix(conn, rid, today):
    try:
        rows = conn.execute("SELECT review_date, sentiment, categories FROM reviews WHERE restaurant_id=? "
                            "AND COALESCE(review_date, fetched_at) >= ?",
                            (rid, (today - timedelta(days=90)).isoformat())).fetchall()
    except Exception:
        rows = []
    d30 = (today - timedelta(days=30)).isoformat()
    last30 = [r for r in rows if r["sentiment"] and _d(r["review_date"]) >= d30]
    if len(last30) >= 5:
        neg = _m(round(sum(1 for r in last30 if r["sentiment"] == "negative") / float(len(last30)), 3),
                 len(last30), f"{len(last30)} analysed reviews, last 30 days")
    else:
        neg = _need(len(last30), f"{len(last30)} analysed reviews in 30 days")
    analysed = [r for r in rows if r["sentiment"]]
    if len(analysed) >= 20:
        hit = 0
        for r in analysed:
            if r["sentiment"] != "negative":
                continue
            try:
                cats = json.loads(r["categories"] or "[]")
            except (TypeError, ValueError):
                cats = []
            if any(str(c).lower() in ("wait_time", "service") for c in cats or []):
                hit += 1
        wait = _m(round(hit / float(len(analysed)), 3), len(analysed),
                  f"negative reviews about wait or service, share of {len(analysed)} reviews in 90 days")
    else:
        wait = _need(len(analysed), f"{len(analysed)} analysed reviews in 90 days")
    return neg, wait


def _food_cost_stability(rid, today, db_path):
    try:
        import metrics
    except Exception:
        return _need(0, "unavailable")
    vals = []
    for k in range(8):
        end = today - timedelta(days=7 * k)
        start = end - timedelta(days=6)
        try:
            v, _ = metrics.measure(rid, "food_cost_pct", start.isoformat(), end.isoformat(), db_path)
        except Exception:
            v = None
        if v is not None:
            vals.append(float(v))
    if len(vals) < 6:
        return _need(len(vals), f"{len(vals)} measured weeks of the last 8")
    return _m(round(_sd(vals), 2), len(vals), f"spread of weekly food cost %, {len(vals)} weeks")


def _count_discipline(conn, rid, today):
    try:
        rows = conn.execute("SELECT DISTINCT substr(event_date,1,10) AS d FROM ingredient_stock_events "
                            "WHERE restaurant_id=? AND event_type='recount' AND COALESCE(source,'') != 'migration' "
                            "AND substr(event_date,1,10) >= ? ORDER BY d",
                            (rid, (today - timedelta(days=180)).isoformat())).fetchall()
    except Exception:
        rows = []
    ds = []
    for r in rows:
        try:
            ds.append(date.fromisoformat(r["d"]))
        except (TypeError, ValueError):
            continue
    if len(ds) < 3:
        return _need(len(ds), f"{len(ds)} count days in the last 180")
    gaps = [(b - a).days for a, b in zip(ds, ds[1:])]
    return _m(round(median(gaps), 1), len(ds), f"median days between the {len(ds)} counts of the last 180 days")


def _loss_rate(conn, rid, kind, today):
    try:
        r = conn.execute("SELECT COUNT(*) AS days, COALESCE(SUM(p.amount),0) AS amt, SUM(l.sales) AS s "
                         "FROM pos_loss_daily p JOIN labor_daily_history l ON l.restaurant_id=p.restaurant_id "
                         "AND l.date=p.business_date AND l.sales > 0 WHERE p.restaurant_id=? AND p.kind=? "
                         "AND p.business_date >= ?", (rid, kind, (today - timedelta(days=28)).isoformat())).fetchone()
    except Exception:
        return _need(0, "no loss data from the POS")
    days = int(r["days"] or 0) if r else 0
    if days < 14 or not (r["s"] or 0):
        return _need(days, f"{days} days of {kind} data in the last 4 weeks")
    return _m(round(float(r["amt"]) / float(r["s"]) * 100.0, 2), days, f"{kind}s ÷ sales over {days} asked days")


def _post_cadence(conn, rid, f):
    try:
        ever = conn.execute("SELECT 1 FROM marketing_content_log WHERE restaurant_id=? AND post_id IS NOT NULL LIMIT 1",
                            (rid,)).fetchone()
    except Exception:
        ever = None
    if not ever or f.get("posts_28d") is None:
        return _need(0, "no post published through Cavnar yet")
    return _m(int(f["posts_28d"]), int(f["posts_28d"]), "posts published in the last 28 days")


def _rec_uptake(conn, rid, today):
    from .scoring import _summarise
    try:
        rows = conn.execute("SELECT restaurant_id, source_key, action, outcome, days_to_effect FROM intel_rec_events "
                            "WHERE restaurant_id=? AND event_at >= ?",
                            (rid, (today - timedelta(days=90)).isoformat())).fetchall()
    except Exception:
        rows = []
    s = _summarise(rows, cross=False)
    settled = int(s.get("answered") or 0) + int(s.get("ignored") or 0)
    if settled < 10 or s.get("acceptance_rate") is None:
        return _need(settled, f"{settled} recommendations settled in 90 days")
    return _m(s["acceptance_rate"], settled, f"taken ÷ settled (answered or expired), {settled} in 90 days")


def _follow_through(conn, rid, today):
    try:
        rows = conn.execute("SELECT status, implemented_at FROM rec_instances WHERE restaurant_id=? AND created_at >= ? "
                            "AND (status IN ('accepted','completed','implemented') OR implemented_at IS NOT NULL)",
                            (rid, (today - timedelta(days=180)).isoformat())).fetchall()
    except Exception:
        rows = []
    if len(rows) < 5:
        return _need(len(rows), f"{len(rows)} accepted recommendations in 180 days")
    made = sum(1 for r in rows if r["implemented_at"])
    return _m(round(made / float(len(rows)), 3), len(rows), f"{made} of {len(rows)} accepted changes seen made")


def _data_hygiene(conn, rid, f, today):
    from .features import completeness
    try:
        r = conn.execute("SELECT overall FROM data_health_daily WHERE restaurant_id=? AND date >= ? "
                         "ORDER BY date DESC LIMIT 1", (rid, (today - timedelta(days=7)).isoformat())).fetchone()
    except Exception:
        r = None
    if not f or not r or r["overall"] is None:
        return _need(0, "no Data Health reading this week")
    return _m(round(completeness(f) * float(r["overall"]) / 100.0, 3), 1,
              "feature completeness × Data Health score")


def _from_feature(f, key, dim, n=None):
    v = (f or {}).get(key)
    if v is None:
        return _need(0, "")
    return _m(v, n, f"from this week's {key} feature")


def measure(restaurant_id, today=None, db_path=DB_PATH, features=None, restaurant=None) -> dict:
    """{dim: {raw, n, basis}} for every dimension — raw None below its
    minimum data. Pure read."""
    today = today or date.today()
    if features is None:
        from . import features as _f
        latest = _f.latest(restaurant_id, db_path=db_path)
        features = (latest or {}).get("features") or {}
    f = features or {}
    if restaurant is None:
        try:
            restaurant = _models_mod.get_restaurant(restaurant_id, db_path=db_path) if db_path != DB_PATH \
                else _models_mod.get_restaurant(restaurant_id)
        except Exception:
            restaurant = None
    out = {}
    conn = get_conn(db_path)
    try:
        days = _sales_days(conn, restaurant_id, today - timedelta(days=400))
        staff = _staff_rows(conn, restaurant_id)
        runs = {
            "volume_band": lambda: _volume_band(days, today),
            "weekend_share": lambda: _weekend_share(days, today),
            "daypart_mix": lambda: _daypart_mix(conn, restaurant_id, days, today),
            "seasonality": lambda: _seasonality(days, today),
            "beverage_share": lambda: _need(0, "no mapped daily sales report"),
            "service_type": lambda: _service_type(restaurant),
            "sales_volatility": lambda: _volatility(days, today),
            "growth": lambda: _growth(days, today),
            "demand_predictability": lambda: _predictability(conn, restaurant_id),
            "labor_pct": lambda: _from_feature(f, "labor_pct_28d", "labor_pct"),
            "labor_hours_per_1k": lambda: _from_feature(f, "labor_hours_per_1k_28d", "labor_hours_per_1k"),
            "labor_flex": lambda: _labor_flex(days, today),
            "labor_swing": lambda: _from_feature(f, "labor_pct_sd_28d", "labor_swing"),
            "overtime_intensity": lambda: _overtime(restaurant_id, days, today, db_path),
            "schedule_publish_rate": lambda: _publish_rate(conn, restaurant_id, today),
            "staffing_issues": lambda: _staffing_issues(conn, restaurant_id, today),
            "retention": lambda: _retention(staff, today),
            "tenure_depth": lambda: _tenure(staff, today),
            "rating_level": lambda: _from_feature(f, "avg_rating_30d", "rating_level", f.get("reviews_30d")),
            "rating_momentum": lambda: _from_feature(f, "avg_rating_delta", "rating_momentum"),
            "reply_rate": lambda: _from_feature(f, "reply_rate_30d", "reply_rate", f.get("reviews_30d")),
            "reply_within_day": lambda: _from_feature(f, "response_24h_rate_30d", "reply_within_day"),
            "food_cost_level": lambda: _from_feature(f, "food_cost_pct_28d", "food_cost_level"),
            "food_cost_stability": lambda: _food_cost_stability(restaurant_id, today, db_path),
            "count_discipline": lambda: _count_discipline(conn, restaurant_id, today),
            "comp_rate": lambda: _loss_rate(conn, restaurant_id, "comp", today),
            "void_rate": lambda: _loss_rate(conn, restaurant_id, "void", today),
            "post_cadence": lambda: _post_cadence(conn, restaurant_id, f),
            "rec_uptake": lambda: _rec_uptake(conn, restaurant_id, today),
            "follow_through": lambda: _follow_through(conn, restaurant_id, today),
            "improvement_rate": lambda: _from_feature(f, "outcomes_improved_rate_90d", "improvement_rate"),
            "data_hygiene": lambda: _data_hygiene(conn, restaurant_id, f, today),
        }
        for dim, fn in runs.items():
            try:
                out[dim] = fn()
            except Exception as e:
                print(f"[intelligence.dna] {dim} unavailable for {restaurant_id}: {e}")
                out[dim] = _need(0, "could not be read")
        try:
            out["negative_share"], out["wait_service_complaints"] = _review_mix(conn, restaurant_id, today)
        except Exception as e:
            print(f"[intelligence.dna] review mix unavailable for {restaurant_id}: {e}")
            out["negative_share"] = out["wait_service_complaints"] = _need(0, "could not be read")
        # F3 and B18: the waste % enters the DNA only when waste is logged
        # regularly (BM4-16) — "doesn't log waste" is never "low waste".
        from .features import WASTE_REGULARITY_KEY, WASTE_REGULARITY_MIN, waste_log_regularity
        reg = f.get(WASTE_REGULARITY_KEY) if WASTE_REGULARITY_KEY in f else waste_log_regularity(conn, restaurant_id,
                                                                                               today)[0]
        out["waste_logging_regularity"] = (_m(reg, 8, "share of the last 8 weeks with waste logged")
                                           if reg is not None else _need(0, "under 8 weeks of inventory"))
        w = f.get("waste_sales_pct_28d")
        if w is None:
            out["waste_rate"] = _need(0, "no waste % this week")
        elif reg is None or float(reg) < WASTE_REGULARITY_MIN:
            out["waste_rate"] = _need(0, "waste isn't logged regularly enough to read a rate")
        else:
            out["waste_rate"] = _m(w, 8, "waste ÷ sales, last 28 days, with waste logged regularly")
    finally:
        conn.close()
    for dim, v in out.items():
        if v.get("raw") is None:
            have = v.get("basis") or ""
            v["basis"] = f"needs {DIMENSIONS[dim]['needs']}" + (f" (has {have})" if have else "")
    return out


# ── normalising ─────────────────────────────────────────────────────────────

def _t(dim, raw):
    """The value in the dimension's normalisation space (log10 where stated)."""
    if raw is None:
        return None
    try:
        x = float(raw)
    except (TypeError, ValueError):
        return None
    if DIMENSIONS[dim]["transform"] == "log10":
        return math.log10(max(x, 1e-6))
    return x


def anchor_norms() -> dict:
    return {dim: {"centre": v["anchor"][0], "scale": v["anchor"][1], "kind": "anchor", "n": None}
            for dim, v in DIMENSIONS.items() if v["kind"] == "numeric" and v.get("anchor")}


def platform_norms(db_path=DB_PATH, weeks=2) -> dict:
    """Per numeric dimension: the robust centre and scale over the latest
    real restaurants' raw values when at least MIN_ROBUST_N measure it,
    else the stated anchor. Read once per nightly pass."""
    from .features import iso_week
    from .jobs import seeded_restaurant_ids
    norms = anchor_norms()
    floor = iso_week(date.today() - timedelta(weeks=weeks))
    conn = get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT d.restaurant_id, d.dims_json FROM intel_dna d JOIN (SELECT restaurant_id, MAX(week) AS week "
            "FROM intel_dna GROUP BY restaurant_id) m ON m.restaurant_id=d.restaurant_id AND m.week=d.week "
            "WHERE d.week >= ?", (floor,)).fetchall()
    except Exception:
        rows = []
    finally:
        conn.close()
    seeded = seeded_restaurant_ids(db_path=db_path)
    vals = {}
    for r in rows:
        if r["restaurant_id"] in seeded:
            continue
        try:
            dims = json.loads(r["dims_json"] or "{}")
        except (TypeError, ValueError):
            continue
        for dim, e in dims.items():
            if dim in norms:
                x = _t(dim, (e or {}).get("raw"))
                if x is not None:
                    vals.setdefault(dim, []).append(x)
    for dim, xs in vals.items():
        if len(xs) < MIN_ROBUST_N:
            continue
        med = percentile(xs, 50)
        mad = percentile([abs(x - med) for x in xs], 50)
        if mad and mad > 0:
            norms[dim] = {"centre": round(med, 6), "scale": round(MAD_K * mad, 6), "kind": "robust", "n": len(xs)}
    return norms


def normalise(measured: dict, norms: dict = None) -> dict:
    """{dim: {raw, z, n, basis, norm}}: z clipped to ±Z_CLIP, None when the
    raw is None or the dimension is categorical."""
    norms = norms or anchor_norms()
    out = {}
    for dim, m in measured.items():
        e = {"raw": m.get("raw"), "z": None, "n": m.get("n"), "basis": m.get("basis") or ""}
        nm = norms.get(dim)
        x = _t(dim, e["raw"]) if DIMENSIONS.get(dim, {}).get("kind") == "numeric" else None
        if x is not None and nm and nm.get("scale"):
            z = (x - float(nm["centre"])) / float(nm["scale"])
            e["z"] = round(max(-Z_CLIP, min(Z_CLIP, z)), 3)
            e["norm"] = {"kind": nm["kind"], "centre": nm["centre"], "scale": nm["scale"],
                         "transform": DIMENSIONS[dim]["transform"]}
        out[dim] = e
    return out


def coverage(dims: dict) -> float:
    buildable = [d for d, v in DIMENSIONS.items() if v["buildable"]]
    have = sum(1 for d in buildable if (dims.get(d) or {}).get("raw") is not None)
    return round(have / float(len(buildable)), 3) if buildable else 0.0


def _check_private(dims: dict):
    """assert_anonymous on the row, keys and values. A categorical raw is a
    code from a closed vocabulary (categories.TAXONOMY, or a service model
    the profile stores) and is checked by shape instead, so a tenant that
    happens to be named like a cuisine cannot make the whole row unstorable."""
    safe = {}
    for dim, e in dims.items():
        e2 = dict(e)
        if DIMENSIONS.get(dim, {}).get("kind") == "categorical" and e2.get("raw") is not None:
            raw = str(e2["raw"])
            if len(raw) > 40 or not all(ch.isalnum() or ch in "_:-" for ch in raw):
                raise privacy.PrivacyError(f"dims.{dim}.raw is not a type code")
            e2["raw"] = "type"
        safe[dim] = e2
    privacy.assert_anonymous({"dims": safe})
    return dims


def compute(restaurant_id, today=None, db_path=DB_PATH, features=None, restaurant=None, norms=None) -> dict:
    """{week, dims, coverage, version} for this restaurant as of `today`."""
    from .features import iso_week
    today = today or date.today()
    dims = normalise(measure(restaurant_id, today=today, db_path=db_path, features=features, restaurant=restaurant),
                     norms)
    _check_private(dims)
    return {"week": iso_week(today), "dims": dims, "coverage": coverage(dims), "version": DNA_VERSION}


def store(restaurant_id, row: dict, db_path=DB_PATH):
    conn = get_conn(db_path)
    try:
        conn.execute("INSERT INTO intel_dna (restaurant_id, week, dims_json, coverage, version) VALUES (?,?,?,?,?) "
                     "ON CONFLICT(restaurant_id, week) DO UPDATE SET dims_json=excluded.dims_json, "
                     "coverage=excluded.coverage, version=excluded.version, computed_at=datetime('now')",
                     (restaurant_id, row["week"], json.dumps(row["dims"]), row["coverage"], row["version"]))
        conn.commit()
    finally:
        conn.close()
    return row["week"]


def compute_and_store(restaurant_id, today=None, db_path=DB_PATH, features=None, restaurant=None, norms=None):
    row = compute(restaurant_id, today=today, db_path=db_path, features=features, restaurant=restaurant, norms=norms)
    store(restaurant_id, row, db_path=db_path)
    return row


def latest(restaurant_id, db_path=DB_PATH):
    conn = get_conn(db_path)
    try:
        r = conn.execute("SELECT week, dims_json, coverage, version, computed_at FROM intel_dna WHERE restaurant_id=? "
                         "ORDER BY week DESC LIMIT 1", (restaurant_id,)).fetchone()
    finally:
        conn.close()
    if not r:
        return None
    return {"week": r["week"], "dims": json.loads(r["dims_json"] or "{}"), "coverage": r["coverage"],
            "version": r["version"], "computed_at": r["computed_at"]}


def latest_by_restaurant(db_path=DB_PATH, max_age_weeks=3) -> dict:
    """{restaurant_id: dims} — each REAL restaurant's latest row, recent
    enough to describe it now. Server-side only (neighbour search): never
    returned to a client."""
    from .features import iso_week
    from .jobs import seeded_restaurant_ids
    floor = iso_week(date.today() - timedelta(weeks=max_age_weeks))
    conn = get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT d.restaurant_id, d.dims_json FROM intel_dna d JOIN (SELECT restaurant_id, MAX(week) AS week "
            "FROM intel_dna GROUP BY restaurant_id) m ON m.restaurant_id=d.restaurant_id AND m.week=d.week "
            "WHERE d.week >= ?", (floor,)).fetchall()
    finally:
        conn.close()
    seeded = seeded_restaurant_ids(db_path=db_path)
    return {r["restaurant_id"]: json.loads(r["dims_json"] or "{}") for r in rows if r["restaurant_id"] not in seeded}


# ── similarity ──────────────────────────────────────────────────────────────

def distance_detail(a: dict, b: dict, weights: dict = None) -> dict:
    """{comparable, d, shared_weight, shared_structural, why_not}. Gower-
    style over the dimensions BOTH measured: d = sqrt(Σ w δ (z_a − z_b)² ÷
    Σ w δ); a categorical dimension contributes 0 when equal, else
    CATEGORICAL_MISMATCH_Z². A missing dimension is neither 0 nor imputed —
    it is left out, and too much left out means not comparable."""
    weights = weights or STRUCTURAL_WEIGHTS
    total = sum(weights.values())
    num = den = 0.0
    shared_struct = 0
    for dim, w in weights.items():
        ea, eb = (a or {}).get(dim) or {}, (b or {}).get(dim) or {}
        if DIMENSIONS.get(dim, {}).get("kind") == "categorical":
            if ea.get("raw") is None or eb.get("raw") is None:
                continue
            diff2 = 0.0 if ea["raw"] == eb["raw"] else CATEGORICAL_MISMATCH_Z ** 2
        else:
            if ea.get("z") is None or eb.get("z") is None:
                continue
            diff2 = (float(ea["z"]) - float(eb["z"])) ** 2
        num += w * diff2
        den += w
        if dim in STRUCTURAL:
            shared_struct += 1
    share = den / total if total else 0.0
    out = {"shared_weight": round(share, 3), "shared_structural": shared_struct}
    if share < MIN_SHARED_WEIGHT:
        return dict(out, comparable=False, d=None,
                    why_not=f"only {int(round(share * 100))}% of the profile is measured for both")
    if shared_struct < MIN_SHARED_STRUCTURAL:
        return dict(out, comparable=False, d=None,
                    why_not=f"only {shared_struct} structural measures in common (needs {MIN_SHARED_STRUCTURAL})")
    return dict(out, comparable=True, d=round(math.sqrt(num / den), 4), why_not=None)


def distance(a: dict, b: dict, weights: dict = None):
    """The DNA distance between two profiles ({dim: {raw, z, …}}), or None
    when they are not comparable (distance_detail says why)."""
    return distance_detail(a, b, weights)["d"]


def prediction_weights(target_dim: str = None) -> dict:
    """The structural weights plus the target metric's baseline dimension
    at PREDICTION_BASELINE_WEIGHT, renormalised to sum to 1."""
    w = dict(STRUCTURAL_WEIGHTS)
    if target_dim and target_dim in DIMENSIONS and target_dim not in w:
        w[target_dim] = PREDICTION_BASELINE_WEIGHT
    tot = sum(w.values())
    return {k: v / tot for k, v in w.items()}


# ── the owner's read ────────────────────────────────────────────────────────

_UNIT_FMT = {
    "share": lambda v: f"{float(v) * 100:.0f}%",
    "pct": lambda v: f"{float(v):.1f}%",
    "pts": lambda v: f"{float(v):.1f} pts",
    "h": lambda v: f"{float(v):.1f}h",
    "stars": lambda v: f"{float(v):.2f}★",
    "days": lambda v: f"{float(v):.0f} days",
    "count": lambda v: f"{int(v)}",
    "band": lambda v: f"band {int(v)} of 0–4",
    "ratio": lambda v: f"{float(v):.2f}×",
    "skill": lambda v: f"{float(v) * 100:.0f}%",
    "elasticity": lambda v: f"{float(v):.2f}",
    "per100": lambda v: f"{float(v):.1f}",
    "pct_per_week": lambda v: f"{float(v):+.1f}% a week",
}


def display(dim, raw) -> str | None:
    if raw is None:
        return None
    meta = DIMENSIONS.get(dim) or {}
    if meta.get("kind") == "categorical":
        s = str(raw)
        if s.startswith("service:"):
            return s.split(":", 1)[1].replace("_", " ").capitalize()
        try:
            from . import categories
            return categories.LABELS.get(s, s.replace("_", " ").capitalize())
        except Exception:
            return s
    try:
        return _UNIT_FMT.get(meta.get("unit"), lambda v: f"{v}")(raw)
    except (TypeError, ValueError):
        return None


def _row_at_least_weeks_before(restaurant_id, week, weeks, db_path):
    from .features import iso_week
    try:
        y, w = week.split("-W")
        mon = date.fromisocalendar(int(y), int(w), 1)
    except (ValueError, AttributeError):
        return None
    cut = iso_week(mon - timedelta(weeks=weeks))
    conn = get_conn(db_path)
    try:
        r = conn.execute("SELECT week, dims_json FROM intel_dna WHERE restaurant_id=? AND week <= ? "
                         "ORDER BY week DESC LIMIT 1", (restaurant_id, cut)).fetchone()
    finally:
        conn.close()
    return {"week": r["week"], "dims": json.loads(r["dims_json"] or "{}")} if r else None


def profile(restaurant_id, db_path=DB_PATH, modules=None) -> dict:
    """The restaurant's OWN profile: every dimension with its label, value,
    change against TREND_WEEKS ago, how it was measured, and for an
    unmeasured one what would measure it. `modules` (a set of permission-
    module keys, or None for no restriction) leaves out the dimensions of a
    module this login can't view. Never another restaurant's figure, never
    a z-score, never a personality label."""
    row = latest(restaurant_id, db_path=db_path)
    if not row:
        return {"available": False, "why_not": "Your profile builds with the nightly pass — check back tomorrow.",
                "families": []}
    prev = _row_at_least_weeks_before(restaurant_id, row["week"], TREND_WEEKS, db_path) or {"dims": {}, "week": None}
    fams = []
    measured = total = 0
    for fam, fam_label in FAMILIES:
        items = []
        for dim, meta in DIMENSIONS.items():
            if meta["family"] != fam:
                continue
            if modules is not None and meta.get("module") and meta["module"] not in modules:
                continue
            e = row["dims"].get(dim) or {}
            p = (prev["dims"].get(dim) or {})
            raw = e.get("raw")
            item = {"key": dim, "code": meta["code"], "label": meta["label"], "value": raw,
                    "display": display(dim, raw), "unit": meta["unit"], "better": meta.get("better"),
                    "measured": raw is not None, "basis": e.get("basis") or f"needs {meta['needs']}",
                    "n": e.get("n"), "dormant": not meta["buildable"]}
            if raw is None:
                item["needs"] = meta["needs"]
            trend = None
            if raw is not None and p.get("raw") is not None and meta["kind"] == "numeric":
                try:
                    change = float(raw) - float(p["raw"])
                    # A move under a quarter of the stated scale is steady
                    # (in the dimension's own space: log10 for day counts).
                    a, b = _t(dim, raw), _t(dim, p["raw"])
                    direction = ("steady" if abs(a - b) < TREND_STEADY_SCALE * float(meta["anchor"][1])
                                 else ("up" if a > b else "down"))
                    trend = {"weeks": TREND_WEEKS, "since_week": prev["week"], "previous": p["raw"],
                             "previous_display": display(dim, p["raw"]), "change": round(change, 4),
                             "direction": direction}
                except (TypeError, ValueError):
                    trend = None
            item["trend"] = trend
            if meta["buildable"]:
                total += 1
                measured += 1 if raw is not None else 0
            items.append(item)
        if items:
            fams.append({"key": fam, "label": fam_label, "dimensions": items})
    from time_utils import mdy
    return {"available": True, "week": row["week"], "as_of": mdy(str(row.get("computed_at") or "")[:10]) or None,
            "measured": measured, "of": total,
            "coverage_pct": int(round(100.0 * measured / total)) if total else 0,
            "version": row["version"], "families": fams,
            "note": ("Your own figures, measured from your data. Comparisons with similar restaurants switch on "
                     "when enough of them are on Cavnar.")}


def payload_for(user, db_path=DB_PATH) -> dict:
    """The body /api/dna and /mobile/api/dna return: the login's own
    restaurant's profile, projected by its module view permissions."""
    rid = (user or {}).get("restaurant_id")
    if not rid:
        return {"ok": False, "error": "No restaurant on this login."}
    from .engine import _visible_modules
    return {"ok": True, "profile": profile(rid, db_path=db_path, modules=_visible_modules(user))}

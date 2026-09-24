"""Level 3: benchmarks — where a restaurant sits among restaurants like it.

Computed weekly per cohort per metric from the latest feature row of each
restaurant, stored when the cohort clears the floor, and read back as a
band (p25/p50/p75) plus this restaurant's own standing. The restaurant's
own value is shown only to its own owner; the band is what crosses the
tenant line.

What crosses it is held to four rules (Never-Say audit NS4 H4/H5/M5/M6,
NS6 §B findings 1 and 5):

* Quartiles only over MIN_QUARTILE_N other restaurants, the VIEWER'S OWN
  ROW TAKEN OUT (`published(exclude_value=…)`), and rounded to a coarse
  step per metric. Linear-interpolated quartiles of five values sit exactly
  on the 2nd, 3rd and 4th members, so an owner who knows their own figure
  read three peers' exact labor % off the band. Fewer than MIN_QUARTILE_N →
  the band is withheld, never published.
* A band older than MAX_BAND_AGE_WEEKS is not served, and neither is the
  restaurant's own value past MAX_OWN_AGE_WEEKS; every band carries its
  `as_of` (M/D/YY) and every prompt line says it.
* The cohort label comes from the cohort ACTUALLY used: a platform band is
  "All restaurants on Cavnar", never "restaurants like yours".
* A cohort picked from a type Cavnar inferred from the restaurant's name
  carries `inferred`, and the line says so.
"""
import json
import math
from datetime import date, datetime, timedelta

import models as _models_mod
from models import DB_PATH
from . import privacy, categories
from . import features as _features
from .stats import percentile, mean


def get_conn(db_path=None):
    """models.get_conn, resolved at call time (CLAUDE.md, bound imports)."""
    if db_path is None or db_path == DB_PATH:
        return _models_mod.get_conn()
    return _models_mod.get_conn(db_path)


# Other restaurants a published quartile band needs (the viewer excluded).
# At 8+ the interpolated quartiles fall between members for most n, and the
# coarse rounding below covers the n where they do not.
MIN_QUARTILE_N = 8
# A band, or this restaurant's own feature row, older than this is history,
# not a comparison (NS4 H5: a 2025-W39 band was served on 9/24/26).
MAX_BAND_AGE_WEEKS = 8
MAX_OWN_AGE_WEEKS = 8

PLATFORM_LABEL = "All restaurants on Cavnar"

# The published precision per metric: coarse enough that a quartile cannot
# be read back as one member's exact figure.
_STEP = {"avg_rating_30d": 0.1, "labor_pct_28d": 0.5, "labor_pct_sd_28d": 0.5, "food_cost_pct_28d": 0.5,
         "waste_sales_pct_28d": 0.5, "post_lift_median_28d": 0.5, "labor_hours_per_1k_28d": 0.1,
         "labor_hours_per_1k_day_28d": 0.1, "labor_hours_per_1k_night_28d": 0.1,
         "response_24h_rate_30d": 0.05, "reply_rate_30d": 0.05, "campaign_tap_rate_28d": 0.01,
         "post_engagement_rate_28d": 0.01, "outcomes_improved_rate_90d": 0.05}


BETTER = {"avg_rating_30d": "higher", "response_24h_rate_30d": "higher", "reply_rate_30d": "higher",
          "labor_pct_28d": "lower", "labor_pct_sd_28d": "lower", "food_cost_pct_28d": "lower",
          "labor_hours_per_1k_28d": "lower", "labor_hours_per_1k_day_28d": "lower", "labor_hours_per_1k_night_28d": "lower",
          "waste_sales_pct_28d": "lower", "campaign_tap_rate_28d": "higher", "outcomes_improved_rate_90d": "higher",
          "post_lift_median_28d": "higher", "post_engagement_rate_28d": "higher"}

LABELS = {"avg_rating_30d": "Average rating (30d)", "response_24h_rate_30d": "Reviews answered within a day",
          "reply_rate_30d": "Reviews answered", "labor_pct_28d": "Labor %", "labor_pct_sd_28d": "Day-to-day labor swing",
          "labor_hours_per_1k_28d": "Labor hours per $1k of sales", "labor_hours_per_1k_day_28d": "Labor hours per $1k, lunch/day",
          "labor_hours_per_1k_night_28d": "Labor hours per $1k, dinner/night",
          "food_cost_pct_28d": "Food cost %", "waste_sales_pct_28d": "Waste as % of sales",
          "campaign_tap_rate_28d": "Text campaign tap rate", "outcomes_improved_rate_90d": "Recommendations that measurably improved",
          "post_lift_median_28d": "Sales lift after a post (median)", "post_engagement_rate_28d": "Post engagement rate"}


def cohort_label(cohort) -> str:
    """The label of the cohort a band was ACTUALLY read from (NS4 H4)."""
    if not cohort or cohort == "platform":
        return PLATFORM_LABEL
    return f"{categories.label(cohort)} on Cavnar"


def coarse(metric, x):
    """A published statistic at its metric's step (2 significant figures
    for a metric with none)."""
    if x is None:
        return None
    step = _STEP.get(metric)
    if step is None and str(metric).startswith("staff_per_1k."):
        step = 0.05
    x = float(x)
    if step is None:
        if x == 0:
            return 0.0
        mag = 10 ** (math.floor(math.log10(abs(x))) - 1)
        return round(round(x / mag) * mag, 6)
    return round(round(x / step) * step, 6)


def _week_monday(week: str):
    try:
        y, w = str(week).split("-W")
        return date.fromisocalendar(int(y), int(w), 1)
    except Exception:
        return None


def _as_of(row) -> str | None:
    """M/D/YY the band was computed (else the end of its ISO week)."""
    from time_utils import mdy
    raw = (row or {}).get("computed_at")
    if raw:
        try:
            return mdy(datetime.strptime(str(raw)[:10], "%Y-%m-%d").date())
        except Exception:
            pass
    mon = _week_monday((row or {}).get("week"))
    return mdy(mon + timedelta(days=6)) if mon else None


def _week_floor(today: date = None, weeks: int = MAX_BAND_AGE_WEEKS) -> str:
    return _features.iso_week((today or date.today()) - timedelta(weeks=weeks))


def compute(db_path=DB_PATH, cohorts: dict = None, today: date = None) -> dict:
    latest = _features.latest_by_restaurant(db_path=db_path)
    cohorts = cohorts or {}
    week = _features.iso_week(today or date.today())
    groups = {"platform": list(latest.items())}
    for rid, row in latest.items():
        c = cohorts.get(rid)
        if c:
            groups.setdefault(c, []).append((rid, row))
    written = 0
    conn = get_conn(db_path)
    try:
        for cohort, rows in groups.items():
            if not privacy.cohort_ok(len(rows)):
                continue
            # Plus the staffing ratios (staffing.compute_ratios), whose keys
            # depend on which role families the cohort runs.
            staffing = sorted({k for _, r in rows for k in (r["features"] or {}) if k.startswith("staff_per_1k.")})
            for metric in tuple(_features.BENCHMARK_KEYS) + tuple(staffing):
                vals = [r["features"].get(metric) for _, r in rows if r["features"].get(metric) is not None]
                if not privacy.cohort_ok(len(vals)):
                    continue
                # The member values, sorted and unlabelled, stay server-side
                # (vals_json is never selected into a payload): published()
                # needs them to take the viewer's own row out of the band it
                # shows that viewer (NS4 M6).
                vj = json.dumps(sorted(round(float(v), 3) for v in vals))
                conn.execute(
                    "INSERT INTO intel_benchmarks (cohort, metric, week, n, p25, p50, p75, mean, vals_json) "
                    "VALUES (?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(cohort, metric, week) DO UPDATE SET n=excluded.n, p25=excluded.p25, p50=excluded.p50, "
                    "p75=excluded.p75, mean=excluded.mean, vals_json=excluded.vals_json, computed_at=datetime('now')",
                    (cohort, metric, week, len(vals), privacy.round_effect(percentile(vals, 25), 3),
                     privacy.round_effect(percentile(vals, 50), 3), privacy.round_effect(percentile(vals, 75), 3),
                     privacy.round_effect(mean(vals), 3), vj))
                written += 1
        conn.commit()
    finally:
        conn.close()
    return {"written": written, "week": week}


def _row(cohort, metric, db_path=DB_PATH, today: date = None, with_vals=False):
    cols = "cohort, metric, week, n, p25, p50, p75, mean, computed_at" + (", vals_json" if with_vals else "")
    conn = get_conn(db_path)
    try:
        row = conn.execute(f"SELECT {cols} FROM intel_benchmarks WHERE cohort=? AND metric=? AND week >= ? "
                           "ORDER BY week DESC LIMIT 1", (cohort, metric, _week_floor(today))).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def band(cohort: str, metric: str, db_path=DB_PATH, today: date = None) -> dict | None:
    """The latest stored band for a cohort within MAX_BAND_AGE_WEEKS, or
    None. Internal (staffing's starting headcount scales its median); what
    an owner is shown goes through published()."""
    row = _row(cohort, metric, db_path=db_path, today=today)
    if not row:
        return None
    row["as_of"] = _as_of(row)
    return privacy.assert_anonymous(row)


def published(cohort: str, metric: str, exclude_value=None, db_path=DB_PATH, today: date = None) -> dict | None:
    """The band as it may be shown: None when nothing current exists,
    {withheld: True, n, reason} under MIN_QUARTILE_N other restaurants, else
    {cohort, cohort_label, metric, week, as_of, n, p25, p50, p75} over the
    members with one instance of `exclude_value` (the viewer's own figure
    for that week) taken out, each quartile rounded to the metric's step."""
    row = _row(cohort, metric, db_path=db_path, today=today, with_vals=True)
    if not row:
        return None
    try:
        vals = json.loads(row.get("vals_json") or "null")
    except Exception:
        vals = None
    if vals is None:
        # A band stored before its member values were kept: the viewer
        # cannot be taken out of it, so it is never shown to one.
        if exclude_value is not None:
            return {"withheld": True, "n": int(row["n"] or 0),
                    "reason": "this band predates the privacy rules and is recomputed nightly"}
        vals = None
    else:
        vals = sorted(float(v) for v in vals)
        if exclude_value is not None:
            ev = round(float(exclude_value), 3)
            for i, v in enumerate(vals):
                if abs(v - ev) <= 0.0005:
                    del vals[i]
                    break
    n = len(vals) if vals is not None else int(row["n"] or 0)
    if n < MIN_QUARTILE_N:
        return {"withheld": True, "n": n,
                "reason": f"fewer than {MIN_QUARTILE_N} other restaurants have this measured"}
    p25 = percentile(vals, 25) if vals is not None else row["p25"]
    p50 = percentile(vals, 50) if vals is not None else row["p50"]
    p75 = percentile(vals, 75) if vals is not None else row["p75"]
    out = {"cohort": row["cohort"], "cohort_label": cohort_label(row["cohort"]), "metric": metric,
           "week": row["week"], "as_of": _as_of(row), "n": n,
           "p25": coarse(metric, p25), "p50": coarse(metric, p50), "p75": coarse(metric, p75)}
    return privacy.assert_anonymous(out)


def _own(restaurant_id, metric, db_path=DB_PATH, today: date = None, band_week: str = None):
    """(value now, value at the band's week, own week, stale) — this
    restaurant's figure within MAX_OWN_AGE_WEEKS, and the one that went into
    a band computed in `band_week` (the latest row at most three weeks
    before it — latest_by_restaurant's rule)."""
    rows = _features.series(restaurant_id, weeks=20, db_path=db_path)
    floor = _week_floor(today, MAX_OWN_AGE_WEEKS)
    cur = rows[-1] if rows else None
    value, own_week, stale = None, None, False
    if cur:
        own_week = cur["week"]
        if cur["week"] >= floor:
            value = (cur.get("features") or {}).get(metric)
        else:
            stale = True
    at_band = None
    if band_week:
        mon = _week_monday(band_week)
        lo = _features.iso_week(mon - timedelta(weeks=3)) if mon else None
        for r in reversed(rows):
            if r["week"] <= band_week and (lo is None or r["week"] >= lo):
                at_band = (r.get("features") or {}).get(metric)
                break
    return value, at_band, own_week, stale


def benchmark(restaurant_id: int, metric: str, cohort: str = None, db_path=DB_PATH,
              cohort_source: str = None, today: date = None) -> dict:
    """This restaurant against its cohort (falling back to platform-wide).
    {available, value, cohort, cohort_label, inferred, n, p25, p50, p75,
    week, as_of, own_week, standing} — or {available: False, reason}."""
    value = None
    out = None
    used = None
    last_reason = None
    for c in ([cohort] if cohort else []) + ["platform"]:
        row = _row(c, metric, db_path=db_path, today=today)
        v_now, v_at, own_week, own_stale = _own(restaurant_id, metric, db_path=db_path, today=today,
                                                band_week=(row or {}).get("week"))
        value = v_now
        p = published(c, metric, exclude_value=v_at, db_path=db_path, today=today) if row else None
        if p and not p.get("withheld"):
            out, used = p, c
            break
        if p and p.get("withheld"):
            last_reason = p.get("reason")
    if not out:
        return {"available": False, "metric": metric, "value": value,
                "reason": last_reason or (f"no current band from at least {MIN_QUARTILE_N} other restaurants "
                                          "with this measured")}
    inferred = bool(used != "platform" and cohort_source == "inferred")
    res = {"available": True, "metric": metric, "label": LABELS.get(metric, metric), "value": value,
           "cohort": used, "cohort_label": out["cohort_label"], "cohort_source": ("platform" if used == "platform"
                                                                                 else cohort_source),
           "inferred": inferred, "n": out["n"], "p25": out["p25"], "p50": out["p50"], "p75": out["p75"],
           "better": BETTER.get(metric, "higher"), "week": out["week"], "as_of": out["as_of"],
           "own_week": own_week, "own_stale": own_stale}
    if value is None:
        res["standing"] = "unmeasured"
        return res
    better_high = BETTER.get(metric, "higher") == "higher"
    b = out
    if (value >= b["p75"]) if better_high else (value <= b["p25"]):
        res["standing"] = "top quarter"
    elif (value >= b["p50"]) if better_high else (value <= b["p50"]):
        res["standing"] = "above the middle"
    elif (value >= b["p25"]) if better_high else (value <= b["p75"]):
        res["standing"] = "below the middle"
    else:
        res["standing"] = "bottom quarter"
    return res


def all_for(restaurant_id: int, cohort: str = None, db_path=DB_PATH, cohort_source: str = None) -> list:
    return [benchmark(restaurant_id, m, cohort=cohort, db_path=db_path, cohort_source=cohort_source)
            for m in _features.BENCHMARK_KEYS]


def context_line(b) -> str | None:
    """One prompt line for a published band, carrying its cohort, size,
    as-of date and — for an inferred type — that it was inferred."""
    if not b or not b.get("available") or b.get("standing") in (None, "unmeasured"):
        return None
    who = ("other restaurants on Cavnar — all types, not a like-for-like cohort" if b.get("cohort") == "platform"
           else f"other {b['cohort_label']}")
    line = (f"{b['label']}: this restaurant is in the {b['standing']} of {b['n']} {who} "
            f"(band {b['p25']:g}–{b['p75']:g}, middle {b['p50']:g}; band as of {b.get('as_of') or 'unknown'}")
    mon = _week_monday(b.get("own_week"))
    if mon:
        from time_utils import mdy
        line += f"; this restaurant's figure from the week of {mdy(mon)}"
    line += ")"
    if b.get("inferred"):
        line += " — the type was inferred from the restaurant's name, not set by the owner"
    return line + "."


def cohort_table(db_path=DB_PATH) -> list:
    """Every published cohort band, latest week — for the admin page."""
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT b.cohort, b.metric, b.week, b.n, b.p25, b.p50, b.p75, b.mean FROM intel_benchmarks b "
                            "JOIN (SELECT cohort, metric, MAX(week) AS week FROM intel_benchmarks GROUP BY cohort, metric) m "
                            "ON m.cohort=b.cohort AND m.metric=b.metric AND m.week=b.week ORDER BY b.cohort, b.metric").fetchall()
    finally:
        conn.close()
    return [privacy.assert_anonymous({**dict(r), "label": LABELS.get(r["metric"], r["metric"])}) for r in rows]

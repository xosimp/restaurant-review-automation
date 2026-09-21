"""Level 3: benchmarks — where a restaurant sits among restaurants like it.

Computed weekly per cohort per metric from the latest feature row of each
restaurant, published only when the cohort clears the floor, and read
back as a band (p25/p50/p75) plus this restaurant's own percentile. The
restaurant's own value is shown only to its own owner; the band is what
crosses the tenant line, and it is an aggregate over ≥ MIN_COHORT.
"""
from datetime import date

from models import get_conn, DB_PATH
from . import privacy, categories
from . import features as _features
from .stats import percentile, mean

BETTER = {"avg_rating_30d": "higher", "response_24h_rate_30d": "higher", "reply_rate_30d": "higher",
          "labor_pct_28d": "lower", "labor_pct_sd_28d": "lower", "food_cost_pct_28d": "lower",
          "waste_sales_pct_28d": "lower", "campaign_tap_rate_28d": "higher", "outcomes_improved_rate_90d": "higher",
          "post_lift_median_28d": "higher", "post_engagement_rate_28d": "higher"}

LABELS = {"avg_rating_30d": "Average rating (30d)", "response_24h_rate_30d": "Reviews answered within a day",
          "reply_rate_30d": "Reviews answered", "labor_pct_28d": "Labor %", "labor_pct_sd_28d": "Day-to-day labor swing",
          "food_cost_pct_28d": "Food cost %", "waste_sales_pct_28d": "Waste as % of sales",
          "campaign_tap_rate_28d": "Text campaign tap rate", "outcomes_improved_rate_90d": "Recommendations that measurably improved",
          "post_lift_median_28d": "Sales lift after a post (median)", "post_engagement_rate_28d": "Post engagement rate"}


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
            for metric in _features.BENCHMARK_KEYS:
                vals = [r["features"].get(metric) for _, r in rows if r["features"].get(metric) is not None]
                if not privacy.cohort_ok(len(vals)):
                    continue
                conn.execute(
                    "INSERT INTO intel_benchmarks (cohort, metric, week, n, p25, p50, p75, mean) VALUES (?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(cohort, metric, week) DO UPDATE SET n=excluded.n, p25=excluded.p25, p50=excluded.p50, "
                    "p75=excluded.p75, mean=excluded.mean, computed_at=datetime('now')",
                    (cohort, metric, week, len(vals), privacy.round_effect(percentile(vals, 25), 3),
                     privacy.round_effect(percentile(vals, 50), 3), privacy.round_effect(percentile(vals, 75), 3),
                     privacy.round_effect(mean(vals), 3)))
                written += 1
        conn.commit()
    finally:
        conn.close()
    return {"written": written, "week": week}


def band(cohort: str, metric: str, db_path=DB_PATH) -> dict | None:
    """The latest published band for a cohort, or None."""
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT cohort, metric, week, n, p25, p50, p75, mean FROM intel_benchmarks WHERE cohort=? AND metric=? "
                           "ORDER BY week DESC LIMIT 1", (cohort, metric)).fetchone()
    finally:
        conn.close()
    return privacy.assert_anonymous(dict(row)) if row else None


def benchmark(restaurant_id: int, metric: str, cohort: str = None, db_path=DB_PATH) -> dict:
    """This restaurant against its cohort (falling back to platform-wide).
    {available, value, cohort, cohort_label, n, p25, p50, p75, percentile, standing}."""
    own = _features.latest(restaurant_id, db_path=db_path)
    value = (own or {}).get("features", {}).get(metric)
    b = band(cohort, metric, db_path=db_path) if cohort else None
    used = cohort
    if not b:
        b = band("platform", metric, db_path=db_path)
        used = "platform"
    if not b:
        return {"available": False, "metric": metric, "value": value,
                "reason": f"fewer than {privacy.MIN_COHORT} similar restaurants have this measured yet"}
    out = {"available": True, "metric": metric, "label": LABELS.get(metric, metric), "value": value,
           "cohort": used, "cohort_label": categories.label(None) if used == "platform" else categories.label(used),
           "n": b["n"], "p25": b["p25"], "p50": b["p50"], "p75": b["p75"], "better": BETTER.get(metric, "higher"),
           "week": b["week"]}
    if value is None:
        out["standing"] = "unmeasured"
        return out
    better_high = BETTER.get(metric, "higher") == "higher"
    if (value >= b["p75"]) if better_high else (value <= b["p25"]):
        out["standing"] = "top quarter"
    elif (value >= b["p50"]) if better_high else (value <= b["p50"]):
        out["standing"] = "above the middle"
    elif (value >= b["p25"]) if better_high else (value <= b["p75"]):
        out["standing"] = "below the middle"
    else:
        out["standing"] = "bottom quarter"
    return out


def all_for(restaurant_id: int, cohort: str = None, db_path=DB_PATH) -> list:
    return [benchmark(restaurant_id, m, cohort=cohort, db_path=db_path) for m in _features.BENCHMARK_KEYS]


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

"""Trend detection: how a cohort's weekly median of a metric is moving.

A slope over at least MIN_WEEKS_FOR_TREND weekly points, each point an
aggregate over ≥ MIN_COHORT restaurants. Emerging trends are the steepest
relative movers. Nothing here names a restaurant.
"""
from models import DB_PATH
from . import privacy, categories
from . import features as _features
from .stats import percentile, slope


def cohort_series(weeks: int = 8, cohorts: dict = None, db_path=DB_PATH) -> dict:
    """{cohort: {metric: [{week, n, p50}]}} — only weeks that clear the floor."""
    by_week = _features.weekly_by_restaurant(weeks=weeks, db_path=db_path)
    cohorts = cohorts or {}
    out = {}
    for week in sorted(by_week):
        rows = by_week[week]
        groups = {"platform": list(rows.items())}
        for rid, f in rows.items():
            c = cohorts.get(rid)
            if c:
                groups.setdefault(c, []).append((rid, f))
        for cohort, items in groups.items():
            for metric in _features.BENCHMARK_KEYS:
                vals = [f.get(metric) for _, f in items if f.get(metric) is not None]
                if not privacy.cohort_ok(len(vals)):
                    continue
                out.setdefault(cohort, {}).setdefault(metric, []).append(
                    {"week": week, "n": len(vals), "p50": privacy.round_effect(percentile(vals, 50), 3)})
    return out


def platform_trends(weeks: int = 8, cohorts: dict = None, db_path=DB_PATH) -> list:
    series = cohort_series(weeks=weeks, cohorts=cohorts, db_path=db_path)
    out = []
    for cohort, metrics in series.items():
        for metric, pts in metrics.items():
            if len(pts) < privacy.MIN_WEEKS_FOR_TREND:
                continue
            ys = [p["p50"] for p in pts]
            s = slope(ys)
            if s is None:
                continue
            base = ys[0] or None
            out.append(privacy.assert_anonymous({
                "cohort": cohort, "cohort_label": "All restaurants" if cohort == "platform" else categories.label(cohort),
                "metric": metric, "weeks": len(pts), "n_latest": pts[-1]["n"], "from": ys[0], "to": ys[-1],
                "slope_per_week": privacy.round_effect(s, 4),
                "relative_per_week": privacy.round_effect(s / base, 4) if base else None,
                "series": pts}))
    out.sort(key=lambda t: abs(t["relative_per_week"] or 0), reverse=True)
    return out


def emerging(limit: int = 6, cohorts: dict = None, db_path=DB_PATH) -> list:
    return [t for t in platform_trends(cohorts=cohorts, db_path=db_path) if t["relative_per_week"]][:limit]

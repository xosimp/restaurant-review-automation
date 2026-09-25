"""Trend detection: how a cohort's weekly median of a metric is moving.

A slope over at least MIN_WEEKS_FOR_TREND weekly points, each point a
Harrell–Davis median over at least benchmarks.MIN_QUARTILE_N eligible
restaurants from privacy.MIN_ORGS organisations — the bands' publishing
gate (fix round #41). Emerging trends are the steepest relative movers.
Nothing here names a restaurant; trends are an admin read.

Over a BALANCED PANEL (Benchmarking audit #44, BM2-7): a cohort's median
moved when two lean pizzerias signed up, and "labor % is falling across
pizza" was membership, not behaviour. A series is computed only over the
restaurants present in at least PANEL_MIN_PRESENT of the PANEL_WEEKS weeks,
and says how many joined and left over the window. The learning pass
persists each week's point (`persist` → intel_cohort_series), so a trend is
what was measured then. Every point is rounded as published (the metric's
coarse step, #47).
"""
from datetime import date

import models as _models_mod
from models import DB_PATH
from . import privacy, categories
from . import features as _features
from . import metrics_registry as _reg
from .stats import harrell_davis, slope

PANEL_WEEKS = 8
PANEL_MIN_PRESENT = 6


def get_conn(db_path=None):
    """models.get_conn, resolved at call time (CLAUDE.md, bound imports)."""
    if db_path is None or db_path == DB_PATH:
        return _models_mod.get_conn()
    return _models_mod.get_conn(db_path)


def _groups_for(week_rows, cohorts, metric):
    """{cohort: [(rid, features)]} for one week and metric: every
    restaurant's type or partition key for this metric's family (a behaviour
    metric also gets the all-restaurants group)."""
    from .benchmarks import _key_for, publishable_group
    fam = _reg.partition_family(metric)
    groups = {}
    if _reg.platform_allowed(metric):
        groups["platform"] = list(week_rows.items())
    for rid, f in week_rows.items():
        key = _key_for((cohorts or {}).get(rid), "labor" if fam == "staff" else fam)
        if key and key != "platform" and publishable_group(key):
            groups.setdefault(key, []).append((rid, f))
    return groups


def panel_series(weeks: int = PANEL_WEEKS, cohorts: dict = None, db_path=DB_PATH, members: dict = None) -> dict:
    """{cohort: {metric: {"points": [{week, n, p50}], "n_panel", "n_joined",
    "n_left"}}} — each point the median over the balanced panel present that
    week, only weeks that clear the floor.

    Through the bands' publishing gate (fix round #41, R1-10): members must
    be eligible (jobs.ineligible — live weeks, completeness, excluded and
    lapsed accounts; the $26 default wage out of labor-cost metrics), a point
    needs benchmarks.MIN_QUARTILE_N restaurants from privacy.MIN_ORGS
    organisations with no organisation over a third (held to a third, as a
    band is), and the median is Harrell–Davis at the metric's coarse step —
    never one member's exact figure. Trends have no viewer, so they are an
    admin read (intelligence.dashboard); an owner surface must go through
    benchmarks.published()."""
    from .benchmarks import coarse, cap_organisations, labor_sourced, MIN_QUARTILE_N
    from .jobs import member_info, ineligible
    by_week = _features.weekly_by_restaurant(weeks=weeks, db_path=db_path)
    order = sorted(by_week)
    if not order:
        return {}
    if members is None:
        try:
            members = member_info(db_path=db_path)
        except Exception:
            members = {}
    members = members or {}

    def org(rid):
        return (members.get(rid) or {}).get("org_hash") or privacy.org_hash(f"r{rid}")

    need = min(PANEL_MIN_PRESENT, len(order))
    out = {}
    for metric in _features.BENCHMARK_KEYS:
        present = {}          # cohort -> rid -> {week: value}
        for week in order:
            for cohort, items in _groups_for(by_week[week], cohorts, metric).items():
                for rid, f in items:
                    v = f.get(metric)
                    m = members.get(rid)
                    if v is None or (m and ineligible(m, {"completeness": None})):
                        continue
                    if m and metric in _reg.LABOR_COST_METRICS and not labor_sourced(m):
                        continue
                    present.setdefault(cohort, {}).setdefault(rid, {})[week] = float(v)
        for cohort, rids in present.items():
            panel = {rid: wk for rid, wk in rids.items() if len(wk) >= need}
            first, last = order[0], order[-1]
            joined = sum(1 for wk in rids.values() if last in wk and first not in wk)
            left = sum(1 for wk in rids.values() if first in wk and last not in wk)
            pts = []
            for week in order:
                pairs = [(wk[week], org(rid)) for rid, wk in panel.items() if week in wk]
                pairs, _dropped = cap_organisations(pairs, week)
                orgs, _share = privacy.org_counts([o for _v, o in pairs])
                if len(pairs) < MIN_QUARTILE_N or orgs < privacy.MIN_ORGS:
                    continue
                vals = [v for v, _o in pairs]
                pts.append({"week": week, "n": len(vals), "p50": coarse(metric, harrell_davis(vals, 50))})
            if pts:
                out.setdefault(cohort, {})[metric] = {"points": pts, "n_panel": len(panel),
                                                      "n_joined": joined, "n_left": left}
    return out


def cohort_series(weeks: int = 8, cohorts: dict = None, db_path=DB_PATH) -> dict:
    """{cohort: {metric: [{week, n, p50}]}} over the balanced panel — only
    weeks that clear the floor."""
    return {c: {m: s["points"] for m, s in ms.items()}
            for c, ms in panel_series(weeks=weeks, cohorts=cohorts, db_path=db_path).items()}


def platform_trends(weeks: int = 8, cohorts: dict = None, db_path=DB_PATH) -> list:
    series = panel_series(weeks=weeks, cohorts=cohorts, db_path=db_path)
    out = []
    for cohort, metrics in series.items():
        for metric, s in metrics.items():
            pts = s["points"]
            if len(pts) < privacy.MIN_WEEKS_FOR_TREND:
                continue
            ys = [p["p50"] for p in pts]
            sl = slope(ys)
            if sl is None:
                continue
            base = ys[0] or None
            from .benchmarks import cohort_label
            out.append(privacy.assert_anonymous({
                "cohort": cohort, "cohort_label": cohort_label(cohort) if cohort != "platform" else "All restaurants on Cavnar AI",
                "metric": metric, "weeks": len(pts), "n_latest": pts[-1]["n"], "from": ys[0], "to": ys[-1],
                "slope_per_week": privacy.round_effect(sl, 4),
                "relative_per_week": privacy.round_effect(sl / base, 4) if base else None,
                "n_panel": s["n_panel"], "n_joined": s["n_joined"], "n_left": s["n_left"],
                "series": pts}))
    out.sort(key=lambda t: abs(t["relative_per_week"] or 0), reverse=True)
    return out


def emerging(limit: int = 6, cohorts: dict = None, db_path=DB_PATH) -> list:
    return [t for t in platform_trends(cohorts=cohorts, db_path=db_path) if t["relative_per_week"]][:limit]


def persist(cohorts: dict = None, members: dict = None, db_path=DB_PATH, today: date = None) -> dict:
    """Write the balanced-panel series into intel_cohort_series (the
    learning pass, nightly): one row per cohort, metric and week, the
    window's joined/left on each. `members` (jobs.member_info) decides
    eligibility and counts organisations, as for a band (#41)."""
    series = panel_series(cohorts=cohorts, db_path=db_path, members=members)
    written = 0
    conn = get_conn(db_path)
    try:
        for cohort, metrics in series.items():
            for metric, s in metrics.items():
                for p in s["points"]:
                    conn.execute(
                        "INSERT INTO intel_cohort_series (cohort, metric, week, n, p50, n_joined, n_left) "
                        "VALUES (?,?,?,?,?,?,?) ON CONFLICT(cohort, metric, week) DO UPDATE SET n=excluded.n, "
                        "p50=excluded.p50, n_joined=excluded.n_joined, n_left=excluded.n_left, "
                        "computed_at=datetime('now')",
                        (cohort, metric, p["week"], p["n"], p["p50"], s["n_joined"], s["n_left"]))
                    written += 1
        conn.commit()
    finally:
        conn.close()
    return {"written": written}

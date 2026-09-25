"""The admin Intelligence page, in one payload.

Everything cross-restaurant here passes `privacy.assert_anonymous` before
it leaves; the one place a restaurant is named is the per-category count,
which is a count. Savings are the four value_delivered figures summed
across restaurants figure by figure — never into each other.
"""
from datetime import date, timedelta

import models as _models_mod
from models import DB_PATH
from . import privacy, categories, patterns, benchmarks, trends, scoring, jobs
from . import features as _features


def get_conn(db_path=None):
    """models.get_conn, resolved at call time (CLAUDE.md, bound imports)."""
    if db_path is None or db_path == DB_PATH:
        return _models_mod.get_conn()
    return _models_mod.get_conn(db_path)


def _savings(restaurants, db_path):
    import value_delivered
    totals = {"delivered": 0.0, "avoided": 0.0, "surfaced": 0.0, "opportunity": 0.0}
    counted = 0
    for r in restaurants:
        try:
            d = value_delivered.delivered(r.id, db_path=db_path)
            a = value_delivered.avoided(r.id, db_path=db_path)
            s = value_delivered.surfaced(r.id, db_path=db_path)
            o = value_delivered.opportunity(r.id, db_path=db_path)
        except Exception:
            continue
        counted += 1
        for k, blob in (("delivered", d), ("avoided", a), ("surfaced", s), ("opportunity", o)):
            v = blob.get("monthly") if isinstance(blob, dict) else None
            if v is None and isinstance(blob, dict):
                v = blob.get("total") or blob.get("dollars_monthly") or blob.get("amount")
            try:
                totals[k] += float(v or 0)
            except (TypeError, ValueError):
                pass
    return {"restaurants_counted": counted, **{k: round(v) for k, v in totals.items()},
            "note": "Four separate measured figures summed across restaurants; never summed into each other."}


def build(db_path=DB_PATH, today: date = None) -> dict:
    today = today or date.today()
    rs = jobs.active_restaurants(db_path)
    cohorts = jobs.cohorts_for(rs)
    by_cat = {}
    inferred = 0
    for r in rs:
        c, src = categories.category_for(r)
        by_cat[c or "uncategorised"] = by_cat.get(c or "uncategorised", 0) + 1
        inferred += 1 if src == "inferred" else 0
    latest = _features.latest_by_restaurant(db_path=db_path)
    conn = get_conn(db_path)
    try:
        weeks = conn.execute("SELECT week, COUNT(*) AS n, ROUND(AVG(completeness),3) AS completeness FROM intel_features "
                             "GROUP BY week ORDER BY week DESC LIMIT 12").fetchall()
        conf = conn.execute("SELECT week, cohort, rec_kind, n, mean_confidence, acceptance_rate, success_rate FROM intel_confidence_log "
                            "WHERE cohort='platform' ORDER BY week DESC, n DESC LIMIT 120").fetchall()
        new_patterns = conn.execute("SELECT COUNT(*) FROM intel_patterns WHERE status='active' AND first_seen >= ?",
                                    ((today - timedelta(days=7)).isoformat(),)).fetchone()[0]
        runs = conn.execute("SELECT job, started_at, ok, duration_ms FROM job_runs WHERE job IN ('intelligence_features','intelligence_learning') "
                            "ORDER BY started_at DESC LIMIT 6").fetchall() if _features._has_col(conn, "job_runs", "job") else []
    finally:
        conn.close()
    totals = scoring.platform_totals(db_path=db_path)
    payload = {
        "ok": True,
        "floor": privacy.MIN_COHORT,
        "learning": {
            "restaurants": len(rs), "with_features": len(latest), "cohorts": {k: v for k, v in sorted(by_cat.items(), key=lambda kv: -kv[1])},
            "cohort_labels": {k: categories.label(k) for k in by_cat if k != "uncategorised"},
            "inferred_categories": inferred,
            "weeks": [dict(w) for w in reversed(weeks)],
            "cohorts_at_floor": sorted(c for c, n in by_cat.items() if c != "uncategorised" and privacy.cohort_ok(n)),
        },
        "patterns": {"new_this_week": int(new_patterns), "rows": patterns.all_patterns(db_path=db_path, limit=60)},
        "recommendations": {
            "totals": {k: v for k, v in totals.items() if k not in ("reason",)},
            "by_kind": scoring.rank_kinds(db_path=db_path, limit=30),
        },
        "top_insights": patterns.active(db_path=db_path, limit=8, projection="admin", all_cohorts=True),
        "emerging": trends.emerging(cohorts=cohorts, db_path=db_path),
        "benchmarks": benchmarks.cohort_table(db_path=db_path),
        "confidence_over_time": [dict(c) for c in reversed(conf)],
        "savings": _savings(rs, db_path),
        "jobs": [dict(r) for r in runs],
        "computed_at": today.isoformat(),
    }
    # The whole payload is cross-restaurant: assert it as a unit.
    return privacy.assert_anonymous(payload)

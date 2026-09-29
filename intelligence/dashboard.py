"""The admin Intelligence page, in one payload.

Everything cross-restaurant here passes `privacy.assert_anonymous` before
it leaves; the one place a restaurant is named is the per-category count,
which is a count. Savings are the four value_delivered figures summed
across restaurants figure by figure — never into each other.

The four figures are computed nightly per restaurant (snapshot_value_figures,
a bounded, resumable sweep into value_figures_daily) and summed from those
rows here; computing them live ran every restaurant's labor and inventory
analysis on the request thread — 42 s at 1,000 restaurants (#57). The whole
payload is cached for BUILD_TTL_SECONDS on request threads.
"""
import threading
import time
from datetime import date, datetime, timedelta, timezone

import models as _models_mod
from models import DB_PATH
from . import privacy, categories, patterns, benchmarks, trends, scoring, jobs
from . import features as _features


def get_conn(db_path=None):
    """models.get_conn, resolved at call time (CLAUDE.md, bound imports)."""
    if db_path is None or db_path == DB_PATH:
        return _models_mod.get_conn()
    return _models_mod.get_conn(db_path)


# Each value_delivered figure's headline key and the period it covers. The
# old reader took `monthly`, then `total` / `dollars_monthly` / `amount`, so
# avoided() and surfaced() — which report `dollars` — always summed to $0.
SAVINGS_KEYS = {"delivered": "monthly", "avoided": "dollars", "surfaced": "dollars", "opportunity": "monthly"}
SAVINGS_PERIODS = {"delivered": "per month, measured before and after",
                   "avoided": "to date, at the stated rates",
                   "surfaced": "the last 30 days of alerts",
                   "opportunity": "per month, against target"}
# A restaurant's stored figures older than this are not summed.
VALUE_SNAPSHOT_MAX_AGE_DAYS = 2
# With no nightly rows yet, a fleet this small is computed live.
LIVE_SAVINGS_MAX = 25
VALUE_SWEEP_MAX_SECONDS = 45 * 60
VALUE_SWEEP_CURSOR = "value_figures"
BUILD_TTL_SECONDS = 3600

_build_cache = {}
_build_lock = threading.Lock()


def _operator_today():
    from time_utils import restaurant_now
    return restaurant_now(None).date()


def _figures(restaurant_id, db_path):
    """{delivered, avoided, surfaced, opportunity} headline dollars for one
    restaurant — each read by its OWN key (SAVINGS_KEYS)."""
    import value_delivered
    blobs = {"delivered": value_delivered.delivered(restaurant_id, db_path=db_path),
             "avoided": value_delivered.avoided(restaurant_id, db_path=db_path),
             "surfaced": value_delivered.surfaced(restaurant_id, db_path=db_path),
             "opportunity": value_delivered.opportunity(restaurant_id, db_path=db_path)}
    out = {}
    for k, blob in blobs.items():
        try:
            out[k] = float((blob or {}).get(SAVINGS_KEYS[k]) or 0)
        except (TypeError, ValueError):
            out[k] = 0.0
    return out


def _stored_figures(ids, db_path):
    """{rid: row} — each restaurant's newest value_figures_daily row within
    VALUE_SNAPSHOT_MAX_AGE_DAYS. {} when the table does not exist yet."""
    if not ids:
        return {}
    floor = (_operator_today() - timedelta(days=VALUE_SNAPSHOT_MAX_AGE_DAYS)).isoformat()
    conn = get_conn(db_path)
    out = {}
    try:
        ids = sorted(ids)
        for n in range(0, len(ids), 400):
            chunk = ids[n:n + 400]
            marks = ",".join("?" for _ in chunk)
            try:
                rows = conn.execute(
                    "SELECT v.* FROM value_figures_daily v JOIN (SELECT restaurant_id, MAX(date) AS date "
                    f"FROM value_figures_daily WHERE date >= ? AND restaurant_id IN ({marks}) GROUP BY restaurant_id) m "
                    "ON m.restaurant_id = v.restaurant_id AND m.date = v.date", (floor, *chunk)).fetchall()
            except Exception:
                return {}
            for r in rows:
                out[r["restaurant_id"]] = dict(r)
    finally:
        conn.close()
    return out


def _savings(restaurants, db_path):
    """The four figures summed across restaurants, figure by figure — from
    the nightly rows; computed live only for a small fleet with none yet."""
    ids = [r.id for r in restaurants]
    stored = _stored_figures(ids, db_path)
    totals = {"delivered": 0.0, "avoided": 0.0, "surfaced": 0.0, "opportunity": 0.0}
    col = {"delivered": "delivered_monthly", "avoided": "avoided_to_date", "surfaced": "surfaced_30d",
           "opportunity": "opportunity_monthly"}
    if stored:
        for row in stored.values():
            for k in totals:
                totals[k] += float(row.get(col[k]) or 0)
        counted, source = len(stored), "nightly"
        as_of = min(r["date"] for r in stored.values())
    elif len(ids) <= LIVE_SAVINGS_MAX:
        counted = 0
        for rid in ids:
            try:
                f = _figures(rid, db_path)
            except Exception:
                continue
            counted += 1
            for k in totals:
                totals[k] += f[k]
        source, as_of = "live", _operator_today().isoformat()
    else:
        return {"restaurants_counted": 0, "restaurants_active": len(ids), "available": False,
                "delivered": None, "avoided": None, "surfaced": None, "opportunity": None,
                "periods": SAVINGS_PERIODS, "source": None, "as_of": None,
                "note": "Not computed yet: the nightly value pass (snapshot_value_figures) has not run."}
    return {"restaurants_counted": counted, "restaurants_active": len(ids), "available": True,
            **{k: round(v) for k, v in totals.items()}, "periods": SAVINGS_PERIODS, "source": source, "as_of": as_of,
            "note": "Four separate measured figures summed across restaurants; never summed into each other."}


def snapshot_value_figures(db_path=DB_PATH, max_seconds=None):
    """The nightly pass behind the Intelligence value totals: every active
    real restaurant's four figures into value_figures_daily, one row per
    restaurant per operator day, through scheduler.resumable_sweep — a
    wall-clock bound and a cursor, so a long fleet resumes where it stopped.
    Returns the sweep counts ops.run_outcome reads. Scheduled by the
    integration wave (nightly, after outcome_evaluations)."""
    import scheduler
    import ops
    rs = jobs.active_restaurants(db_path)
    day = _operator_today().isoformat()
    counts = {"ok": 0, "failed": 0}

    def one(rid):
        try:
            f = _figures(rid, db_path)
            conn = get_conn(db_path)
            try:
                conn.execute("INSERT OR REPLACE INTO value_figures_daily (restaurant_id, date, delivered_monthly, "
                             "avoided_to_date, surfaced_30d, opportunity_monthly, computed_at) VALUES (?,?,?,?,?,?,?)",
                             (rid, day, f["delivered"], f["avoided"], f["surfaced"], f["opportunity"],
                              datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")))
                conn.commit()
            finally:
                conn.close()
            counts["ok"] += 1
        except Exception as e:
            counts["failed"] += 1
            ops.capture(e, job="value_figures", context=f"restaurant_id={rid}")

    done, hit = scheduler.resumable_sweep(VALUE_SWEEP_CURSOR, [r.id for r in rs], one,
                                          max_seconds or VALUE_SWEEP_MAX_SECONDS, workers=1, job="value_figures")
    return {"attempted": counts["ok"] + counts["failed"], "ok": counts["ok"], "failed": counts["failed"],
            "skipped": max(0, len(rs) - counts["ok"] - counts["failed"]), "hit_bound": bool(hit), "date": day}


def _in_request():
    try:
        from flask import has_request_context
        return has_request_context()
    except Exception:
        return False


def invalidate():
    with _build_lock:
        _build_cache.clear()


def build(db_path=DB_PATH, today: date = None, fresh: bool = False) -> dict:
    """The payload, cached for BUILD_TTL_SECONDS on a request thread (off
    one — the scheduler, tests — it is always built fresh)."""
    key = (str(db_path), id(get_conn), (today or date.today()).isoformat())
    if _in_request() and not fresh:
        with _build_lock:
            hit = _build_cache.get(key)
        if hit and time.monotonic() - hit[0] < BUILD_TTL_SECONDS:
            return dict(hit[1], cached=True, age_seconds=round(time.monotonic() - hit[0]))
    payload = _build(db_path, today)
    if _in_request():
        with _build_lock:
            _build_cache.clear()
            _build_cache[key] = (time.monotonic(), payload)
    return dict(payload, cached=False, age_seconds=0)


def _build(db_path=DB_PATH, today: date = None) -> dict:
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
        weeks = conn.execute("SELECT week, COUNT(*) AS n, ROUND(AVG(completeness),3) AS completeness, "
                             "SUM(backfilled) AS backfilled FROM intel_features WHERE version = ? "
                             "GROUP BY week ORDER BY week DESC LIMIT 12", (_features.FEATURES_VERSION,)).fetchall()
        conf = conn.execute("SELECT week, cohort, rec_kind, n, mean_confidence, acceptance_rate, success_rate FROM intel_confidence_log "
                            "WHERE cohort='platform' ORDER BY week DESC, n DESC LIMIT 120").fetchall()
        new_patterns = conn.execute("SELECT COUNT(*) FROM intel_patterns WHERE status='active' AND first_seen >= ?",
                                    ((today - timedelta(days=7)).isoformat(),)).fetchone()[0]
        runs = conn.execute("SELECT job, started_at, ok, duration_ms FROM job_runs WHERE job IN ('intelligence_features','intelligence_learning','value_figures') "
                            "ORDER BY started_at DESC LIMIT 9").fetchall() if _features._has_col(conn, "job_runs", "job") else []
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

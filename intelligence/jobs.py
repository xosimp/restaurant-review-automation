"""The two nightly passes.

`run_features` walks every active restaurant and writes this ISO week's
feature row — bounded by a wall clock and resumed from a cursor in
`job_cursors`, so a pass that runs out of time tonight picks up where it
stopped tomorrow rather than starving the same tail every night
(run_daily_fetch's rule).

`run_learning` reads only the materialized tables: feedback sync, pattern
discovery, benchmarks, trends, the confidence log.
"""
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date

import models as _models_mod
from models import DB_PATH, get_all_restaurants
from . import categories, feedback, patterns, benchmarks, scoring
from . import features as _features


def get_conn(db_path=None):
    """models.get_conn, resolved at call time (CLAUDE.md, bound imports)."""
    if db_path is None or db_path == DB_PATH:
        return _models_mod.get_conn()
    return _models_mod.get_conn(db_path)


CURSOR_KEY = "intelligence_features"
FEATURE_WALL_SECONDS = 240
FEATURE_WORKERS = 4

_LIVE = ("trial", "active")


def _cursor_get(db_path):
    conn = get_conn(db_path)
    try:
        # job_cursors is created by models.init_db (one owner, one definition).
        row = conn.execute("SELECT value FROM job_cursors WHERE key=?", (CURSOR_KEY,)).fetchone()
    finally:
        conn.close()
    try:
        return int(row["value"]) if row and row["value"] else 0
    except (TypeError, ValueError):
        return 0


def _cursor_set(db_path, value):
    conn = get_conn(db_path)
    try:
        conn.execute("INSERT INTO job_cursors (key, value, updated_at) VALUES (?,?,datetime('now')) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=datetime('now')",
                     (CURSOR_KEY, str(int(value))))
        conn.commit()
    finally:
        conn.close()


# After is_demo is turned off, the seeded history is still in the tables
# (never hard-deleted). The longest window a feature reads is 90 days
# (features.compute), so the restaurant stays out of cross-restaurant
# learning until that window holds none of it.
SEEDED_HISTORY_DAYS = 90

# The one predicate (one parameter: f"-{SEEDED_HISTORY_DAYS} days"), so SQL
# readers such as scoring.kind_stats filter exactly as real_restaurant_ids.
REAL_RESTAURANT_SQL = ("SELECT id FROM restaurants WHERE COALESCE(is_demo,0)=0 AND "
                       "(demo_cleared_at IS NULL OR demo_cleared_at < datetime('now', ?))")
# Its complement over the restaurants table — what readers of other tables
# exclude (a row whose restaurant is not in the table is left alone).
SEEDED_RESTAURANT_SQL = ("SELECT id FROM restaurants WHERE COALESCE(is_demo,0)=1 OR "
                         "(demo_cleared_at IS NOT NULL AND demo_cleared_at >= datetime('now', ?))")


def seeded_restaurant_ids(db_path=DB_PATH) -> set:
    """The complement of real_restaurant_ids within the restaurants table:
    demo accounts and recently de-flagged ones. Readers of the feature and
    event tables drop these ids."""
    conn = get_conn(db_path)
    try:
        try:
            rows = conn.execute(SEEDED_RESTAURANT_SQL, (f"-{SEEDED_HISTORY_DAYS} days",)).fetchall()
        except Exception:
            rows = conn.execute("SELECT id FROM restaurants WHERE COALESCE(is_demo,0)=1").fetchall()
    finally:
        conn.close()
    return {int(r["id"]) for r in rows}


def real_restaurant_ids(db_path=DB_PATH) -> set:
    """Ids of restaurants whose data may feed CROSS-restaurant learning —
    benchmarks, patterns, trends, cohort and platform rates, the privacy
    floor's count (CA3 F7). Excluded: is_demo=1 accounts (seeded, synthetic),
    and a restaurant whose is_demo flag was turned off less than
    SEEDED_HISTORY_DAYS ago (restaurants.demo_cleared_at), because its
    windows still hold the seeded rows. A restaurant's OWN screens are not
    filtered by this — only what it contributes to everyone else's."""
    conn = get_conn(db_path)
    try:
        try:
            rows = conn.execute(REAL_RESTAURANT_SQL, (f"-{SEEDED_HISTORY_DAYS} days",)).fetchall()
        except Exception:
            rows = conn.execute("SELECT id FROM restaurants WHERE COALESCE(is_demo,0)=0").fetchall()
    finally:
        conn.close()
    return {int(r["id"]) for r in rows}


def active_restaurants(db_path=DB_PATH, include_demo=False) -> list:
    """Restaurants in service, for the nightly passes. Cross-restaurant
    learning (cohorts, patterns, benchmarks, the confidence log) takes the
    default — real restaurants only (real_restaurant_ids). run_features
    passes include_demo=True: a demo account's OWN feature row still backs
    its own screens; the cross-restaurant readers leave it out."""
    live = [r for r in get_all_restaurants(db_path)
            if (getattr(r, "billing_status", None) or "trial").lower() in _LIVE]
    if include_demo:
        return live
    seeded = seeded_restaurant_ids(db_path)
    return [r for r in live if r.id not in seeded]


def cohorts_for(restaurants) -> dict:
    """{restaurant_id: category or None} — the cohort map every cross-
    restaurant pass takes, so a category is resolved once per night."""
    return {r.id: categories.category_for(r)[0] for r in restaurants}


def run_features(db_path=DB_PATH, today: date = None, wall_seconds=FEATURE_WALL_SECONDS, workers=FEATURE_WORKERS) -> dict:
    today = today or date.today()
    rs = sorted(active_restaurants(db_path, include_demo=True), key=lambda r: r.id)
    if not rs:
        return {"computed": 0, "skipped": 0, "resumed_at": 0, "complete": True}
    start_after = _cursor_get(db_path)
    order = [r for r in rs if r.id > start_after] + [r for r in rs if r.id <= start_after]
    deadline = time.monotonic() + wall_seconds
    computed = failed = 0
    last_done = start_after
    stopped_early = False

    def one(r):
        try:
            _features.compute_and_store(r.id, today=today, db_path=db_path)
            return r.id, None
        except Exception as e:      # one restaurant's failure never stops the pass
            return r.id, e

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for i in range(0, len(order), workers):
            # The first batch always runs: a pass that never computes anything
            # would never move the cursor, and the same tail would starve.
            if i > 0 and time.monotonic() > deadline:
                stopped_early = True
                break
            batch = order[i:i + workers]
            for rid, err in pool.map(one, batch):
                if err is None:
                    computed += 1
                else:
                    failed += 1
                    try:
                        import ops
                        ops.capture(err, job="intelligence_features", context=f"restaurant_id={rid}")
                    except Exception:
                        pass
                last_done = rid
    # A completed sweep resets the cursor so the next night starts at the top.
    _cursor_set(db_path, last_done if stopped_early else 0)
    return {"computed": computed, "failed": failed, "resumed_at": start_after, "complete": not stopped_early,
            "week": _features.iso_week(today)}


def run_learning(db_path=DB_PATH, today: date = None) -> dict:
    today = today or date.today()
    rs = active_restaurants(db_path)
    cohorts = cohorts_for(rs)
    out = {"restaurants": len(rs), "cohorts": len({c for c in cohorts.values() if c})}
    out["feedback"] = feedback.sync(db_path=db_path, cohorts=cohorts)
    out["patterns"] = patterns.discover(db_path=db_path, cohorts=cohorts, today=today)
    out["benchmarks"] = benchmarks.compute(db_path=db_path, cohorts=cohorts, today=today)
    out["confidence_log"] = log_confidence(db_path=db_path, cohorts=cohorts, today=today)
    return out


def log_confidence(db_path=DB_PATH, cohorts: dict = None, today: date = None) -> dict:
    """The week's acceptance and success by kind, per cohort and platform-
    wide, so the dashboard can draw model confidence over time."""
    week = _features.iso_week(today or date.today())
    cohorts = cohorts or {}
    written = 0
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT restaurant_id, source_key, rec_kind, action, outcome, confidence_at, days_to_effect "
                            "FROM intel_rec_events").fetchall()
    finally:
        conn.close()
    groups = {}
    seeded = seeded_restaurant_ids(db_path)      # demo accounts never in a platform rate (CA3 F7)
    for r in rows:
        if r["restaurant_id"] in seeded:
            continue
        groups.setdefault(("platform", r["rec_kind"]), []).append(r)
        c = cohorts.get(r["restaurant_id"])
        if c:
            groups.setdefault((c, r["rec_kind"]), []).append(r)
    conn = get_conn(db_path)
    try:
        for (cohort, kind), rs_ in groups.items():
            s = scoring._summarise(rs_)
            if not s["available"]:
                continue
            confs = [float(r["confidence_at"]) for r in rs_ if r["confidence_at"] is not None]
            conn.execute("INSERT INTO intel_confidence_log (week, cohort, rec_kind, n, mean_confidence, acceptance_rate, success_rate) "
                         "VALUES (?,?,?,?,?,?,?) ON CONFLICT(week, cohort, rec_kind) DO UPDATE SET n=excluded.n, "
                         "mean_confidence=excluded.mean_confidence, acceptance_rate=excluded.acceptance_rate, "
                         "success_rate=excluded.success_rate, computed_at=datetime('now')",
                         (week, cohort, kind, len(rs_), round(sum(confs) / len(confs), 3) if confs else None,
                          s["acceptance_rate"], s["success_rate"]))
            written += 1
        conn.commit()
    finally:
        conn.close()
    return {"written": written, "week": week}

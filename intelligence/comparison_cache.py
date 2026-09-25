"""Level 3: the Benchmark Engine's comparisons, materialised nightly
(Benchmarking audit BM4-14, Top-50 #46).

Every Ask call used to recompute every benchmark — up to ~84 queries and
~560 feature-row parses per call. `materialise()` writes each restaurant's
engine.compare() output into `intel_benchmark_facts` once a night, and
`read()` hands a fresh row back, so a request can be one indexed read
(engine.compare(..., use_cache=True)).

  * Bounded and resumable (CLAUDE.md): a wall clock, restaurants in id
    order from a cursor in job_cursors, the first restaurant always runs.
  * The viewer-dependent `location` kind (only logins that may switch
    locations see it) is never stored: a caller asking for it computes live.
  * Per restaurant, its own row only — a stored payload is exactly what
    engine.compare returns to that restaurant (published bands, viewer
    excluded), never another restaurant's figure.
"""
import json
import time
from datetime import date, datetime, timedelta

import models as _models_mod
from models import DB_PATH


def get_conn(db_path=None):
    """models.get_conn, resolved at call time (CLAUDE.md, bound imports)."""
    if db_path is None or db_path == DB_PATH:
        return _models_mod.get_conn()
    return _models_mod.get_conn(db_path)


CACHED_KINDS = ("self", "peers", "platform", "industry", "market")
MAX_AGE_HOURS = 36            # a night's pass plus slack; older is recomputed live
KEEP_WEEKS = 8                # rows older than this are pruned by the pass
CURSOR_KEY = "intelligence_benchmark_facts"
WALL_SECONDS = 120


def _cursor(conn, value=None):
    if value is None:
        try:
            row = conn.execute("SELECT value FROM job_cursors WHERE key=?", (CURSOR_KEY,)).fetchone()
            return int(row["value"]) if row and row["value"] else 0
        except (TypeError, ValueError, Exception):
            return 0
    conn.execute("INSERT INTO job_cursors (key, value, updated_at) VALUES (?,?,datetime('now')) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=datetime('now')",
                 (CURSOR_KEY, str(int(value))))


def store(restaurant_id, comparisons, week, db_path=DB_PATH):
    conn = get_conn(db_path)
    try:
        for c in comparisons:
            if not c.get("metric"):
                continue
            avail = any(k.get("available") for k in c.get("comparisons") or [])
            conn.execute("INSERT INTO intel_benchmark_facts (restaurant_id, metric, week, payload_json, available) "
                         "VALUES (?,?,?,?,?) ON CONFLICT(restaurant_id, metric, week) DO UPDATE SET "
                         "payload_json=excluded.payload_json, available=excluded.available, "
                         "computed_at=datetime('now')",
                         (restaurant_id, c["metric"], week, json.dumps(c, default=str), 1 if avail else 0))
        conn.commit()
    finally:
        conn.close()


def read(restaurant_id, metric, kinds=None, db_path=DB_PATH, max_age_hours=MAX_AGE_HOURS, now=None):
    """The stored engine.compare payload for (restaurant, metric) when it is
    fresh and the kinds asked for are exactly CACHED_KINDS (the headline and
    facts were chosen over those), else None. Never raises."""
    want = tuple(kinds or ())
    if set(want) != set(CACHED_KINDS):
        return None
    try:
        conn = get_conn(db_path)
        try:
            row = conn.execute("SELECT payload_json, computed_at FROM intel_benchmark_facts WHERE restaurant_id=? "
                               "AND metric=? ORDER BY computed_at DESC LIMIT 1", (restaurant_id, metric)).fetchone()
        finally:
            conn.close()
    except Exception:
        return None
    if not row:
        return None
    try:
        at = datetime.strptime(str(row["computed_at"])[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    if ((now or datetime.utcnow()) - at).total_seconds() > max_age_hours * 3600:
        return None
    try:
        payload = json.loads(row["payload_json"])
    except (TypeError, ValueError):
        return None
    if {c.get("kind") for c in payload.get("comparisons") or []} != set(want):
        return None
    payload["cached_at"] = str(row["computed_at"])
    return payload


def materialise(db_path=DB_PATH, today: date = None, wall_seconds=WALL_SECONDS) -> dict:
    """engine.compare_all for every active restaurant (demo accounts too —
    their own screens read it), bounded by `wall_seconds` and resumed from
    the cursor."""
    from . import engine
    from .features import iso_week
    from .jobs import active_restaurants
    today = today or date.today()
    week = iso_week(today)
    rs = sorted(active_restaurants(db_path, include_demo=True), key=lambda r: r.id)
    conn = get_conn(db_path)
    try:
        after = _cursor(conn)
    finally:
        conn.close()
    order = [r for r in rs if r.id > after] + [r for r in rs if r.id <= after]
    deadline = time.monotonic() + wall_seconds
    done = failed = 0
    last, stopped = after, False
    for i, r in enumerate(order):
        if i > 0 and time.monotonic() > deadline:
            stopped = True
            break
        try:
            comps = engine.compare_all(r.id, kinds=CACHED_KINDS, restaurant=r, db_path=db_path, today=today)
            store(r.id, comps, week, db_path=db_path)
            done += 1
        except Exception as e:
            failed += 1
            print(f"[intelligence.comparison_cache] {r.id} not materialised: {e}")
        last = r.id
    conn = get_conn(db_path)
    try:
        _cursor(conn, last if stopped else 0)
        conn.execute("DELETE FROM intel_benchmark_facts WHERE week < ?",
                     (iso_week(today - timedelta(weeks=KEEP_WEEKS)),))
        conn.commit()
    finally:
        conn.close()
    return {"restaurants": done, "failed": failed, "complete": not stopped, "resumed_after": after or None,
            "week": week}

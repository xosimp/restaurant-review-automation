"""Level 3: from comparison to prediction — what advice did at restaurants
whose Restaurant DNA is close to this one's (Benchmarking audit BM4 §5.3).

    predict_effect(rid, rec_kind, metric, tags=None)

returns a fact of kind "prediction" — {value, n_restaurants, n_orgs,
interval, basis} — for the response-validation P2 rule: a "restaurants like
yours reduced waste 11%" claim must bind to one. Built and DORMANT: below
any floor it is unavailable with its reason, which today is every
restaurant on the platform.

The estimate, in order:

  1. Neighbours: every other real restaurant OUTSIDE the viewer's
     organisation whose DNA is comparable under the prediction weights
     (dna.prediction_weights: structure plus the target metric's baseline),
     within the viewer's service type when it has one, that "started where
     you are" (the target dimension within BASELINE_Z_BAND z of the
     viewer's), at most K_NEIGHBOURS nearest and no farther than the 75th
     percentile of the candidates' distances.
  2. Taken effects: the neighbours' counted, measured results for this kind
     and metric (intel_rec_events.effect_pct, feedback.sync — signed so
     positive is better), weighted 1 ÷ (1 + distance) × the per-restaurant
     cap (scoring.capped_counts: no restaurant above ⅓).
  3. Control: the same metric's untaken results at those neighbours
     (observed:untaken trackers) in overlapping calendar windows. The lift is
     the weighted median taken effect minus the median untaken effect — a
     difference against doing nothing that absorbs seasonality and
     platform-wide shocks.
  4. Noise: a restaurant-cluster bootstrap (BOOTSTRAP resamples, seeded)
     for the 80% interval; Kish's n_eff = (Σw)² ÷ Σw².
  5. Floors, all of them: MIN_RESTAURANTS restaurants from MIN_ORGS
     organisations with a counted taken result, MIN_RESULTS capped results,
     n_eff ≥ MIN_N_EFF, MIN_UNTAKEN untaken results.
  6. An interval that spans 0 is "mixed results among similar restaurants",
     never a figure.

Never reveals a neighbour, a distance, a single restaurant's effect, a date
or a dollar: counts, a median and an interval only (privacy.assert_anonymous).
Likelihood words never come from here — only from the recommendation's own
confidence % (BM4 §5.3).
"""
import json
import random
import time
from datetime import date, datetime, timedelta

import models as _models_mod
from models import DB_PATH
from . import privacy
from .stats import percentile


def get_conn(db_path=None):
    """models.get_conn, resolved at call time (CLAUDE.md, bound imports)."""
    if db_path is None or db_path == DB_PATH:
        return _models_mod.get_conn()
    return _models_mod.get_conn(db_path)


K_NEIGHBOURS = 20
NEIGHBOUR_DISTANCE_PCTL = 75
BASELINE_Z_BAND = 1.0
MIN_RESTAURANTS = 5
MIN_ORGS = 5
MIN_RESULTS = 10               # rec_learning.PRIOR_MIN_MEASURED, capped
MIN_N_EFF = 8.0
MIN_UNTAKEN = 5
BOOTSTRAP = 2000
INTERVAL = (10, 90)            # the 80% interval
WINDOW_DAYS = 365
CURSOR_KEY = "intelligence_effects"
WALL_SECONDS = 60

# The DNA dimension that holds each outcome metric's baseline (for the
# prediction weights and the "started where you are" match).
METRIC_DIM = {
    "labor_pct": "labor_pct", "food_cost_pct": "food_cost_level", "avg_rating": "rating_level",
    "weekly_waste": "waste_rate", "comp_rate": "comp_rate", "void_rate": "void_rate",
    "overtime_hours": "overtime_intensity", "complaints": "negative_share", "response_hours": "reply_within_day",
}


def _unavailable(why, **kw):
    out = {"kind": "prediction", "available": False, "why_not": why, "value": None, "interval": None,
           "n_restaurants": kw.pop("n_restaurants", 0), "n_orgs": kw.pop("n_orgs", 0)}
    out.update(kw)
    return out


def _weighted_median(vals, weights):
    pairs = sorted(zip(vals, weights))
    tot = sum(w for _v, w in pairs)
    if not pairs or tot <= 0:
        return None
    acc = 0.0
    for v, w in pairs:
        acc += w
        if acc >= tot / 2.0:
            return v
    return pairs[-1][0]


def _cap_factors(counts):
    """{restaurant: factor} so no restaurant carries more than
    scoring.MAX_RESTAURANT_SHARE of the capped total (capped_counts' cap)."""
    from .scoring import MAX_RESTAURANT_SHARE
    ns = [n for n in counts.values() if n > 0]
    if not ns:
        return {}, 0.0
    c = float(max(ns))
    for _ in range(200):
        nxt = MAX_RESTAURANT_SHARE * sum(min(n, c) for n in ns)
        if nxt >= c - 1e-9:
            break
        c = nxt
    f = {r: min(1.0, c / n) for r, n in counts.items() if n > 0}
    return f, sum(n * f[r] for r, n in counts.items() if n > 0)


def _orgs(db_path):
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT id, organization_id FROM restaurants").fetchall()
    finally:
        conn.close()
    return {int(r["id"]): (r["organization_id"] if r["organization_id"] is not None else f"r{r['id']}") for r in rows}


def neighbours(restaurant_id, metric=None, db_path=DB_PATH, dnas=None, orgs=None) -> list:
    """[(restaurant, distance)] — SERVER-SIDE ONLY (never returned to a
    client, never in a payload): the comparable restaurants outside the
    viewer's organisation, nearest first, per the module docstring."""
    from . import dna
    dnas = dnas if dnas is not None else dna.latest_by_restaurant(db_path=db_path)
    orgs = orgs if orgs is not None else _orgs(db_path)
    mine = dnas.get(restaurant_id)
    if mine is None:
        row = dna.latest(restaurant_id, db_path=db_path)
        mine = (row or {}).get("dims")
    if not mine:
        return []
    target = METRIC_DIM.get(str(metric or "").split(":")[0])
    weights = dna.prediction_weights(target)
    my_org = orgs.get(restaurant_id)
    my_type = (mine.get("service_type") or {}).get("raw")
    my_base = (mine.get(target) or {}).get("z") if target else None
    cands = []
    for rid, dims in dnas.items():
        if rid == restaurant_id or orgs.get(rid) == my_org:
            continue
        if my_type is not None and (dims.get("service_type") or {}).get("raw") != my_type:
            continue
        if target:
            theirs = (dims.get(target) or {}).get("z")
            if my_base is None or theirs is None or abs(float(theirs) - float(my_base)) > BASELINE_Z_BAND:
                continue
        d = dna.distance(mine, dims, weights)
        if d is not None:
            cands.append((rid, d))
    if not cands:
        return []
    limit = percentile([d for _r, d in cands], NEIGHBOUR_DISTANCE_PCTL)
    return sorted([c for c in cands if c[1] <= limit], key=lambda c: c[1])[:K_NEIGHBOURS]


def _taken(conn, rids, rec_kind, metric, tags, since):
    if not rids:
        return []
    marks = ",".join("?" for _ in rids)
    rows = conn.execute(
        f"SELECT restaurant_id, effect_pct, tags_json, after_end FROM intel_rec_events WHERE action='measured' "
        f"AND rec_kind=? AND metric=? AND effect_pct IS NOT NULL AND outcome IN ('improved','worsened',"
        f"'no_clear_change') AND event_at >= ? AND restaurant_id IN ({marks})",
        (rec_kind, metric, since, *rids)).fetchall()
    out = []
    for r in rows:
        if tags:
            try:
                have = set(json.loads(r["tags_json"] or "[]"))
            except (TypeError, ValueError):
                have = set()
            if not set(tags) <= have:
                continue
        out.append(dict(r))
    return out


def _untaken(conn, rids, rec_kind, metric, since, span):
    """Untaken results (outcomes.observe_untaken: 'observed:untaken:<rec_id>')
    of this kind and metric at the neighbours, whose after-window overlaps
    the taken results' calendar span."""
    if not rids:
        return []
    import rec_learning
    from .feedback import effect_of
    marks = ",".join("?" for _ in rids)
    try:
        rows = conn.execute(
            f"SELECT o.* FROM recommendation_outcomes o JOIN rec_instances i "
            f"ON i.rec_id = substr(o.source_key, 18) AND i.restaurant_id = o.restaurant_id "
            f"WHERE o.source_key LIKE 'observed:untaken:%' AND o.status='evaluated' AND o.metric=? AND i.kind=? "
            f"AND o.evaluate_on >= ? AND o.restaurant_id IN ({marks})", (metric, rec_kind, since, *rids)).fetchall()
    except Exception:
        return []
    lo, hi = span
    out, last_end = [], {}
    # The rule rec_learning._untaken_rows counts an untaken result by: a
    # clear verdict, not read against its trigger window, not confounded —
    # and one result per window on a number, earliest first.
    for r in sorted((dict(x) for x in rows), key=lambda d: (str(d.get("after_start") or ""),
                                                           str(d.get("after_end") or ""))):
        start = str(r.get("after_start") or r.get("started_on") or "")[:10]
        end = str(r.get("after_end") or r.get("evaluate_on") or "")[:10]
        if lo and hi and (end < lo or start > hi):
            continue
        try:
            overl = bool(int(r.get("baseline_overlaps_trigger") or 0))
        except (TypeError, ValueError):
            overl = False
        if r.get("verdict") not in rec_learning.CLEAR_VERDICTS or overl or rec_learning._confounded(r):
            continue
        k = (r["restaurant_id"], r.get("metric"))
        if k in last_end and start <= last_end[k]:
            continue
        last_end[k] = end
        e = effect_of(r)
        if e and e.get("effect_pct") is not None:
            out.append({"restaurant_id": r["restaurant_id"], "effect_pct": e["effect_pct"]})
    return out


def _lift(taken, untaken, sims, caps):
    e = [float(t["effect_pct"]) for t in taken]
    w = [sims[t["restaurant_id"]] * caps.get(t["restaurant_id"], 1.0) for t in taken]
    m = _weighted_median(e, w)
    u = percentile([float(x["effect_pct"]) for x in untaken], 50)
    return None if (m is None or u is None) else m - u


def predict_effect(restaurant_id, rec_kind, metric, tags=None, db_path=DB_PATH, now=None, seed=11) -> dict:
    """The prediction fact for one (kind, metric) at one restaurant, or an
    unavailable one with why_not. See the module docstring. Never raises."""
    try:
        return _predict(restaurant_id, rec_kind, metric, tags, db_path, now or datetime.utcnow(), seed)
    except Exception as e:
        print(f"[intelligence.predict] {restaurant_id}/{rec_kind}/{metric} failed: {e}")
        return _unavailable("could not be computed right now")


def _predict(restaurant_id, rec_kind, metric, tags, db_path, now, seed):
    orgs = _orgs(db_path)
    nb = neighbours(restaurant_id, metric=metric, db_path=db_path, orgs=orgs)
    if len(nb) < MIN_RESTAURANTS:
        return _unavailable(f"fewer than {MIN_RESTAURANTS} restaurants on Cavnar have a profile close to yours yet",
                            n_restaurants=len(nb))
    sims = {r: 1.0 / (1.0 + d) for r, d in nb}
    since = (now - timedelta(days=WINDOW_DAYS)).strftime("%Y-%m-%d")
    conn = get_conn(db_path)
    try:
        taken = _taken(conn, list(sims), rec_kind, metric, tags, since)
        ends = sorted(str(t.get("after_end") or "")[:10] for t in taken if t.get("after_end"))
        span = (ends[0] if ends else "", ends[-1] if ends else "")
        untaken = _untaken(conn, list(sims), rec_kind, metric, since, span)
    finally:
        conn.close()
    per = {}
    for t in taken:
        per[t["restaurant_id"]] = per.get(t["restaurant_id"], 0) + 1
    n_rest = len(per)
    n_orgs = len({orgs.get(r) for r in per})
    caps, capped_total = _cap_factors(per)
    w = [sims[t["restaurant_id"]] * caps.get(t["restaurant_id"], 1.0) for t in taken]
    n_eff = (sum(w) ** 2 / sum(x * x for x in w)) if w and sum(x * x for x in w) else 0.0
    counts = {"n_restaurants": n_rest, "n_orgs": n_orgs, "n_results": round(capped_total, 1),
              "n_eff": round(n_eff, 1), "n_untaken": len(untaken)}
    if n_rest < MIN_RESTAURANTS or n_orgs < MIN_ORGS:
        return _unavailable(f"fewer than {MIN_RESTAURANTS} similar restaurants from {MIN_ORGS} owners have measured "
                            f"this advice", **counts)
    if capped_total < MIN_RESULTS:
        return _unavailable(f"fewer than {MIN_RESULTS} measured results among similar restaurants", **counts)
    if n_eff < MIN_N_EFF:
        return _unavailable("the results rest on too few restaurants once weighted", **counts)
    if len(untaken) < MIN_UNTAKEN:
        return _unavailable(f"fewer than {MIN_UNTAKEN} results where the advice wasn't taken to compare against",
                            **counts)
    value = _lift(taken, untaken, sims, caps)
    rng = random.Random(seed)
    t_by, u_by = {}, {}
    for t in taken:
        t_by.setdefault(t["restaurant_id"], []).append(t)
    for u in untaken:
        u_by.setdefault(u["restaurant_id"], []).append(u)
    t_keys, u_keys = sorted(t_by), sorted(u_by)
    boots = []
    for _ in range(BOOTSTRAP):
        tt = [x for k in (rng.choice(t_keys) for _ in t_keys) for x in t_by[k]]
        uu = [x for k in (rng.choice(u_keys) for _ in u_keys) for x in u_by[k]]
        cnt = {}
        for x in tt:
            cnt[x["restaurant_id"]] = cnt.get(x["restaurant_id"], 0) + 1
        lf = _lift(tt, uu, sims, _cap_factors(cnt)[0])
        if lf is not None:
            boots.append(lf)
    lo, hi = percentile(boots, INTERVAL[0]), percentile(boots, INTERVAL[1])
    if value is None or lo is None or hi is None:
        return _unavailable("could not be estimated", **counts)
    interval = [round(lo, 1), round(hi, 1)]
    if lo <= 0 <= hi:
        fact = {"kind": "prediction", "available": True, "mixed": True, "value": None, "interval": interval,
                "basis": (f"mixed results among {n_rest} restaurants with a profile close to yours — no figure"),
                **counts}
    else:
        fact = {"kind": "prediction", "available": True, "mixed": False, "value": round(value, 1),
                "interval": interval, "metric": metric, "rec_kind": rec_kind,
                "basis": (f"among {n_rest} restaurants with a profile close to yours ({n_orgs} owners), taking this "
                          f"advice moved the number a median {abs(value):.0f}% "
                          f"{'better' if value > 0 else 'worse'} than not taking it (most between "
                          f"{interval[0]:+.0f}% and {interval[1]:+.0f}%) — what happened at similar restaurants, "
                          f"not a guarantee here"), **counts}
    return privacy.assert_anonymous(fact)


def run_weekly(db_path=DB_PATH, today: date = None, wall_seconds=WALL_SECONDS) -> dict:
    """The weekly, bounded, resumable pass: for each real restaurant, each
    (kind, metric) its own open recommendations target (rec_instances.
    expected_metric — no trawling), predict_effect into intel_effects. When
    no (kind, metric) anywhere has MIN_RESTAURANTS restaurants with a
    counted taken effect, nothing can clear the floors and the pass writes
    nothing (every restaurant today)."""
    from .features import iso_week
    from .jobs import active_restaurants
    today = today or date.today()
    week = iso_week(today)
    conn = get_conn(db_path)
    try:
        best = conn.execute("SELECT MAX(n) FROM (SELECT COUNT(DISTINCT restaurant_id) AS n FROM intel_rec_events "
                            "WHERE action='measured' AND effect_pct IS NOT NULL GROUP BY rec_kind, metric)"
                            ).fetchone()[0] or 0
        row = conn.execute("SELECT value FROM job_cursors WHERE key=?", (CURSOR_KEY,)).fetchone()
    finally:
        conn.close()
    if best < MIN_RESTAURANTS:
        return {"week": week, "written": 0, "skipped": "below the floors platform-wide"}
    cur_week, after = "", 0
    if row and row["value"] and "|" in str(row["value"]):
        cur_week, a = str(row["value"]).split("|", 1)
        after = int(a) if a.isdigit() else (-1 if a == "done" else 0)
    if cur_week == week and after == -1:
        return {"week": week, "written": 0, "complete": True}
    if cur_week != week:
        after = 0
    rs = sorted((r for r in active_restaurants(db_path) if r.id > after), key=lambda r: r.id)
    deadline = time.monotonic() + wall_seconds
    written, last, stopped = 0, after, False
    for i, r in enumerate(rs):
        if i > 0 and time.monotonic() > deadline:
            stopped = True
            break
        conn = get_conn(db_path)
        try:
            pairs = conn.execute("SELECT DISTINCT kind, expected_metric FROM rec_instances WHERE restaurant_id=? "
                                 "AND expected_metric IS NOT NULL AND kind IS NOT NULL AND created_at >= ?",
                                 (r.id, (today - timedelta(days=90)).isoformat())).fetchall()
        finally:
            conn.close()
        for p in pairs:
            fact = predict_effect(r.id, p["kind"], p["expected_metric"], db_path=db_path)
            conn = get_conn(db_path)
            try:
                conn.execute("INSERT INTO intel_effects (restaurant_id, rec_kind, metric, week, available, payload_json) "
                             "VALUES (?,?,?,?,?,?) ON CONFLICT(restaurant_id, rec_kind, metric, week) DO UPDATE SET "
                             "available=excluded.available, payload_json=excluded.payload_json, "
                             "computed_at=datetime('now')",
                             (r.id, p["kind"], p["expected_metric"], week, 1 if fact.get("available") else 0,
                              json.dumps(fact)))
                conn.commit()
            finally:
                conn.close()
            written += 1
        last = r.id
    conn = get_conn(db_path)
    try:
        conn.execute("INSERT INTO job_cursors (key, value, updated_at) VALUES (?,?,datetime('now')) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=datetime('now')",
                     (CURSOR_KEY, f"{week}|{last if stopped else 'done'}"))
        conn.commit()
    finally:
        conn.close()
    return {"week": week, "written": written, "complete": not stopped}


def stored(restaurant_id, rec_kind, metric, db_path=DB_PATH) -> dict:
    """This week's (or the latest) stored prediction fact, else unavailable."""
    conn = get_conn(db_path)
    try:
        r = conn.execute("SELECT payload_json FROM intel_effects WHERE restaurant_id=? AND rec_kind=? AND metric=? "
                         "ORDER BY week DESC LIMIT 1", (restaurant_id, rec_kind, metric)).fetchone()
    finally:
        conn.close()
    if not r:
        return _unavailable("not computed yet")
    try:
        return json.loads(r["payload_json"])
    except (TypeError, ValueError):
        return _unavailable("not computed yet")

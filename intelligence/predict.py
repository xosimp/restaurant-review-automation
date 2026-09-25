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
     organisation (privacy.org_key — the same organisation rule as every
     band, R4-22) whose DNA is comparable under the prediction weights
     (dna.prediction_weights: structure plus the target metric's baseline,
     both rows re-normalised from raw under one norm set), within the
     viewer's confirmed service model when it has one, at most K_NEIGHBOURS
     nearest and no farther than the 75th percentile of the candidates'
     distances.
  2. "Started where you are", per result: a neighbour's result counts only
     when its target dimension, AS OF THE WEEK THE ADVICE WAS TAKEN (its DNA
     row from that week or up to BASELINE_MAX_AGE_WEEKS before), sat within
     BASELINE_Z_BAND z of the viewer's today — never the neighbour's
     current baseline (R4-20). No row from then: not counted.
  3. Taken effects: the neighbours' counted, measured results for this kind
     and metric (intel_rec_events.effect_pct, feedback.sync — signed so
     positive is better), weighted 1 ÷ (1 + distance) × the per-restaurant
     cap (scoring.capped_counts: no restaurant above ⅓).
  4. Control, built exactly like the taken arm (R4-20): the same kind's and
     metric's untaken results at those neighbours (observed:untaken
     trackers; the kind read from the key by feedback.kind_of, the one
     vocabulary the taken rows use), filtered by the same tags and the same
     started-where-you-are rule, in windows overlapping the taken results'
     (no taken window, no control), weighted and capped the same way, from
     MIN_UNTAKEN_RESTAURANTS restaurants and MIN_UNTAKEN_ORGS organisations.
     The lift is taken minus untaken — a difference against doing nothing
     that absorbs seasonality and platform-wide shocks.
  5. The estimate of each arm is a weighted Harrell–Davis median (Beta
     weights over every result, n = Kish's n_eff), never one member's own
     effect, and the figure and interval are whole percents (R4-21) — the
     disclosure rule bands follow (benchmarks: Harrell–Davis, coarse step).
  6. Noise: a restaurant-cluster bootstrap (BOOTSTRAP resamples, seeded)
     for the 80% interval; Kish's n_eff = (Σw)² ÷ Σw².
  7. Floors, all of them: MIN_RESTAURANTS restaurants from MIN_ORGS
     organisations with a counted taken result, MIN_RESULTS capped results,
     n_eff ≥ MIN_N_EFF, the control floors above and MIN_UNTAKEN results.
  8. An interval that spans 0 is "mixed results among similar restaurants",
     never a figure.

The weekly pass (run_weekly) loads the DNA, the organisations and the norms
once, and checks its wall-clock bound before every (kind, metric) pair, with
a cursor that resumes mid-restaurant (R4-24).

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
from .stats import beta_cdf, percentile


def get_conn(db_path=None):
    """models.get_conn, resolved at call time (CLAUDE.md, bound imports)."""
    if db_path is None or db_path == DB_PATH:
        return _models_mod.get_conn()
    return _models_mod.get_conn(db_path)


K_NEIGHBOURS = 20
NEIGHBOUR_DISTANCE_PCTL = 75
BASELINE_Z_BAND = 1.0
BASELINE_MAX_AGE_WEEKS = 3     # the DNA row that describes a restaurant when it took the advice
MIN_RESTAURANTS = 5
MIN_ORGS = 5
MIN_RESULTS = 10               # rec_learning.PRIOR_MIN_MEASURED, capped
MIN_N_EFF = 8.0
MIN_UNTAKEN = 5                # untaken results
MIN_UNTAKEN_RESTAURANTS = 5    # … from this many restaurants
MIN_UNTAKEN_ORGS = 3           # … and this many organisations
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


def _weighted_hd(vals, weights, p=50):
    """The weighted Harrell–Davis quantile (p in 0..100): every value
    weighted by the Beta(q(n+1), (1−q)(n+1)) mass over its share of the
    cumulative weight, n = Kish's n_eff. With equal weights it is
    stats.harrell_davis. Unlike a weighted median it is never one member's
    own figure (R4-21). None on empty."""
    pairs = sorted((float(v), float(w)) for v, w in zip(vals, weights) if w is not None and float(w) > 0)
    if not pairs:
        return None
    if len(pairs) == 1:
        return pairs[0][0]
    tot = sum(w for _v, w in pairs)
    n_eff = tot * tot / sum(w * w for _v, w in pairs)
    q = min(max(p / 100.0, 1e-9), 1 - 1e-9)
    a, b = q * (n_eff + 1), (1 - q) * (n_eff + 1)
    out = acc = prev = 0.0
    for v, w in pairs:
        acc += w / tot
        cur = beta_cdf(min(acc, 1.0), a, b)
        out += (cur - prev) * v
        prev = cur
    return out


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
    """{restaurant_id: organisation key} by privacy.org_key — the one rule
    bands, patterns and priors count organisations by (R4-22). The whole row
    goes in, so the key sees every column its fallback reads."""
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT * FROM restaurants").fetchall()
    finally:
        conn.close()
    return {int(r["id"]): privacy.org_key(dict(r)) for r in rows}


def _target_dim(metric):
    return METRIC_DIM.get(str(metric or "").split(":")[0])


def _my_dims(restaurant_id, dnas, db_path):
    from . import dna
    mine = dnas.get(restaurant_id)
    if mine is None:
        row = dna.latest(restaurant_id, db_path=db_path)
        if row and int(row.get("version") or 1) >= dna.DNA_VERSION:
            mine = row.get("dims")
    return mine or None


def neighbours(restaurant_id, metric=None, db_path=DB_PATH, dnas=None, orgs=None, norms=None) -> list:
    """[(restaurant, distance)] — SERVER-SIDE ONLY (never returned to a
    client, never in a payload): the comparable restaurants outside the
    viewer's organisation, nearest first, per the module docstring. The
    viewer must have the target metric's baseline measured; whether each
    neighbour started where the viewer is is judged per result, at the time
    it took the advice (_started_near)."""
    from . import dna
    dnas = dnas if dnas is not None else dna.latest_by_restaurant(db_path=db_path)
    orgs = orgs if orgs is not None else _orgs(db_path)
    norms = norms if norms is not None else dna.platform_norms(db_path=db_path)
    mine = _my_dims(restaurant_id, dnas, db_path)
    if not mine:
        return []
    target = _target_dim(metric)
    weights = dna.prediction_weights(target)
    my_org = orgs.get(restaurant_id, f"r{restaurant_id}")
    my_type = (mine.get("service_type") or {}).get("raw")
    if target and dna.z_of(target, (mine.get(target) or {}).get("raw"), norms) is None:
        return []
    cands = []
    for rid, dims in dnas.items():
        if rid == restaurant_id or orgs.get(rid, f"r{rid}") == my_org:
            continue
        if my_type is not None and (dims.get("service_type") or {}).get("raw") != my_type:
            continue
        d = dna.distance(mine, dims, weights, norms)
        if d is not None:
            cands.append((rid, d))
    if not cands:
        return []
    limit = percentile([d for _r, d in cands], NEIGHBOUR_DISTANCE_PCTL)
    return sorted([c for c in cands if c[1] <= limit], key=lambda c: c[1])[:K_NEIGHBOURS]


def _baseline_history(conn, rids, target, norms) -> dict:
    """{restaurant: [(week, z of the target dimension)]}, oldest first, from
    each neighbour's stored DNA rows (current version), re-normalised under
    the pass's norms."""
    from . import dna
    if not rids or not target:
        return {}
    marks = ",".join("?" for _ in rids)
    try:
        rows = conn.execute(f"SELECT restaurant_id, week, dims_json FROM intel_dna WHERE restaurant_id IN ({marks}) "
                            f"AND COALESCE(version, 1) >= ? ORDER BY week", (*rids, dna.DNA_VERSION)).fetchall()
    except Exception:
        return {}
    out = {}
    for r in rows:
        try:
            raw = ((json.loads(r["dims_json"] or "{}") or {}).get(target) or {}).get("raw")
        except (TypeError, ValueError):
            continue
        z = dna.z_of(target, raw, norms)
        if z is not None:
            out.setdefault(r["restaurant_id"], []).append((r["week"], z))
    return out


def _started_near(history, rid, started_on, my_base) -> bool:
    """Whether restaurant `rid`'s target dimension, in the DNA row that
    described it when it took the advice (the week of `started_on`, or up to
    BASELINE_MAX_AGE_WEEKS before), was within BASELINE_Z_BAND of the
    viewer's baseline now. Unknown is False."""
    from .features import iso_week
    if my_base is None:
        return True                      # no target dimension for this metric: structure only
    try:
        start = date.fromisoformat(str(started_on or "")[:10])
    except ValueError:
        return False
    hi, lo = iso_week(start), iso_week(start - timedelta(weeks=BASELINE_MAX_AGE_WEEKS))
    then = [z for wk, z in history.get(rid, ()) if lo <= wk <= hi]
    return bool(then) and abs(then[-1] - my_base) <= BASELINE_Z_BAND


def _taken(conn, rids, rec_kind, metric, tags, since):
    if not rids:
        return []
    marks = ",".join("?" for _ in rids)
    rows = conn.execute(
        f"SELECT restaurant_id, source_key, effect_pct, tags_json, after_end FROM intel_rec_events "
        f"WHERE action='measured' "
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
    # When each advice was taken: the measured row's key is
    # "<source_key>#o<tracker id>" (feedback.measured_key); the tracker's
    # started_on is the day. A row with no tracker id has no known start.
    from .feedback import MEASURED_KEY_SEP
    tids = {}
    for t in out:
        tail = str(t.get("source_key") or "").rsplit(MEASURED_KEY_SEP, 1)
        if len(tail) == 2 and tail[1].isdigit():
            tids[int(tail[1])] = t
    if tids:
        marks = ",".join("?" for _ in tids)
        try:
            for r in conn.execute(f"SELECT id, started_on FROM recommendation_outcomes WHERE id IN ({marks})",
                                  tuple(tids)).fetchall():
                tids[int(r["id"])]["started_on"] = r["started_on"]
        except Exception:
            pass
    return out


def _untaken(conn, rids, rec_kind, metric, since, span, tags=None):
    """Untaken results (outcomes.observe_untaken: 'observed:untaken:<rec_id>')
    of this kind and metric at the neighbours, whose after-window overlaps
    the taken results' calendar span — [] when there is no span (R4-20: an
    empty span used to skip the window filter). The kind comes from the
    episode's key through feedback.kind_of, the function the taken rows'
    rec_kind was written by, so the two arms share one vocabulary; the
    episode's tags (rec_instances.tags, else rec_ledger.tags_for) must hold
    `tags`, as the taken rows' must."""
    lo, hi = span
    if not rids or not lo or not hi:
        return []
    import rec_learning
    from .feedback import effect_of, kind_of
    marks = ",".join("?" for _ in rids)
    base = (f"FROM recommendation_outcomes o JOIN rec_instances i "
            f"ON i.rec_id = substr(o.source_key, 18) AND i.restaurant_id = o.restaurant_id "
            f"WHERE o.source_key LIKE 'observed:untaken:%' AND o.status='evaluated' AND o.metric=? "
            f"AND (i.key = ? OR substr(i.key, 1, ?) = ?) "
            f"AND o.evaluate_on >= ? AND o.restaurant_id IN ({marks})")
    args = (metric, rec_kind, len(rec_kind) + 1, rec_kind + ":", since, *rids)
    try:
        rows = conn.execute(f"SELECT o.*, i.key AS rec_key, i.tags AS rec_tags {base}", args).fetchall()
    except Exception:
        try:                     # a database from before rec_instances.tags
            rows = conn.execute(f"SELECT o.*, i.key AS rec_key, NULL AS rec_tags {base}", args).fetchall()
        except Exception:
            return []
    out, last_end = [], {}
    # The rule rec_learning._untaken_rows counts an untaken result by: a
    # clear verdict, not read against its trigger window, not confounded —
    # and one result per window on a number, earliest first.
    for r in sorted((dict(x) for x in rows), key=lambda d: (str(d.get("after_start") or ""),
                                                           str(d.get("after_end") or ""))):
        if kind_of(r.get("rec_key")) != rec_kind:
            continue
        if tags:
            try:
                have = set(json.loads(r.get("rec_tags") or "null") or ())
            except (TypeError, ValueError):
                have = set()
            if not have:
                try:
                    import rec_ledger
                    have = set(rec_ledger.tags_for(r.get("rec_key"), kind=rec_kind))
                except Exception:
                    have = set()
            if not set(tags) <= have:
                continue
        start = str(r.get("after_start") or r.get("started_on") or "")[:10]
        end = str(r.get("after_end") or r.get("evaluate_on") or "")[:10]
        if end < lo or start > hi:
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
            out.append({"restaurant_id": r["restaurant_id"], "effect_pct": e["effect_pct"],
                        "started_on": r.get("started_on")})
    return out


def _arm(rows, sims):
    """(effects, weights) of one arm: similarity × the per-restaurant cap,
    the same for the taken and the untaken arm (R4-20)."""
    cnt = {}
    for x in rows:
        cnt[x["restaurant_id"]] = cnt.get(x["restaurant_id"], 0) + 1
    caps = _cap_factors(cnt)[0]
    return ([float(x["effect_pct"]) for x in rows],
            [sims[x["restaurant_id"]] * caps.get(x["restaurant_id"], 1.0) for x in rows])


def _lift(taken, untaken, sims):
    m = _weighted_hd(*_arm(taken, sims))
    u = _weighted_hd(*_arm(untaken, sims))
    return None if (m is None or u is None) else m - u


def predict_effect(restaurant_id, rec_kind, metric, tags=None, db_path=DB_PATH, now=None, seed=11,
                   dnas=None, orgs=None, norms=None) -> dict:
    """The prediction fact for one (kind, metric) at one restaurant, or an
    unavailable one with why_not. See the module docstring. `dnas`, `orgs`
    and `norms` are loaded once per pass by run_weekly (R4-24); a single
    call loads them itself. Never raises."""
    try:
        return _predict(restaurant_id, rec_kind, metric, tags, db_path, now or datetime.utcnow(), seed,
                        dnas, orgs, norms)
    except Exception as e:
        print(f"[intelligence.predict] {restaurant_id}/{rec_kind}/{metric} failed: {e}")
        return _unavailable("could not be computed right now")


def _predict(restaurant_id, rec_kind, metric, tags, db_path, now, seed, dnas=None, orgs=None, norms=None):
    from . import dna
    dnas = dnas if dnas is not None else dna.latest_by_restaurant(db_path=db_path)
    orgs = orgs if orgs is not None else _orgs(db_path)
    norms = norms if norms is not None else dna.platform_norms(db_path=db_path)
    nb = neighbours(restaurant_id, metric=metric, db_path=db_path, dnas=dnas, orgs=orgs, norms=norms)
    if len(nb) < MIN_RESTAURANTS:
        return _unavailable(f"fewer than {MIN_RESTAURANTS} restaurants on Cavnar AI have a profile close to yours yet",
                            n_restaurants=len(nb))
    sims = {r: 1.0 / (1.0 + d) for r, d in nb}
    target = _target_dim(metric)
    mine = _my_dims(restaurant_id, dnas, db_path) or {}
    my_base = dna.z_of(target, (mine.get(target) or {}).get("raw"), norms) if target else None
    since = (now - timedelta(days=WINDOW_DAYS)).strftime("%Y-%m-%d")
    conn = get_conn(db_path)
    try:
        history = _baseline_history(conn, list(sims), target, norms)
        taken = [t for t in _taken(conn, list(sims), rec_kind, metric, tags, since)
                 if _started_near(history, t["restaurant_id"], t.get("started_on"), my_base)]
        ends = sorted(str(t.get("after_end") or "")[:10] for t in taken if t.get("after_end"))
        span = (ends[0] if ends else "", ends[-1] if ends else "")
        untaken = [u for u in _untaken(conn, list(sims), rec_kind, metric, since, span, tags=tags)
                   if _started_near(history, u["restaurant_id"], u.get("started_on"), my_base)]
    finally:
        conn.close()
    per = {}
    for t in taken:
        per[t["restaurant_id"]] = per.get(t["restaurant_id"], 0) + 1
    n_rest = len(per)
    n_orgs = len({orgs.get(r, f"r{r}") for r in per})
    caps, capped_total = _cap_factors(per)
    w = [sims[t["restaurant_id"]] * caps.get(t["restaurant_id"], 1.0) for t in taken]
    n_eff = (sum(w) ** 2 / sum(x * x for x in w)) if w and sum(x * x for x in w) else 0.0
    u_rest = {u["restaurant_id"] for u in untaken}
    u_orgs = {orgs.get(r, f"r{r}") for r in u_rest}
    counts = {"n_restaurants": n_rest, "n_orgs": n_orgs, "n_results": round(capped_total, 1),
              "n_eff": round(n_eff, 1), "n_untaken": len(untaken), "n_untaken_restaurants": len(u_rest),
              "n_untaken_orgs": len(u_orgs)}
    if n_rest < MIN_RESTAURANTS or n_orgs < MIN_ORGS:
        return _unavailable(f"fewer than {MIN_RESTAURANTS} similar restaurants from {MIN_ORGS} owners have measured "
                            f"this advice", **counts)
    if capped_total < MIN_RESULTS:
        return _unavailable(f"fewer than {MIN_RESULTS} measured results among similar restaurants", **counts)
    if n_eff < MIN_N_EFF:
        return _unavailable("the results rest on too few restaurants once weighted", **counts)
    if not span[0]:
        return _unavailable("the measured results carry no window to compare against", **counts)
    if len(untaken) < MIN_UNTAKEN or len(u_rest) < MIN_UNTAKEN_RESTAURANTS or len(u_orgs) < MIN_UNTAKEN_ORGS:
        return _unavailable(f"fewer than {MIN_UNTAKEN} results where the advice wasn't taken, from "
                            f"{MIN_UNTAKEN_RESTAURANTS} restaurants and {MIN_UNTAKEN_ORGS} owners, to compare "
                            f"against", **counts)
    value = _lift(taken, untaken, sims)
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
        lf = _lift(tt, uu, sims)
        if lf is not None:
            boots.append(lf)
    lo, hi = percentile(boots, INTERVAL[0]), percentile(boots, INTERVAL[1])
    if value is None or lo is None or hi is None:
        return _unavailable("could not be estimated", **counts)
    # Whole percents (R4-21): the figure and its interval are coarse, the way
    # a published band's quartiles are.
    interval = [int(round(lo)), int(round(hi))]
    shown = int(round(value))
    if interval[0] <= 0 <= interval[1] or shown == 0:
        fact = {"kind": "prediction", "available": True, "mixed": True, "value": None, "interval": interval,
                "basis": (f"mixed results among {n_rest} restaurants with a profile close to yours — no figure"),
                **counts}
    else:
        fact = {"kind": "prediction", "available": True, "mixed": False, "value": shown,
                "interval": interval, "metric": metric, "rec_kind": rec_kind,
                "basis": (f"among {n_rest} restaurants with a profile close to yours ({n_orgs} owners), taking this "
                          f"advice moved the number a median {abs(shown)}% "
                          f"{'better' if shown > 0 else 'worse'} than not taking it (most between "
                          f"{interval[0]:+d}% and {interval[1]:+d}%) — what happened at similar restaurants, "
                          f"not a guarantee here"), **counts}
    return privacy.assert_anonymous(fact)


def _read_cursor(value, week):
    """(start restaurant id, pairs of it already done) for this week, or
    None when this week is complete. "week|rid:k" resumes at restaurant rid
    after its first k pairs; "week|rid" (an older cursor) after rid."""
    if not value or "|" not in str(value):
        return 0, 0
    cur_week, a = str(value).split("|", 1)
    if cur_week != week:
        return 0, 0
    if a == "done":
        return None
    if ":" in a:
        rid, k = a.split(":", 1)
        if rid.isdigit() and k.isdigit():
            return int(rid), int(k)
    return (int(a) + 1, 0) if a.isdigit() else (0, 0)


def _pairs(restaurant_id, today, db_path):
    """The (kind, metric) pairs this restaurant's own recommendations of the
    last 90 days target, the kind read from each key by feedback.kind_of —
    the vocabulary intel_rec_events.rec_kind is written in. Sorted, so a
    resumed pass skips the same ones."""
    from .feedback import kind_of
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT DISTINCT key, expected_metric FROM rec_instances WHERE restaurant_id=? "
                            "AND expected_metric IS NOT NULL AND key IS NOT NULL AND created_at >= ?",
                            (restaurant_id, (today - timedelta(days=90)).isoformat())).fetchall()
    finally:
        conn.close()
    return sorted({(kind_of(r["key"]), r["expected_metric"]) for r in rows})


def run_weekly(db_path=DB_PATH, today: date = None, wall_seconds=WALL_SECONDS) -> dict:
    """The weekly, bounded, resumable pass: for each real restaurant, each
    (kind, metric) its own open recommendations target (rec_instances —
    no trawling), predict_effect into intel_effects. When no (kind, metric)
    anywhere has MIN_RESTAURANTS restaurants with a counted taken effect,
    nothing can clear the floors and the pass writes nothing (every
    restaurant today).

    Bounded PER PAIR (R4-24): the DNA, the organisations and the norms are
    loaded once for the pass, the wall clock is checked before every pair
    (the first always runs, so the pass always moves), and the cursor
    records the restaurant and the pairs of it already done."""
    from . import dna
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
    resume = _read_cursor(row["value"] if row else None, week)
    if resume is None:
        return {"week": week, "written": 0, "complete": True}
    start_rid, skip = resume
    dnas = dna.latest_by_restaurant(db_path=db_path)
    orgs = _orgs(db_path)
    norms = dna.platform_norms(db_path=db_path)
    rs = sorted((r for r in active_restaurants(db_path) if r.id >= start_rid), key=lambda r: r.id)
    deadline = time.monotonic() + wall_seconds
    written, stopped_at, first = 0, None, True
    for r in rs:
        pairs = _pairs(r.id, today, db_path)
        k0 = skip if r.id == start_rid else 0
        for k in range(k0, len(pairs)):
            if not first and time.monotonic() > deadline:
                stopped_at = (r.id, k)
                break
            first = False
            kind, metric = pairs[k]
            fact = predict_effect(r.id, kind, metric, db_path=db_path, dnas=dnas, orgs=orgs, norms=norms)
            conn = get_conn(db_path)
            try:
                conn.execute("INSERT INTO intel_effects (restaurant_id, rec_kind, metric, week, available, payload_json) "
                             "VALUES (?,?,?,?,?,?) ON CONFLICT(restaurant_id, rec_kind, metric, week) DO UPDATE SET "
                             "available=excluded.available, payload_json=excluded.payload_json, "
                             "computed_at=datetime('now')",
                             (r.id, kind, metric, week, 1 if fact.get("available") else 0, json.dumps(fact)))
                conn.commit()
            finally:
                conn.close()
            written += 1
        if stopped_at:
            break
    conn = get_conn(db_path)
    try:
        conn.execute("INSERT INTO job_cursors (key, value, updated_at) VALUES (?,?,datetime('now')) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=datetime('now')",
                     (CURSOR_KEY, f"{week}|{stopped_at[0]}:{stopped_at[1]}" if stopped_at else f"{week}|done"))
        conn.commit()
    finally:
        conn.close()
    return {"week": week, "written": written, "complete": stopped_at is None}


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

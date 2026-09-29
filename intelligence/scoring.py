"""Recommendation success: which kinds are accepted, which improve the
metric they pointed at, and how long that takes — per cohort or
platform-wide, always as rates over counts, never a restaurant.

Counted per recommendation (restaurant, key), not per event row: one
reprice answered "accepted", then "done", then "implemented" is ONE
recommendation taken.

  acceptance_rate  taken / (taken + declined + hidden + ignored). An ignored
                   recommendation — shown, never answered, expired — stays
                   in the denominator; a snooze ("Not today") is neither.
  success_rate     improved / (improved + worsened + no_clear_change). A
                   result that could not be measured (`unknown`) is NOT a
                   failure: it is reported as `unknown` and kept out of the
                   denominator (ROI audit #2 — it used to count as one).
                   `no_clear_change` is reported on its own.
  measured         results with a clear verdict (the success denominator).
                   Each verdict is rec_learning.learned_verdict's (feedback.
                   sync writes it that way) — the one success definition.
  success_enough   measured ≥ MIN_MEASURED_FOR_RATE; public() withholds the
                   rate below it.
  auto             delayed actions that ran unless cancelled: neither taken
                   nor declined, in no acceptance rate (CA2 finding 15).

Each recommendation lands in ONE bucket, the strongest thing that happened
to it: taken > declined > hidden > ignored (a snooze, then an auto, only
when nothing else did). An episode that expired and was answered later is the answer, not an
answer AND an ignore (re-audit B8).

The privacy floor is per figure (re-audit B3): an acceptance rate is a
cohort fact only over MIN_COHORT restaurants that SETTLED a recommendation
of the kind (`answered_restaurants`), a success rate only over MIN_COHORT
restaurants that MEASURED one (`measured_restaurants`). Five restaurants
that let one card expire are no floor for a success rate one restaurant
measured. `acceptance_available` / `success_available` say which cleared;
`available` is either. `exclude_restaurant_id` leaves the asking restaurant
out of its own prior.

Organisations, not locations (Benchmarking re-audit #14, R1-02, R4-23): a
cross-restaurant figure also needs privacy.MIN_ORGS organisations behind
it (`answered_orgs` / `measured_orgs`), the asking restaurant's WHOLE
organisation is left out of its prior (a group owner could otherwise
subtract their own locations' results and read the one outsider's), and
the share cap is per organisation — privacy.org_map's organisation, the
shared-login and Stripe grouping the bands count by (memory audit
PLATFORM-19: one owner with two emails and one login was two
organisations here).

Memory audit 9/29/26 (workstream M8):
  * Counted per EPISODE: every row is one episode's answer
    ("<key>#e<rec_id>", feedback.episode_key), so nine declines and one
    take of trim_day:Monday are ten recommendations, not one "taken".
  * Who may teach: a cross-restaurant figure leaves out every restaurant
    models.learning_eligible refuses (demo, test, internal) and the ones
    still inside their seeded quarantine (jobs.excluded_learning_ids); a
    deleted restaurant's kept, anonymised rows (no restaurants row) count
    as their own organisation and never carry a name.
  * Google user data never pooled: a row marked `google_data`
    (intelligence.provenance) is read by no cross-restaurant figure.
  * The group is a RUNG (prior_rungs): the confirmed concept (`cohort`),
    the confirmed partition of the kind's metric family (`partition`,
    membership by the ladder's rule — a bar-led member stands in its
    service model's group too), or — for a behaviour kind only — every
    restaurant on Cavnar AI.
  * Decay (`decay=True`): each row weighs rec_learning.decay_weight — the
    kind's half-life, same-season weighting for seasonal kinds, nothing
    past the horizon — in the `*_decayed` figures the priors read.
"""
import models as _models_mod
from models import DB_PATH
from . import privacy
from .stats import percentile, shrink


def get_conn(db_path=None):
    """models.get_conn, resolved at call time (CLAUDE.md, bound imports —
    re-audit B23: a patched models.get_conn never reached this module)."""
    if db_path is None or db_path == DB_PATH:
        return _models_mod.get_conn()
    return _models_mod.get_conn(db_path)


# "auto" is NOT here (CA2 finding 15): a delayed action that runs unless
# someone cancels it (delayed.py) is Cavnar acting, not the owner
# accepting. It is its own bucket, reported beside the others and in no
# acceptance rate.
_ACCEPT = ("done", "confirmed", "tracking", "accepted", "implemented")
_DECLINE = ("not_for_us", "dismissed")
_AUTO = ("auto",)
CLEAR = ("improved", "worsened", "no_clear_change")
# The acceptance buckets, strongest first.
BUCKETS = ("taken", "declined", "hidden", "ignored")
# A success rate is SHOWN (public()) only over this many clear results —
# rec_learning.MIN_MEASURED_FOR_RATE, the owner-facing floor (CA2 finding
# 4). The raw and shrunk rates are still computed for the engine's own
# weighting.
MIN_MEASURED_FOR_RATE = 5
# No one restaurant may carry more than this share of a CROSS-restaurant
# success figure (re-audit B2 #3): one peer's 8 results among a cohort's 12
# stood as "10 of 12 improved at restaurants like yours". Each restaurant's
# clear results are scaled down to at most this share of the capped total
# (capped_counts); `measured_capped` / `improved_capped` are the figure a
# prior may use, the raw counts stay beside them.
MAX_RESTAURANT_SHARE = 1.0 / 3.0


def capped_counts(per_restaurant, max_share=MAX_RESTAURANT_SHARE):
    """(measured, improved) summed over {restaurant: (measured, improved)}
    after each restaurant's weight is cut so its measured count is at most
    `max_share` of the capped total: the cap c is the fixed point of
    c = max_share × Σ min(n_r, c), and a restaurant over it counts c of its
    n_r results at its own improved share. Pure."""
    ns = [n for n, _k in per_restaurant.values() if n > 0]
    if not ns:
        return 0.0, 0.0
    c = float(max(ns))
    for _ in range(200):
        nxt = max_share * sum(min(n, c) for n in ns)
        if nxt >= c - 1e-9:
            break
        c = nxt
    measured = improved = 0.0
    for n, k in per_restaurant.values():
        if n <= 0:
            continue
        w = min(1.0, c / n)
        measured += n * w
        improved += k * w
    return round(measured, 3), round(improved, 3)


# Priors read the cohort's record over the learning horizon (BM3-12,
# Top-50 #32; memory audit PLATFORM-11): rec_learning.DECAY_HORIZON_DAYS —
# the same window the restaurant's own model reads, inside the ledger's
# retention — each result weighted by the kind's decay in the `*_decayed`
# figures, and by 0.5 ** (age / PRIOR_HALF_LIFE_DAYS) in the older
# `*_recent` ones. A test holds PRIOR_WINDOW_DAYS in step with
# rec_learning's.
PRIOR_WINDOW_DAYS = 730
PRIOR_HALF_LIFE_DAYS = 180

# privacy.org_map reads every restaurant and the shared logins; kept this
# long per database, then read again.
ORG_MAP_TTL_SECONDS = 300
_org_cache = {"key": None, "at": 0.0, "map": None}


def _full_org_map(db_path):
    """privacy.org_map, cached briefly per database (the same organisation
    every band counts by — shared owner logins and the Stripe customer
    joined). A failed read falls back to each row's own key."""
    import time as _time
    key = (db_path or DB_PATH, getattr(_models_mod, "DB_PATH", None), id(getattr(_models_mod, "get_conn", None)))
    now = _time.monotonic()
    if _org_cache["key"] == key and _org_cache["map"] is not None and now - _org_cache["at"] < ORG_MAP_TTL_SECONDS:
        return _org_cache["map"]
    try:
        m = privacy.org_map(db_path=None if db_path in (None, DB_PATH) else db_path)
    except Exception as e:
        print(f"[intelligence.scoring] organisation map unavailable, each row's own key: {e}")
        m = None
    _org_cache.update(key=key, at=now, map=m)
    return m


def invalidate_org_map(*_a):
    _org_cache.update(key=None, at=0.0, map=None)


def org_map(ids, db_path=DB_PATH, conn=None) -> dict:
    """{restaurant_id: organisation key} for `ids` — privacy.org_map's
    (organisation, owner email, an owner-level login shared between
    restaurants and the Stripe customer, joined; PLATFORM-19), else each
    row's own privacy.org_key. A restaurant with no row (a deleted
    restaurant's kept rows) is its own organisation. Server-side only."""
    ids = sorted({int(i) for i in ids or () if i is not None})
    if not ids:
        return {}
    full = _full_org_map(db_path)
    out = {i: f"r{i}" for i in ids}
    if full is not None:
        for i in ids:
            if i in full:
                out[i] = full[i]
        return out
    own = conn is None
    conn = conn or get_conn(db_path)
    try:
        rows = conn.execute(f"SELECT id, organization_id, location_group, owner_email FROM restaurants "
                            f"WHERE id IN ({','.join('?' for _ in ids)})", ids).fetchall()
    finally:
        if own:
            conn.close()
    for r in rows:
        out[int(r["id"])] = privacy.org_key(dict(r))
    return out


# ── the kind's family and comparability (the prior ladder) ──────────────────
# Which confirmed partition a recommendation kind is read in, from what it
# pulls on (rec_ledger's topic): staffing, hours and overtime among
# restaurants that staff alike (the labor family); pricing, purchasing,
# food cost, waste and ordering within a menu family (food); everything
# else by service model (format). And which kinds are BEHAVIOUR — how a
# restaurant runs, comparable across every type (metrics_registry's
# behaviour class: replying to reviews, posting on a cadence) — the only
# kinds that may borrow from all restaurants on Cavnar AI.
_TOPIC_FAMILY = {"staffing": "labor", "hours": "labor", "overtime": "labor", "training": "labor",
                 "pricing": "food", "purchasing": "food", "food_cost": "food", "waste": "food", "ordering": "food"}
BEHAVIOUR_TOPICS = ("replies", "posting")
_ECONOMICS_TOPICS = ("staffing", "hours", "overtime", "pricing", "purchasing", "food_cost", "waste", "ordering",
                     "loss", "sales")


def _topic(kind, key=None):
    try:
        import rec_ledger
        return rec_ledger._topic_of(str(kind or ""), str(key or kind or ""))
    except Exception:
        return None


def kind_family(kind, key=None) -> str:
    """'format' | 'labor' | 'food' — the metric family a recommendation
    kind's partition is read in. By the KIND (a row is stamped, and a prior
    read, per kind): a DSR action or a link, whose topic varies by key, is
    read by service model alone unless a key is passed."""
    return _TOPIC_FAMILY.get(_topic(kind, key), "format")


def kind_comparability(kind, key=None) -> str:
    """'behaviour' | 'economics' | 'format' — metrics_registry's classes, for
    a recommendation kind. Only a behaviour kind reads the platform rung."""
    t = _topic(kind, key)
    if t in BEHAVIOUR_TOPICS:
        return "behaviour"
    if t in _ECONOMICS_TOPICS:
        return "economics"
    return "format"


def partition_rows_sql(column="partition_key"):
    """The ladder's membership rule for one partition group, as SQL: a row
    of that partition, or of the same partition with the bar-led split
    (categories.partition_ladder's coarser rung holds both)."""
    return f"({column} = ? OR REPLACE({column}, '|bar', '') = ?)"


def _without_org(rows, orgs, exclude_restaurant_id):
    """rows minus every row of the excluded restaurant's organisation."""
    if exclude_restaurant_id is None:
        return list(rows)
    mine = orgs.get(int(exclude_restaurant_id), f"r{int(exclude_restaurant_id)}")
    return [r for r in rows if r["restaurant_id"] != exclude_restaurant_id
            and orgs.get(r["restaurant_id"], f"r{r['restaurant_id']}") != mine]


def _age_days(at, now):
    from datetime import datetime
    try:
        t = datetime.strptime(str(at or "")[:10], "%Y-%m-%d")
    except ValueError:
        return None
    return max(0.0, (now - t).total_seconds() / 86400.0)


def _pooled_filter(where, args, exclude_restaurant_id):
    """The WHERE clauses every CROSS-restaurant read of intel_rec_events
    takes: never the asking restaurant, never Google user data
    (intelligence.provenance). The eligibility filter is applied in Python
    (_eligible_rows): it is models.learning_eligible, not SQL."""
    if exclude_restaurant_id is not None:
        where.append("restaurant_id != ?"); args.append(exclude_restaurant_id)
    where.append("COALESCE(google_data, 0) = 0")


def _eligible_rows(rows, db_path):
    """Rows whose restaurant may teach a cross-restaurant figure: not a
    demo, test or internal account, not inside its seeded quarantine
    (jobs.excluded_learning_ids — models.learning_eligible and the demo
    rule). A deleted restaurant's kept rows are not in the table and stay."""
    from .jobs import excluded_learning_ids
    out_ids = excluded_learning_ids(db_path=db_path)
    return [r for r in rows if r["restaurant_id"] not in out_ids]


def kind_stats(rec_kind: str, cohort: str = None, restaurant_id: int = None, db_path: str = DB_PATH,
               exclude_restaurant_id: int = None, window_days: int = None, half_life_days: float = None,
               now=None, partition: str = None, decay: bool = False) -> dict:
    """Rates for one kind. With `restaurant_id` it is that restaurant's own
    record (Level 1); with `cohort` the concept's (a confirmed type); with
    `partition` the confirmed peer partition's (partition_rows_sql — the
    ladder's membership); otherwise every restaurant on Cavnar AI.
    `exclude_restaurant_id` leaves one restaurant — and its whole
    organisation — out of a cross-restaurant figure (the restaurant the
    figure is a prior for).

    `window_days` reads only events of the last that many days (priors pass
    PRIOR_WINDOW_DAYS). `half_life_days` adds, for a cross-restaurant
    figure, `measured_recent` / `improved_recent` / `success_rate_recent`:
    the capped counts with each clear result weighted 0.5 ** (age ÷
    half-life). `decay` adds the same with the kind's own decay
    (rec_learning.decay_weight — half-life by kind, same-season weighting
    for a seasonal kind): `measured_decayed` / `improved_decayed` /
    `success_rate_decayed` and `acceptance_rate_decayed` (each episode
    weighted by its answer's age) with `acceptance_rate_decayed_shrunk`
    over `acceptance_n_decayed`. Recency only re-weights; it never lets a
    figure stand below the privacy floors above."""
    from datetime import datetime, timedelta
    now = now or datetime.utcnow()
    where, args = ["rec_kind=?"], [rec_kind]
    if window_days:
        where.append("event_at >= ?"); args.append((now - timedelta(days=int(window_days))).strftime("%Y-%m-%d"))
    cross = restaurant_id is None
    if not cross:
        where.append("restaurant_id=?"); args.append(restaurant_id)
    elif cohort:
        where.append("cohort=?"); args.append(cohort)
    elif partition:
        where.append(partition_rows_sql()); args += [partition, partition]
    if cross:
        _pooled_filter(where, args, exclude_restaurant_id)
    conn = get_conn(db_path)
    try:
        rows = conn.execute(f"SELECT restaurant_id, source_key, action, outcome, days_to_effect, event_at "
                            f"FROM intel_rec_events WHERE {' AND '.join(where)}", args).fetchall()
    finally:
        conn.close()
    orgs = {}
    if cross:
        rows = _eligible_rows(rows, db_path)
        orgs = org_map([r["restaurant_id"] for r in rows] + [exclude_restaurant_id], db_path=db_path)
        # The asking restaurant's whole organisation is out of its prior.
        rows = _without_org(rows, orgs, exclude_restaurant_id)
    out = _summarise(rows, cross=cross, orgs=orgs)
    if half_life_days and cross:
        per = {}
        for r in rows:
            if r["action"] != "measured" or r["outcome"] not in CLEAR:
                continue
            age = _age_days(r["event_at"], now)
            w = 0.5 ** ((age or 0.0) / float(half_life_days))
            o = orgs.get(r["restaurant_id"], f"r{r['restaurant_id']}")
            n, k = per.get(o, (0.0, 0.0))
            per[o] = (n + w, k + (w if r["outcome"] == "improved" else 0.0))
        mc, ic = capped_counts(per)
        out["measured_recent"], out["improved_recent"] = mc, ic
        out["success_rate_recent"] = round(ic / mc, 3) if mc else None
        out["half_life_days"] = half_life_days
    if decay and cross:
        out.update(_decayed(rec_kind, rows, orgs, now))
    if window_days:
        out["window_days"] = int(window_days)
    return out


def _decayed(rec_kind, rows, orgs, now) -> dict:
    """The `*_decayed` figures (kind_stats' `decay`): every episode and
    result weighted by rec_learning.decay_weight for the kind."""
    import rec_learning
    acts, last = {}, {}
    per = {}
    for r in rows:
        if r["action"] == "measured":
            if r["outcome"] in CLEAR:
                w = rec_learning.decay_weight(rec_kind, r["event_at"], now)
                o = orgs.get(r["restaurant_id"], f"r{r['restaurant_id']}")
                n, k = per.get(o, (0.0, 0.0))
                per[o] = (n + w, k + (w if r["outcome"] == "improved" else 0.0))
            continue
        rec = _rec(r)
        acts.setdefault(rec, set()).add(r["action"])
        last[rec] = max(last.get(rec, ""), str(r["event_at"] or ""))
    num = den = 0.0
    for rec, a in acts.items():
        b = _bucket(a)
        if b not in BUCKETS:
            continue
        w = rec_learning.decay_weight(rec_kind, last[rec], now)
        den += w
        num += w if b == "taken" else 0.0
    mc, ic = capped_counts(per)
    rate = round(num / den, 3) if den else None
    return {"acceptance_n_decayed": round(den, 3), "acceptance_rate_decayed": rate,
            "acceptance_rate_decayed_shrunk": shrink(rate, den) if rate is not None else None,
            "measured_decayed": mc, "improved_decayed": ic,
            "success_rate_decayed": round(ic / mc, 3) if mc else None}


# A kind this restaurant has no record of is RANKED with help from similar
# restaurants' results (BM3-12, Top-50 #32): each peer's clear results
# weighted by recency (PRIOR_HALF_LIFE_DAYS) and by how close its Restaurant
# DNA is to this restaurant's — 1 ÷ (1 + distance) when the two profiles are
# comparable, SIMILARITY_UNMATCHED when they are the same type but not yet
# comparable — over the capped counts (no restaurant above
# MAX_RESTAURANT_SHARE). Below the privacy floor or PRIOR_MIN_RESULTS clear
# results it is unavailable. Ranking only: it never produces or lifts a
# confidence %.
SIMILARITY_UNMATCHED = 0.5
PRIOR_MIN_RESULTS = 10          # rec_learning.PRIOR_MIN_MEASURED


def similar_prior(rec_kind: str, restaurant_id: int, cohort: str = None, db_path: str = DB_PATH, now=None,
                  partition: str = None, platform: bool = False) -> dict:
    """{available, rate, restaurants, measured, weighted} — the similarity-
    and recency-weighted improved share of `rec_kind` among the OTHER real
    restaurants of one prior rung over PRIOR_WINDOW_DAYS: the concept
    (`cohort`), the confirmed partition (`partition`), or — `platform`, a
    behaviour kind only (the caller's rule) — every restaurant on Cavnar
    AI. Never a demo, test or internal account's result, never Google user
    data, never the asking restaurant's organisation. Anonymous by
    construction (counts and a rate only)."""
    from datetime import datetime, timedelta
    now = now or datetime.utcnow()
    out = {"available": False, "rate": None, "restaurants": 0, "measured": 0, "weighted": 0.0}
    if not cohort and not partition and not platform:
        return dict(out, reason="no restaurant type")
    where = ["rec_kind=?", "action='measured'", "event_at >= ?"]
    args = [rec_kind, (now - timedelta(days=PRIOR_WINDOW_DAYS)).strftime("%Y-%m-%d")]
    if cohort:
        where.append("cohort=?"); args.append(cohort)
    elif partition:
        where.append(partition_rows_sql()); args += [partition, partition]
    _pooled_filter(where, args, restaurant_id)
    conn = get_conn(db_path)
    try:
        rows = conn.execute(f"SELECT restaurant_id, outcome, event_at FROM intel_rec_events "
                            f"WHERE {' AND '.join(where)}", args).fetchall()
    finally:
        conn.close()
    rows = _eligible_rows(rows, db_path)
    orgs = org_map([r["restaurant_id"] for r in rows] + [restaurant_id], db_path=db_path)
    rows = _without_org(rows, orgs, restaurant_id)
    clear = [r for r in rows if r["outcome"] in CLEAR]
    peers = {r["restaurant_id"] for r in clear}
    n_orgs = len({orgs.get(p, f"r{p}") for p in peers})
    out.update(restaurants=len(peers), measured=len(clear))
    if not privacy.cohort_ok(len(peers)) or n_orgs < privacy.MIN_ORGS or len(clear) < PRIOR_MIN_RESULTS:
        return dict(out, reason="too few similar restaurants (or organisations) have measured this")
    sims = {}
    try:
        from . import dna
        mine = (dna.latest(restaurant_id, db_path=db_path) or {}).get("dims") or {}
        theirs = dna.latest_by_restaurant(db_path=db_path)
        for p in peers:
            d = dna.distance(mine, theirs.get(p) or {}) if mine else None
            sims[p] = (1.0 / (1.0 + d)) if d is not None else SIMILARITY_UNMATCHED
    except Exception as e:
        print(f"[intelligence.scoring] DNA similarity unavailable: {e}")
    import rec_learning
    per = {}
    for r in clear:
        # The kind's own decay (rec_learning.decay_weight), the rule every
        # learner reads by (PLATFORM-11).
        w = sims.get(r["restaurant_id"], SIMILARITY_UNMATCHED) * rec_learning.decay_weight(rec_kind, r["event_at"],
                                                                                            now)
        o = orgs.get(r["restaurant_id"], f"r{r['restaurant_id']}")      # capped per organisation
        n, k = per.get(o, (0.0, 0.0))
        per[o] = (n + w, k + (w if r["outcome"] == "improved" else 0.0))
    mc, ic = capped_counts(per)
    if not mc:
        return dict(out, reason="no weight")
    return privacy.assert_anonymous(dict(out, available=True, rate=round(ic / mc, 4), weighted=round(mc, 3)))


def _rec(r):
    """The recommendation a row belongs to: (restaurant, key). Rows without
    a key (a caller's own synthetic rows) count one each."""
    try:
        key = r["source_key"]
    except (IndexError, KeyError):
        key = None
    return (r["restaurant_id"], key if key is not None else id(r))


def _bucket(actions):
    """The one acceptance bucket a recommendation's actions put it in."""
    if actions & set(_ACCEPT):
        return "taken"
    if actions & set(_DECLINE):
        return "declined"
    if "hidden" in actions:
        return "hidden"
    if "ignored" in actions:
        return "ignored"
    if "snoozed" in actions:
        return "snoozed"
    if actions & set(_AUTO):
        return "auto"
    return None


def _summarise(rows, cross=True, orgs=None) -> dict:
    restaurants = {r["restaurant_id"] for r in rows}
    acts = {}
    for r in rows:
        acts.setdefault(_rec(r), set()).add(r["action"])
    buckets = {rec: _bucket(a) for rec, a in acts.items()}
    counts = {b: sum(1 for v in buckets.values() if v == b) for b in BUCKETS + ("snoozed", "auto")}
    accepted, declined, hidden, ignored = (counts[b] for b in BUCKETS)
    snoozed, auto = counts["snoozed"], counts["auto"]
    answered = accepted + declined + hidden
    results = [r for r in rows if r["action"] == "measured"]
    clear = [r for r in results if r["outcome"] in CLEAR]
    improved = sum(1 for r in clear if r["outcome"] == "improved")
    worsened = sum(1 for r in clear if r["outcome"] == "worsened")
    no_change = sum(1 for r in clear if r["outcome"] == "no_clear_change")
    days = [r["days_to_effect"] for r in clear if r["outcome"] == "improved" and r["days_to_effect"] is not None]
    denominator = answered + ignored
    answered_restaurants = {rec[0] for rec, b in buckets.items() if b in BUCKETS}
    measured_restaurants = {r["restaurant_id"] for r in clear}
    out = {
        "restaurants": len(restaurants), "answered_restaurants": len(answered_restaurants),
        "measured_restaurants": len(measured_restaurants),
        "answered": answered, "accepted": accepted, "declined": declined,
        "hidden": hidden, "ignored": ignored, "snoozed": snoozed, "auto": auto,
        "acceptance_rate": round(accepted / denominator, 3) if denominator else None,
        "measured": len(clear), "improved": improved, "worsened": worsened, "no_clear_change": no_change,
        "unknown": len(results) - len(clear),
        "success_rate": round(improved / len(clear), 3) if clear else None,
        "success_enough": len(clear) >= MIN_MEASURED_FOR_RATE,
        "median_days_to_improvement": percentile(days, 50) if days else None,
    }
    out["success_rate_shrunk"] = shrink(out["success_rate"], len(clear))
    out["acceptance_rate_shrunk"] = shrink(out["acceptance_rate"], denominator)
    if cross:
        orgs = orgs or {}

        def _o(rid):
            return orgs.get(rid, f"r{rid}")
        per = {}
        for r in clear:
            o = _o(r["restaurant_id"])        # the share cap is per organisation
            n, k = per.get(o, (0, 0))
            per[o] = (n + 1, k + (1 if r["outcome"] == "improved" else 0))
        mc, ic = capped_counts(per)
        out["measured_capped"], out["improved_capped"] = mc, ic
        out["success_rate_capped"] = round(ic / mc, 3) if mc else None
        out["max_restaurant_share"] = round(MAX_RESTAURANT_SHARE, 3)
        # Below the floor the rates are still computed for the engine's own
        # weighting, but nothing here may be shown as a cohort fact — and
        # each figure has its own population.
        out["answered_orgs"] = len({_o(r) for r in answered_restaurants})
        out["measured_orgs"] = len({_o(r) for r in measured_restaurants})
        out["acceptance_available"] = (privacy.cohort_ok(len(answered_restaurants))
                                       and out["answered_orgs"] >= privacy.MIN_ORGS)
        out["success_available"] = (privacy.cohort_ok(len(measured_restaurants))
                                    and out["measured_orgs"] >= privacy.MIN_ORGS)
        out["available"] = out["acceptance_available"] or out["success_available"]
        if not out["available"]:
            out["reason"] = (f"fewer than {privacy.MIN_COHORT} restaurants from {privacy.MIN_ORGS} organisations "
                             "have answered this kind")
        elif not out["success_available"]:
            out["reason"] = (f"fewer than {privacy.MIN_COHORT} restaurants from {privacy.MIN_ORGS} organisations "
                             "have a measured result for this kind")
    else:
        out["acceptance_available"] = out["success_available"] = out["available"] = True
    return out


def public(s) -> dict:
    """A cross-restaurant summary as it may be SHOWN: a rate whose own
    population is below the floor is withheld (its counts too)."""
    out = dict(s)
    if not s.get("success_available"):
        for k in ("measured", "improved", "worsened", "no_clear_change", "unknown", "success_rate",
                  "success_rate_shrunk", "median_days_to_improvement", "measured_capped", "improved_capped",
                  "success_rate_capped"):
            out[k] = None
    elif not s.get("success_enough", True):
        # The counts stay; a rate over fewer than MIN_MEASURED_FOR_RATE clear
        # results is not shown as one (CA2 finding 4).
        out["success_rate"] = None
        if "success_rate_capped" in out:
            out["success_rate_capped"] = None
    if not s.get("acceptance_available"):
        for k in ("answered", "accepted", "declined", "hidden", "ignored", "snoozed", "auto", "acceptance_rate",
                  "acceptance_rate_shrunk"):
            out[k] = None
    return out


def rank_kinds(cohort: str = None, db_path: str = DB_PATH, limit: int = 20) -> list:
    """Every kind with its rates, best measured success first. Cross-
    restaurant: a kind answered by fewer than MIN_COHORT restaurants is
    listed as unavailable with its count only. Never a demo, test or
    internal account's rows, never Google user data (the admin view counts
    what a prior may count)."""
    where, args = [], []
    if cohort:
        where.append("cohort=?"); args.append(cohort)
    _pooled_filter(where, args, None)
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT rec_kind, restaurant_id, source_key, action, outcome, days_to_effect "
                            f"FROM intel_rec_events WHERE {' AND '.join(where)}", args).fetchall()
    finally:
        conn.close()
    rows = _eligible_rows(rows, db_path)
    by_kind = {}
    for r in rows:
        by_kind.setdefault(r["rec_kind"], []).append(r)
    orgs = org_map([r["restaurant_id"] for r in rows], db_path=db_path)
    out = []
    for kind, rs in by_kind.items():
        s = _summarise(rs, orgs=orgs)
        s["rec_kind"] = kind
        if not s["available"]:
            s = {"rec_kind": kind, "restaurants": s["restaurants"], "available": False, "reason": s["reason"]}
        else:
            s = public(s)
        out.append(s)
    out.sort(key=lambda s: (s.get("available", False), s.get("success_rate_shrunk") or 0, s.get("measured") or 0), reverse=True)
    return out[:limit]


def platform_totals(db_path: str = DB_PATH) -> dict:
    """Every recommendation on the platform, summarised — what the admin
    Intelligence page shows: the eligible restaurants' rows only, never
    Google user data."""
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT restaurant_id, source_key, action, outcome, days_to_effect "
                            "FROM intel_rec_events WHERE COALESCE(google_data, 0) = 0").fetchall()
    finally:
        conn.close()
    rows = _eligible_rows(rows, db_path)
    return _summarise(rows, orgs=org_map([r["restaurant_id"] for r in rows], db_path=db_path))

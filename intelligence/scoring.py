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


# Priors read the cohort's recent record, not all of history (BM3-12,
# Top-50 #32): a 365-day window, and within it each clear result weighted
# 0.5 ** (age / PRIOR_HALF_LIFE_DAYS) in the `*_recent` figures.
PRIOR_WINDOW_DAYS = 365
PRIOR_HALF_LIFE_DAYS = 180


def _age_days(at, now):
    from datetime import datetime
    try:
        t = datetime.strptime(str(at or "")[:10], "%Y-%m-%d")
    except ValueError:
        return None
    return max(0.0, (now - t).total_seconds() / 86400.0)


def kind_stats(rec_kind: str, cohort: str = None, restaurant_id: int = None, db_path: str = DB_PATH,
               exclude_restaurant_id: int = None, window_days: int = None, half_life_days: float = None,
               now=None) -> dict:
    """Rates for one kind. With `restaurant_id` it is that restaurant's own
    record (Level 1); with `cohort` the cohort's; otherwise platform-wide.
    `exclude_restaurant_id` leaves one restaurant out of a cohort or
    platform figure (the restaurant the figure is a prior for).

    `window_days` reads only events of the last that many days (priors pass
    PRIOR_WINDOW_DAYS). `half_life_days` adds, for a cross-restaurant
    figure, `measured_recent` / `improved_recent` / `success_rate_recent`:
    the capped counts with each clear result weighted 0.5 ** (age ÷
    half-life) — recency only ever re-weights measured results, it never
    lets a cohort figure stand below the privacy floors above."""
    from datetime import datetime, timedelta
    now = now or datetime.utcnow()
    where, args = ["rec_kind=?"], [rec_kind]
    if window_days:
        where.append("event_at >= ?"); args.append((now - timedelta(days=int(window_days))).strftime("%Y-%m-%d"))
    if restaurant_id is not None:
        where.append("restaurant_id=?"); args.append(restaurant_id)
    elif cohort:
        where.append("cohort=?"); args.append(cohort)
    if restaurant_id is None and exclude_restaurant_id is not None:
        where.append("restaurant_id != ?"); args.append(exclude_restaurant_id)
    if restaurant_id is None:
        # A cohort or platform rate never counts a demo account's seeded
        # answers (CA3 F7; the rule lives in jobs.real_restaurant_ids).
        from .jobs import SEEDED_RESTAURANT_SQL, SEEDED_HISTORY_DAYS
        where.append(f"restaurant_id NOT IN ({SEEDED_RESTAURANT_SQL})"); args.append(f"-{SEEDED_HISTORY_DAYS} days")
    conn = get_conn(db_path)
    try:
        rows = conn.execute(f"SELECT restaurant_id, source_key, action, outcome, days_to_effect, event_at "
                            f"FROM intel_rec_events WHERE {' AND '.join(where)}", args).fetchall()
    finally:
        conn.close()
    out = _summarise(rows, cross=restaurant_id is None)
    if half_life_days and restaurant_id is None:
        per = {}
        for r in rows:
            if r["action"] != "measured" or r["outcome"] not in CLEAR:
                continue
            age = _age_days(r["event_at"], now)
            w = 0.5 ** ((age or 0.0) / float(half_life_days))
            n, k = per.get(r["restaurant_id"], (0.0, 0.0))
            per[r["restaurant_id"]] = (n + w, k + (w if r["outcome"] == "improved" else 0.0))
        mc, ic = capped_counts(per)
        out["measured_recent"], out["improved_recent"] = mc, ic
        out["success_rate_recent"] = round(ic / mc, 3) if mc else None
        out["half_life_days"] = half_life_days
    if window_days:
        out["window_days"] = int(window_days)
    return out


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


def similar_prior(rec_kind: str, restaurant_id: int, cohort: str, db_path: str = DB_PATH, now=None) -> dict:
    """{available, rate, restaurants, measured, weighted} — the similarity-
    and recency-weighted improved share of `rec_kind` among the cohort's
    OTHER real restaurants over PRIOR_WINDOW_DAYS. Anonymous by
    construction (counts and a rate only)."""
    from datetime import datetime, timedelta
    now = now or datetime.utcnow()
    out = {"available": False, "rate": None, "restaurants": 0, "measured": 0, "weighted": 0.0}
    if not cohort:
        return dict(out, reason="no restaurant type")
    from .jobs import SEEDED_RESTAURANT_SQL, SEEDED_HISTORY_DAYS
    conn = get_conn(db_path)
    try:
        rows = conn.execute(
            f"SELECT restaurant_id, outcome, event_at FROM intel_rec_events WHERE rec_kind=? AND cohort=? "
            f"AND action='measured' AND restaurant_id != ? AND event_at >= ? "
            f"AND restaurant_id NOT IN ({SEEDED_RESTAURANT_SQL})",
            (rec_kind, cohort, restaurant_id, (now - timedelta(days=PRIOR_WINDOW_DAYS)).strftime("%Y-%m-%d"),
             f"-{SEEDED_HISTORY_DAYS} days")).fetchall()
    finally:
        conn.close()
    clear = [r for r in rows if r["outcome"] in CLEAR]
    peers = {r["restaurant_id"] for r in clear}
    out.update(restaurants=len(peers), measured=len(clear))
    if not privacy.cohort_ok(len(peers)) or len(clear) < PRIOR_MIN_RESULTS:
        return dict(out, reason="too few similar restaurants have measured this")
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
    per = {}
    for r in clear:
        age = _age_days(r["event_at"], now) or 0.0
        w = sims.get(r["restaurant_id"], SIMILARITY_UNMATCHED) * 0.5 ** (age / float(PRIOR_HALF_LIFE_DAYS))
        n, k = per.get(r["restaurant_id"], (0.0, 0.0))
        per[r["restaurant_id"]] = (n + w, k + (w if r["outcome"] == "improved" else 0.0))
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


def _summarise(rows, cross=True) -> dict:
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
        per = {}
        for r in clear:
            n, k = per.get(r["restaurant_id"], (0, 0))
            per[r["restaurant_id"]] = (n + 1, k + (1 if r["outcome"] == "improved" else 0))
        mc, ic = capped_counts(per)
        out["measured_capped"], out["improved_capped"] = mc, ic
        out["success_rate_capped"] = round(ic / mc, 3) if mc else None
        out["max_restaurant_share"] = round(MAX_RESTAURANT_SHARE, 3)
        # Below the floor the rates are still computed for the engine's own
        # weighting, but nothing here may be shown as a cohort fact — and
        # each figure has its own population.
        out["acceptance_available"] = privacy.cohort_ok(len(answered_restaurants))
        out["success_available"] = privacy.cohort_ok(len(measured_restaurants))
        out["available"] = out["acceptance_available"] or out["success_available"]
        if not out["available"]:
            out["reason"] = f"fewer than {privacy.MIN_COHORT} restaurants have answered this kind"
        elif not out["success_available"]:
            out["reason"] = f"fewer than {privacy.MIN_COHORT} restaurants have a measured result for this kind"
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
    listed as unavailable with its count only."""
    conn = get_conn(db_path)
    try:
        if cohort:
            rows = conn.execute("SELECT rec_kind, restaurant_id, source_key, action, outcome, days_to_effect "
                                "FROM intel_rec_events WHERE cohort=?", (cohort,)).fetchall()
        else:
            rows = conn.execute("SELECT rec_kind, restaurant_id, source_key, action, outcome, days_to_effect "
                                "FROM intel_rec_events").fetchall()
    finally:
        conn.close()
    by_kind = {}
    for r in rows:
        by_kind.setdefault(r["rec_kind"], []).append(r)
    out = []
    for kind, rs in by_kind.items():
        s = _summarise(rs)
        s["rec_kind"] = kind
        if not s["available"]:
            s = {"rec_kind": kind, "restaurants": s["restaurants"], "available": False, "reason": s["reason"]}
        else:
            s = public(s)
        out.append(s)
    out.sort(key=lambda s: (s.get("available", False), s.get("success_rate_shrunk") or 0, s.get("measured") or 0), reverse=True)
    return out[:limit]


def platform_totals(db_path: str = DB_PATH) -> dict:
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT restaurant_id, source_key, action, outcome, days_to_effect "
                            "FROM intel_rec_events").fetchall()
    finally:
        conn.close()
    return _summarise(rows)

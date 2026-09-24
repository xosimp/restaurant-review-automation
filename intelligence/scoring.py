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

Each recommendation lands in ONE bucket, the strongest thing that happened
to it: taken > declined > hidden > ignored (a snooze only when nothing else
did). An episode that expired and was answered later is the answer, not an
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


_ACCEPT = ("done", "confirmed", "tracking", "auto", "accepted", "implemented")
_DECLINE = ("not_for_us", "dismissed")
CLEAR = ("improved", "worsened", "no_clear_change")
# The acceptance buckets, strongest first.
BUCKETS = ("taken", "declined", "hidden", "ignored")


def kind_stats(rec_kind: str, cohort: str = None, restaurant_id: int = None, db_path: str = DB_PATH,
               exclude_restaurant_id: int = None) -> dict:
    """Rates for one kind. With `restaurant_id` it is that restaurant's own
    record (Level 1); with `cohort` the cohort's; otherwise platform-wide.
    `exclude_restaurant_id` leaves one restaurant out of a cohort or
    platform figure (the restaurant the figure is a prior for)."""
    where, args = ["rec_kind=?"], [rec_kind]
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
        rows = conn.execute(f"SELECT restaurant_id, source_key, action, outcome, days_to_effect FROM intel_rec_events "
                            f"WHERE {' AND '.join(where)}", args).fetchall()
    finally:
        conn.close()
    return _summarise(rows, cross=restaurant_id is None)


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
    return None


def _summarise(rows, cross=True) -> dict:
    restaurants = {r["restaurant_id"] for r in rows}
    acts = {}
    for r in rows:
        acts.setdefault(_rec(r), set()).add(r["action"])
    buckets = {rec: _bucket(a) for rec, a in acts.items()}
    counts = {b: sum(1 for v in buckets.values() if v == b) for b in BUCKETS + ("snoozed",)}
    accepted, declined, hidden, ignored = (counts[b] for b in BUCKETS)
    snoozed = counts["snoozed"]
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
        "hidden": hidden, "ignored": ignored, "snoozed": snoozed,
        "acceptance_rate": round(accepted / denominator, 3) if denominator else None,
        "measured": len(clear), "improved": improved, "worsened": worsened, "no_clear_change": no_change,
        "unknown": len(results) - len(clear),
        "success_rate": round(improved / len(clear), 3) if clear else None,
        "median_days_to_improvement": percentile(days, 50) if days else None,
    }
    out["success_rate_shrunk"] = shrink(out["success_rate"], len(clear))
    out["acceptance_rate_shrunk"] = shrink(out["acceptance_rate"], denominator)
    if cross:
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
                  "success_rate_shrunk", "median_days_to_improvement"):
            out[k] = None
    if not s.get("acceptance_available"):
        for k in ("answered", "accepted", "declined", "hidden", "ignored", "snoozed", "acceptance_rate",
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

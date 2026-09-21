"""Recommendation success: which kinds are accepted, which improve the
metric they pointed at, and how long that takes — per cohort or
platform-wide, always as rates over counts, never a restaurant.
"""
from models import get_conn, DB_PATH
from . import privacy
from .stats import percentile, shrink

_ACCEPT = ("done", "confirmed", "tracking", "auto")
_DECLINE = ("not_for_us", "dismissed")


def kind_stats(rec_kind: str, cohort: str = None, restaurant_id: int = None, db_path: str = DB_PATH) -> dict:
    """Rates for one kind. With `restaurant_id` it is that restaurant's own
    record (Level 1); with `cohort` the cohort's; otherwise platform-wide."""
    where, args = ["rec_kind=?"], [rec_kind]
    if restaurant_id is not None:
        where.append("restaurant_id=?"); args.append(restaurant_id)
    elif cohort:
        where.append("cohort=?"); args.append(cohort)
    conn = get_conn(db_path)
    try:
        rows = conn.execute(f"SELECT restaurant_id, action, outcome, days_to_effect FROM intel_rec_events "
                            f"WHERE {' AND '.join(where)}", args).fetchall()
    finally:
        conn.close()
    return _summarise(rows, cross=restaurant_id is None)


def _summarise(rows, cross=True) -> dict:
    restaurants = {r["restaurant_id"] for r in rows}
    accepted = sum(1 for r in rows if r["action"] in _ACCEPT)
    declined = sum(1 for r in rows if r["action"] in _DECLINE)
    hidden = sum(1 for r in rows if r["action"] == "hidden")
    answered = accepted + declined + hidden
    measured = [r for r in rows if r["action"] == "measured"]
    improved = sum(1 for r in measured if r["outcome"] == "improved")
    worsened = sum(1 for r in measured if r["outcome"] == "worsened")
    days = [r["days_to_effect"] for r in measured if r["outcome"] == "improved" and r["days_to_effect"] is not None]
    out = {
        "restaurants": len(restaurants), "answered": answered, "accepted": accepted, "declined": declined,
        "hidden": hidden, "acceptance_rate": round(accepted / answered, 3) if answered else None,
        "measured": len(measured), "improved": improved, "worsened": worsened,
        "success_rate": round(improved / len(measured), 3) if measured else None,
        "median_days_to_improvement": percentile(days, 50) if days else None,
    }
    out["success_rate_shrunk"] = shrink(out["success_rate"], len(measured))
    out["acceptance_rate_shrunk"] = shrink(out["acceptance_rate"], answered)
    if cross and not privacy.cohort_ok(len(restaurants)):
        # Below the floor the rates are still computed for the engine's own
        # weighting, but nothing here may be shown as a cohort fact.
        out["available"] = False
        out["reason"] = f"fewer than {privacy.MIN_COHORT} restaurants have answered this kind"
    else:
        out["available"] = True
    return out


def rank_kinds(cohort: str = None, db_path: str = DB_PATH, limit: int = 20) -> list:
    """Every kind with its rates, best measured success first. Cross-
    restaurant: a kind answered by fewer than MIN_COHORT restaurants is
    listed as unavailable with its count only."""
    conn = get_conn(db_path)
    try:
        if cohort:
            rows = conn.execute("SELECT rec_kind, restaurant_id, action, outcome, days_to_effect FROM intel_rec_events "
                                "WHERE cohort=?", (cohort,)).fetchall()
        else:
            rows = conn.execute("SELECT rec_kind, restaurant_id, action, outcome, days_to_effect FROM intel_rec_events").fetchall()
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
        out.append(s)
    out.sort(key=lambda s: (s.get("available", False), s.get("success_rate_shrunk") or 0, s.get("measured") or 0), reverse=True)
    return out[:limit]


def platform_totals(db_path: str = DB_PATH) -> dict:
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT restaurant_id, action, outcome, days_to_effect FROM intel_rec_events").fetchall()
    finally:
        conn.close()
    return _summarise(rows)

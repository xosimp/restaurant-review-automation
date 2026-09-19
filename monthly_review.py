"""
monthly_review.py — the month, read the way an owner reads a P&L.

The monthly email counted reviews. An owner closing a month asks four
questions instead: did the business make more or less money than last
month, what moved, what did the changes I made actually do, and what should
I fix next month. All four are already measured somewhere in this product —
prime cost (food_cost_intelligence), labour and sales (metrics), results
(outcomes), goals (goals), and the ranked money (business_intelligence) —
and nothing joined them.

DETERMINISTIC. Every figure is measured over a named window and compared
with the month before. A metric that cannot be measured for either month is
reported as unknown, never as a change of zero: "flat" and "we didn't sync
your sales" are different sentences, and an owner who is told the first when
the second is true stops trusting the rest.
"""
import calendar
import logging
from datetime import date

import metrics

log = logging.getLogger(__name__)

# The month's headline metrics, in the order an operator reads them.
HEADLINE_METRICS = ("sales", "labor_pct", "food_cost_pct", "avg_rating")


def month_bounds(day):
    """(first, last) of the month `day` falls in."""
    first = day.replace(day=1)
    return first, first.replace(day=calendar.monthrange(first.year, first.month)[1])


def _safe(fn, *a, **k):
    try:
        return fn(*a, **k)
    except Exception as e:
        log.warning("monthly_review: %s failed: %s", getattr(fn, "__name__", fn), e)
        return None


def build(restaurant_id, today=None, restaurant=None, db_path=None):
    """The month that just ended: {"month", "metrics", "results", "goals",
    "priorities", "prime_cost"}. `today` is any day in the month AFTER the
    one being reviewed (the email goes out on the 1st)."""
    from models import get_restaurant, DB_PATH
    db_path = db_path or DB_PATH
    today = today or date.today()
    restaurant = restaurant or get_restaurant(restaurant_id)
    last_start, last_end = month_bounds((today.replace(day=1) - __import__("datetime").timedelta(days=1)))
    prev_start, prev_end = month_bounds(last_start - __import__("datetime").timedelta(days=1))

    rows = []
    for key in HEADLINE_METRICS:
        if key in ("food_cost_pct",) and not getattr(restaurant, "module_inventory", 0):
            continue
        if key in ("labor_pct", "sales") and not getattr(restaurant, "module_labor", 0):
            continue
        if key == "avg_rating" and not getattr(restaurant, "module_reviews", 0):
            continue
        now_v, now_why = metrics.measure(restaurant_id, key, last_start.isoformat(),
                                         last_end.isoformat(), db_path)
        was_v, _ = metrics.measure(restaurant_id, key, prev_start.isoformat(),
                                   prev_end.isoformat(), db_path)
        info = metrics.describe(key)
        row = {"key": key, "label": info["label"], "unit": info["unit"],
               "value": now_v, "previous": was_v, "why": now_why,
               **metrics.compare(key, was_v, now_v)}
        row["monthly_dollars"] = (metrics.monthly_dollars(restaurant_id, key, row["delta"], db_path)
                                  if row["verdict"] in ("improved", "worsened") else None)
        rows.append(row)

    prime = None
    if getattr(restaurant, "module_inventory", 0):
        import food_cost_intelligence as fci
        p = _safe(fci.profitability_projection, restaurant_id, db_path=db_path)
        if p and p.get("available"):
            prime = {"pct": p.get("prime_cost_pct"), "delta": p.get("prime_pct_delta")}

    import outcomes as _outcomes
    results = [r for r in (_safe(_outcomes.list_outcomes, restaurant_id, status="evaluated",
                                 db_path=db_path) or [])
               if last_start.isoformat() <= (r.get("evaluate_on") or "") <= last_end.isoformat()]

    import goals as _goals
    goal_rows = [g for g in (_safe(_goals.progress, restaurant_id, db_path=db_path) or [])
                 if g.get("state") != "unknown"]

    priorities = []
    import business_intelligence as bi
    brief = _safe(bi.executive_brief, restaurant_id, restaurant=restaurant, db_path=db_path) or {}
    for t in ((brief.get("money") or {}).get("ranked") or [])[:3]:
        priorities.append({"label": t.get("label"), "monthly": t.get("monthly"),
                           "monthly_low": t.get("monthly_low"), "monthly_high": t.get("monthly_high"),
                           "is_range": t.get("is_range"), "basis": t.get("basis")})

    return {"month": last_start.strftime("%B %Y"),
            "window": [last_start.isoformat(), last_end.isoformat()],
            "compared_with": prev_start.strftime("%B"),
            "metrics": rows, "prime_cost": prime, "results": results,
            "goals": goal_rows, "priorities": priorities,
            "fix_first": brief.get("fix_first")}


def _fmt(value, unit):
    if value is None:
        return "—"
    if unit == "$":
        return f"${value:,.0f}"
    if unit == "%":
        return f"{value:.1f}%"
    if unit == "★":
        return f"{value:.2f}★"
    return f"{value:g}"


def headline(review) -> str:
    """One sentence naming the month, from the metrics that moved."""
    moved = [m for m in review["metrics"] if m["verdict"] in ("improved", "worsened")]
    if not moved:
        measured = [m for m in review["metrics"] if m["value"] is not None]
        return ("A steady month — nothing moved beyond normal variation."
                if measured else "Not enough data synced last month to read it.")
    better = [m for m in moved if m["verdict"] == "improved"]
    worse = [m for m in moved if m["verdict"] == "worsened"]
    if better and not worse:
        return f"A better month: {better[0]['label'].lower()} improved."
    if worse and not better:
        return f"{worse[0]['label']} went the wrong way last month."
    return f"{better[0]['label']} improved; {worse[0]['label'].lower()} went the other way."


def lines(review):
    """Plain sentences for the email body — one per metric, then results,
    goals and what to fix. Each says what it rests on."""
    out = []
    for m in review["metrics"]:
        if m["value"] is None:
            out.append(f"{m['label']}: not measurable last month ({m['why']}).")
            continue
        base = f"{m['label']}: {_fmt(m['value'], m['unit'])}"
        if m["previous"] is None:
            out.append(base + f" (no {review['compared_with']} figure to compare with).")
            continue
        word = {"improved": "better than", "worsened": "worse than",
                "no_clear_change": "in line with"}.get(m["verdict"], "against")
        money = ""
        if m.get("monthly_dollars"):
            money = f", worth about ${abs(m['monthly_dollars']):,.0f}/month"
        out.append(f"{base}, {word} {review['compared_with']}'s "
                   f"{_fmt(m['previous'], m['unit'])}{money}.")
    if review.get("prime_cost") and review["prime_cost"].get("pct") is not None:
        p = review["prime_cost"]
        drift = (f", {abs(p['delta']):.1f} points {'up' if p['delta'] > 0 else 'down'} on last month"
                 if p.get("delta") else "")
        out.append(f"Prime cost is running at {p['pct']:.1f}% of sales{drift}.")
    return out

"""
monthly_review.py — the month, read the way an owner reads a P&L.

The monthly email counted reviews. An owner closing a month asks four
questions instead: did the business make more or less money than last
month, what moved, what did the changes I made actually do, and what should
I fix next month. All four are already measured somewhere in this product —
prime cost (food_cost_intelligence), labor and sales (metrics), results
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
from datetime import date, timedelta

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


def _months_back(day, n):
    """First day of the month n months before the month `day` is in."""
    y, m = day.year, day.month - n
    while m <= 0:
        y, m = y - 1, m + 12
    return date(y, m, 1)


def build(restaurant_id, today=None, restaurant=None, db_path=None, months=1):
    """The month that just ended: {"month", "metrics", "results", "goals",
    "priorities", "prime_cost"}. `today` is any day in the month AFTER the
    one being reviewed (the email goes out on the 1st).

    months=3 reads the quarter that just ended against the quarter before
    it — the same four questions over a longer window, so a quarter with
    one bad week is not a bad quarter."""
    from models import get_restaurant, DB_PATH
    db_path = db_path or DB_PATH
    today = today or date.today()
    restaurant = restaurant or get_restaurant(restaurant_id)
    months = max(1, int(months or 1))
    last_end = today.replace(day=1) - timedelta(days=1)
    last_start = _months_back(last_end, months - 1)
    prev_end = last_start - timedelta(days=1)
    prev_start = _months_back(prev_end, months - 1)

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
        # The same month a year ago, once there is one. A month-over-month
        # read cannot tell a seasonal dip from a slide; this can. Absent —
        # not zero — until thirteen months of data exist, and the noise
        # band is metrics.compare's, so a wobble is not a verdict.
        try:
            ago_start, ago_end = month_bounds(last_start.replace(year=last_start.year - 1))
            ago_v, _ = metrics.measure(restaurant_id, key, ago_start.isoformat(),
                                       ago_end.isoformat(), db_path)
        except ValueError:
            ago_v = None
        row["year_ago"] = ago_v
        row["yoy"] = metrics.compare(key, ago_v, now_v) if ago_v is not None else None
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

    label = (last_start.strftime("%B %Y") if months == 1
             else f"{last_start.strftime('%B')}–{last_end.strftime('%B %Y')}")
    compared = (prev_start.strftime("%B") if months == 1
                else f"{prev_start.strftime('%B')}–{prev_end.strftime('%B')}")
    return {"month": label, "months": months,
            "window": [last_start.isoformat(), last_end.isoformat()],
            "compared_with": compared,
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


def yoy_clause(m):
    """" — and 1.2 points under the same month last year" or "". Only when
    last year's month could be measured and the move clears the band."""
    y = m.get("yoy")
    if not y or m.get("year_ago") is None or y.get("verdict") not in ("improved", "worsened"):
        return ""
    unit = m.get("unit") or ""
    delta = abs(y["delta"])
    amt = f"${delta:,.0f}" if unit == "$" else (f"{delta:.1f} points" if unit == "%" else f"{delta:g}{unit}")
    word = ("under" if (m["value"] < m["year_ago"]) else "over")
    return f" — {amt} {word} the same month last year"


def lines(review):
    """Plain sentences for the email body — one per metric, then results,
    goals and what to fix. Each says what it rests on."""
    out = []
    for m in review["metrics"]:
        if m["value"] is None:
            span = "last quarter" if review.get("months", 1) > 1 else "last month"
            out.append(f"{m['label']}: not measurable {span} ({m['why']}).")
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
                   f"{_fmt(m['previous'], m['unit'])}{money}{yoy_clause(m)}.")
    if review.get("prime_cost") and review["prime_cost"].get("pct") is not None:
        p = review["prime_cost"]
        drift = (f", {abs(p['delta']):.1f} points {'up' if p['delta'] > 0 else 'down'} on last month"
                 if p.get("delta") else "")
        out.append(f"Prime cost is running at {p['pct']:.1f}% of sales{drift}.")
    return out


def cost_of_waiting(review) -> str:
    """What another month of this costs, in dollars, or "" when nothing
    measured moved far enough to put a number on.

    No email in this product answered "what happens if I ignore this" — the
    one sentence that separates an advisor from a dashboard. It is not a
    new measurement: monthly_dollars is already computed per metric, and
    this states the consequence of leaving it where it is.

    Only ever said about a metric that WORSENED, and only when there is a
    dollar figure behind it. "Nothing got worse" needs no warning, and a
    warning without a number is the generic nudge this is meant to replace.
    """
    worse = [m for m in review["metrics"]
             if m["verdict"] == "worsened" and m.get("monthly_dollars")]
    if not worse:
        return ""
    lead = max(worse, key=lambda m: abs(m["monthly_dollars"]))
    return (f"If {lead['label'].lower()} stays where it is, that is about "
            f"${abs(lead['monthly_dollars']):,.0f} a month — roughly "
            f"${abs(lead['monthly_dollars']) * 12:,.0f} over a year.")

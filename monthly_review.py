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
import review_common
from review_common import fmt as _fmt, cost_of_waiting  # shared with the other review

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

    # Prime cost for the CLOSED month under review, measured the way the
    # frozen mid-month projection is scored (food cost % + labor % over the
    # window, metrics.measure) — never profitability_projection, which reads
    # the CURRENT month to date: on the 1st it was withheld, mid-month it
    # reported this month under last month's heading, and the in-app screen
    # and the email disagreed depending on the day they were built (CA4 F12).
    prime = None
    if getattr(restaurant, "module_inventory", 0):
        prime = _safe(_closed_prime, restaurant_id, last_start, last_end, prev_start, prev_end, db_path)

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
    # One "no" everywhere: a priority answered on any surface is not listed.
    silenced = review_common.silenced(restaurant_id, db_path)
    for t in [t for t in ((brief.get("money") or {}).get("ranked") or []) if t.get("key") not in silenced][:3]:
        priorities.append({"key": t.get("key"), "label": t.get("label"), "monthly": t.get("monthly"),
                           "monthly_low": t.get("monthly_low"), "monthly_high": t.get("monthly_high"),
                           "is_range": t.get("is_range"), "basis": t.get("basis")})
    # Nothing is presented here: the month-ready push, the subject line and
    # the web view all build this, and none of them is the monthly email.
    # emails._monthly_review_sections stages what it renders; the send
    # presents it once delivered (rec_delivery).

    label = (last_start.strftime("%B %Y") if months == 1
             else f"{last_start.strftime('%B')}–{last_end.strftime('%B %Y')}")
    compared = (prev_start.strftime("%B") if months == 1
                else f"{prev_start.strftime('%B')}–{prev_end.strftime('%B')}")
    return {"month": label, "months": months,
            "window": [last_start.isoformat(), last_end.isoformat()],
            "compared_with": compared,
            "metrics": rows, "prime_cost": prime, "results": results,
            "plan": _plan_score(restaurant_id, last_start, last_end, db_path),
            "goals": goal_rows, "priorities": priorities,
            "fix_first": brief.get("fix_first")}


def _closed_prime(restaurant_id, start, end, prev_start, prev_end, db_path):
    """{"pct", "previous", "delta", "claim_kind": "measured", "window"} for
    the closed window, or None when either half of it cannot be measured —
    never a prime cost with one component assumed."""
    def _prime(s, e):
        fc, _ = metrics.measure(restaurant_id, "food_cost_pct", s.isoformat(), e.isoformat(), db_path)
        lb, _ = metrics.measure(restaurant_id, "labor_pct", s.isoformat(), e.isoformat(), db_path)
        return round(float(fc) + float(lb), 1) if fc is not None and lb is not None else None
    now = _prime(start, end)
    if now is None:
        return None
    was = _prime(prev_start, prev_end)
    return {"pct": now, "previous": was,
            "delta": round(now - was, 1) if was is not None else None,
            "claim_kind": "measured", "window": [start.isoformat(), end.isoformat()],
            "basis": "food cost % + labor % over the closed month, measured"}


def _plan_score(restaurant_id, start, end, db_path):
    """The Monday plans' own receipt: how many of the actions the agent
    filed in the month were resolved. Absent when none were filed."""
    try:
        from models import get_conn
        conn = get_conn(db_path)
        try:
            rows = conn.execute(
                "SELECT status FROM ops_issues WHERE restaurant_id=? AND kind='plan' "
                "AND substr(created_at,1,10) BETWEEN ? AND ?", (restaurant_id, start.isoformat(), end.isoformat())).fetchall()
        finally:
            conn.close()
    except Exception:
        return None
    if not rows:
        return None
    done = sum(1 for r in rows if (r["status"] or "") == "resolved")
    return {"filed": len(rows), "done": done}


def headline(review) -> str:
    """One sentence naming the month, from the metrics that moved."""
    return review_common.headline(review, "month")

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
            from weekly_review import money_word
            money = f", {money_word(m)} ${abs(m['monthly_dollars']):,.0f}/month"
        out.append(f"{base}, {word} {review['compared_with']}'s "
                   f"{_fmt(m['previous'], m['unit'])}{money}{yoy_clause(m)}.")
    if review.get("prime_cost") and review["prime_cost"].get("pct") is not None:
        p = review["prime_cost"]
        span = "last quarter" if review.get("months", 1) > 1 else "last month"
        drift = (f", {abs(p['delta']):.1f} points {'up' if p['delta'] > 0 else 'down'} on "
                 f"{review['compared_with']}" if p.get("delta") else "")
        # Measured over the closed window, and said so — "is running at"
        # read as this month's run rate, which is what it used to be.
        out.append(f"Prime cost (food cost + labor) was {p['pct']:.1f}% of sales {span}{drift}, measured.")
    plan = review.get("plan")
    if plan:
        span = "last quarter" if review.get("months", 1) > 1 else "last month"
        out.append(f"You did {plan['done']} of the {plan['filed']} actions the Monday plans filed {span}.")
    return out


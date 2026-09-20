"""
weekly_review.py — the week, read the way an owner reads a week.

The weekly digest counted reviews. It has done that since it shipped, and
it is the one thing Cavnar AI sends every single week — so it is also the
clearest statement the product makes about what it thinks matters. An owner
opening it learned how many reviews arrived and what their average star
rating was, and nothing about whether the week made or lost money.

The monthly email was rebuilt to answer four questions instead (see
monthly_review.py). This is the same four questions over seven days, so the
weekly and the monthly read as one voice rather than two products:

  did the week make more or less money than the week before,
  what moved,
  what did the changes I made actually do,
  what is worth my time next week.

DETERMINISTIC. Same rules as monthly_review: every figure is measured over a
named window and compared with the window before, and a metric that cannot
be measured for either week is reported as unknown, never as a change of
zero. Seven days is a short window and a noisy one, so this says so — a
single big party or one closed day moves a week in a way it cannot move a
month, and an owner told "sales down 12%" without that caveat learns to
distrust the number rather than the noise.
"""
import logging
from datetime import date, timedelta

import metrics

log = logging.getLogger(__name__)

# The same four the month leads with, in the same order, so a reader moving
# between the weekly and the monthly is reading one report at two cadences.
HEADLINE_METRICS = ("sales", "labor_pct", "food_cost_pct", "avg_rating")

# A week is a small sample. Below this many days of data on either side there
# is nothing honest to compare.
MIN_DAYS = 5

WINDOW_CAVEAT = ("A week is a short window — one closed day or one large party "
                 "moves it in a way it cannot move a month.")


def week_bounds(day):
    """(monday, sunday) of the ISO week `day` falls in."""
    monday = day - timedelta(days=day.weekday())
    return monday, monday + timedelta(days=6)


def _safe(fn, *a, **k):
    try:
        return fn(*a, **k)
    except Exception as e:
        log.warning("weekly_review: %s failed: %s", getattr(fn, "__name__", fn), e)
        return None


def build(restaurant_id, today=None, restaurant=None, db_path=None):
    """The week that just ended: {"week", "metrics", "results", "priorities",
    "fix_first"}. `today` is any day in the week AFTER the one being
    reviewed — the digest goes out on the owner's chosen day."""
    from models import get_restaurant, DB_PATH
    db_path = db_path or DB_PATH
    today = today or date.today()
    restaurant = restaurant or get_restaurant(restaurant_id)
    last_start, last_end = week_bounds(today - timedelta(days=7))
    prev_start, prev_end = week_bounds(last_start - timedelta(days=7))

    rows = []
    for key in HEADLINE_METRICS:
        if key == "food_cost_pct" and not getattr(restaurant, "module_inventory", 0):
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

    import outcomes as _outcomes
    results = [r for r in (_safe(_outcomes.list_outcomes, restaurant_id, status="evaluated",
                                 db_path=db_path) or [])
               if last_start.isoformat() <= (r.get("evaluate_on") or "") <= last_end.isoformat()]

    priorities, fix_first = [], None
    import business_intelligence as bi
    brief = _safe(bi.executive_brief, restaurant_id, restaurant=restaurant, db_path=db_path) or {}
    for t in ((brief.get("money") or {}).get("ranked") or [])[:2]:
        priorities.append({"label": t.get("label"), "monthly": t.get("monthly"),
                           "monthly_low": t.get("monthly_low"),
                           "monthly_high": t.get("monthly_high"),
                           "is_range": t.get("is_range")})
    fix_first = brief.get("fix_first")

    return {"week": f"{last_start.strftime('%b %-d')}–{last_end.strftime('%b %-d')}",
            "window": [last_start.isoformat(), last_end.isoformat()],
            "compared_with": f"{prev_start.strftime('%b %-d')}–{prev_end.strftime('%b %-d')}",
            "metrics": rows, "results": results, "priorities": priorities,
            "fix_first": fix_first}


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
    """One sentence naming the week, from the metrics that moved."""
    moved = [m for m in review["metrics"] if m["verdict"] in ("improved", "worsened")]
    if not moved:
        measured = [m for m in review["metrics"] if m["value"] is not None]
        return ("A steady week — nothing moved beyond normal variation."
                if measured else "Not enough synced last week to read it.")
    better = [m for m in moved if m["verdict"] == "improved"]
    worse = [m for m in moved if m["verdict"] == "worsened"]
    if better and not worse:
        return f"A better week: {better[0]['label'].lower()} improved."
    if worse and not better:
        return f"{worse[0]['label']} went the wrong way last week."
    return f"{better[0]['label']} improved; {worse[0]['label'].lower()} went the other way."


def lines(review):
    """Plain sentences for the email body — one per metric. Each says what
    it rests on, and none of them invents a figure it could not measure."""
    out = []
    for m in review["metrics"]:
        if m["value"] is None:
            out.append(f"{m['label']}: not measurable last week ({m['why']}).")
            continue
        base = f"{m['label']}: {_fmt(m['value'], m['unit'])}"
        if m["previous"] is None:
            out.append(base + f" (no {review['compared_with']} figure to compare with).")
            continue
        word = {"improved": "better than", "worsened": "worse than",
                "no_clear_change": "in line with"}.get(m["verdict"], "against")
        money = ""
        if m.get("monthly_dollars"):
            money = f", worth about ${abs(m['monthly_dollars']):,.0f}/month if it holds"
        out.append(f"{base}, {word} {review['compared_with']}'s "
                   f"{_fmt(m['previous'], m['unit'])}{money}.")
    return out


def cost_of_waiting(review) -> str:
    """What another week of this costs, in dollars, or "" when nothing
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

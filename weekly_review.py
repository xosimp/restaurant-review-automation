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
import review_common
from review_common import fmt as _fmt, cost_of_waiting  # shared with the other review

log = logging.getLogger(__name__)

# The same four the month leads with, in the same order, so a reader moving
# between the weekly and the monthly is reading one report at two cadences.
HEADLINE_METRICS = ("sales", "labor_pct", "food_cost_pct", "avg_rating")

WINDOW_CAVEAT = ("A week is a short window — one closed day or one large party "
                 "moves it in a way it cannot move a month.")

WINDOW_DAYS = 7


def band_scale(key, window_days=WINDOW_DAYS) -> float:
    """How much wider a metric's noise band is over `window_days` than over
    the window it was set for (metrics' default_window_days, 28-30 days for
    the headline four).

    metrics.compare's bands are the smallest move that counts over the
    metric's own default window, and they were applied unscaled to 7-day
    windows (CA1 H12/red flag 21): a week's mean wobbles about twice as much
    as a four-week one, so a week-on-week "better than" cleared a band set
    for a month. The spread of a mean scales with 1/sqrt(days), so the band
    widens by sqrt(default / window). Never narrower than the metric's own
    band. When per-restaurant bands land (metrics, group F) they pass
    through the same `band_scale` argument."""
    try:
        default = float(metrics.describe(key).get("default_window_days") or window_days)
    except Exception:
        return 1.0
    return round(max(1.0, (default / float(window_days)) ** 0.5), 3)


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
               # The band scaled for a 7-day window (band_scale).
               **metrics.compare(key, was_v, now_v, band_scale=band_scale(key))}
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
    # One "no" everywhere: a priority the owner answered on any surface is
    # not a priority here (fix_first is already filtered by pick_one_thing).
    import review_common as _rc
    silenced = _rc.silenced(restaurant_id, db_path)
    for t in [t for t in ((brief.get("money") or {}).get("ranked") or []) if t.get("key") not in silenced][:2]:
        priorities.append({"key": t.get("key"), "label": t.get("label"), "monthly": t.get("monthly"),
                           "monthly_low": t.get("monthly_low"),
                           "monthly_high": t.get("monthly_high"),
                           "is_range": t.get("is_range")})
    fix_first = brief.get("fix_first")
    # Nothing is presented here: a build is not a delivery. The digest's
    # preheader and fallback headline build this too, and neither shows a
    # priority. emails._weekly_review_sections stages what it renders and
    # the digest send presents it once the email went out (rec_delivery).

    # Dates an owner reads are M/D/YY (DESIGN_SYSTEM.md → Dates and times).
    from time_utils import mdy_range
    return {"week": mdy_range(last_start, last_end),
            "window": [last_start.isoformat(), last_end.isoformat()],
            "compared_with": mdy_range(prev_start, prev_end),
            "metrics": rows, "results": results, "priorities": priorities,
            "fix_first": fix_first}


def headline(review) -> str:
    """One sentence naming the week, from the metrics that moved."""
    return review_common.headline(review, "week")

def money_word(m) -> str:
    """How a move's monthly dollars are introduced: "worth about" only for
    a move the right way; a move the wrong way is "costing about" — "worse
    than last week, worth about $9,082/month" read a loss as a gain
    (re-audit C12)."""
    return {"improved": "worth about", "worsened": "costing about"}.get(m.get("verdict"), "about")


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
            money = f", {money_word(m)} ${abs(m['monthly_dollars']):,.0f}/month if it holds"
        out.append(f"{base}, {word} {review['compared_with']}'s "
                   f"{_fmt(m['previous'], m['unit'])}{money}.")
    return out


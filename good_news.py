"""
good_news.py — the things that got better, that nobody was told about.

Every detector in this product looks for trouble. `notify.py` fires on 1-star
reviews, labor over target, waste, price spikes and visibility drops;
`issues.py` tracks what is unresolved; `business_intelligence.money_at_stake`
ranks what is being lost. The delight audit found the consequence: a
restaurant that quietly improves is told nothing, and the only notification
in the product about money already made
(`strategy_jobs._tell_owners_what_worked`) requires the owner to have pressed
"Track this" on a recommendation first.

So an owner whose Fridays stopped generating complaints, whose labor has
been under target for six weeks, or who just had the best month of sales
since they signed up, hears exactly the same from Cavnar AI as one whose
numbers are flat. They conclude the improvement was theirs and the software
was incidental — which, for that improvement, it may well have been. It is
still the thing a partner would say out loud.

Three detectors, all deterministic, all reading `metrics.py` so a record and
a goal and an outcome all mean the same number:

  records()   the best window this restaurant has had, over a metric that
              has enough history for "best" to mean anything.
  streaks()   consecutive periods meeting a target the owner themselves set.
  stopped()   a complaint category that was a real share of reviews and is
              now not.

FIVE HONESTY RULES, enforced here rather than left to wording. These matter
more for good news than for bad: an owner who is congratulated on a wobble
learns that the congratulations are worthless, and then the real one lands
on deaf ears.

  1. A RECORD NEEDS HISTORY. Below MIN_PRIOR_PERIODS comparable windows
     there is no "best ever" to claim — the first good month of a new
     account is just the first month.

  2. A RECORD MUST BEAT THE RUNNER-UP BY MORE THAN NOISE. metrics.compare
     already knows each metric's noise band. Being 0.1 points better than
     the previous best is not a record, it is the same number twice.

  3. UNKNOWN IS NOT A WIN. A window that cannot be measured is skipped, never
     treated as zero. `metrics.measure` returns None for exactly this and the
     whole file respects it.

  4. A STREAK IS AGAINST THE OWNER'S OWN TARGET, never an industry number.
     "Six weeks under 30%" is a fact about a target they set. "Six weeks
     better than average" is a comparison they did not ask for.

  5. NOTHING HERE CLAIMS CAUSE. These detect that a number moved, not that
     Cavnar AI moved it. `value_delivered.py` is the file that answers what
     the product was worth, and it counts only measured outcomes. A record
     is news, not a receipt — CAVEAT says so and every renderer carries it.
"""
import logging
from datetime import date, timedelta

import metrics
from models import DB_PATH

log = logging.getLogger(__name__)

CAVEAT = ("Measured from your own numbers. A record says the number moved, "
          "not what moved it.")

# Below this many prior comparable windows, "best ever" means "best of two".
MIN_PRIOR_PERIODS = 5

# Per-metric floors where the default is wrong in one direction. Reviews
# arrive from day one on every account, and a rating window is 30 days —
# five priors meant the first "best rating" could not land before month
# six, which the retention audit put squarely in the churn window. Three
# priors on rating is a quarter of history, and the noise band
# (metrics.compare) still refuses a tie or a wobble.
MIN_PRIOR_BY_METRIC = {"avg_rating": 3}

# How far back to look for a record, in windows. Six of a 28-day window is
# about half a year, which is as far back as most of these accounts go.
LOOKBACK_PERIODS = 12

# Metrics worth telling an owner about when they hit a record. Deliberately
# not every metric in the registry: "best comp rate in six months" reads as
# an accusation about the months before it (see metrics._loss_rate's own
# note on the same risk), and complaint share is handled by stopped() with
# a category attached rather than as a bare percentage.
RECORD_METRICS = ("sales", "labor_pct", "food_cost_pct", "avg_rating", "weekly_waste")

# A streak is counted in whole weeks against a target on `restaurants`.
# (metric key, restaurant column, "under" | "over")
STREAK_TARGETS = (
    ("labor_pct", "labor_target_pct", "under"),
    ("food_cost_pct", "food_cost_target", "under"),
)

# A streak is only worth mentioning once it is longer than a run that
# happens by chance. Three weeks is the floor; below it this says nothing.
MIN_STREAK_WEEKS = 3
MAX_STREAK_WEEKS = 26

# A complaint category has to have been a real share of reviews before it
# can be said to have stopped. Same floor review_intelligence uses to call
# something a cluster at all.
COMPLAINT_WAS_SHARE = 8.0     # percent of all reviews in the "before" window
COMPLAINT_NOW_SHARE = 2.0     # and under this in the "after" window

# One vocabulary for how a category is written, shared with
# business_intelligence and anything else that renders one. A second copy
# here is how "wait_time" ends up as "wait times" on one screen and "wait
# time" on the next.
from analyser import CATEGORIES as COMPLAINT_CATEGORIES, category_label


def _safe(fn, *a, **k):
    try:
        return fn(*a, **k)
    except Exception as e:
        log.warning("good_news: %s failed: %s", getattr(fn, "__name__", fn), e)
        return None


def _fmt(value, unit):
    if value is None:
        return "—"
    if unit == "$":
        return f"${value:,.0f}"
    if unit == "★":
        return f"{value:.2f}★"
    return f"{value:g}{unit}"


# ── records ──────────────────────────────────────────────────────────────────

def _windows(key, today, periods):
    """Consecutive non-overlapping windows of this metric's own length,
    newest first. Non-overlapping matters: two windows sharing days share
    the days' numbers, and "best ever" against a window that contains most
    of the same data is not a comparison at all."""
    length = metrics.describe(key)["default_window_days"]
    out = []
    end = today
    for _ in range(periods):
        start = end - timedelta(days=length - 1)
        out.append((start, end))
        end = start - timedelta(days=1)
    return out


def records(restaurant_id, today=None, db_path=DB_PATH, keys=None,
            restaurant=None, denied_modules=None):
    """Metrics sitting at their best measured window, with the runner-up.

    Returns a list of dicts, best first. Empty is the common and correct
    outcome — most weeks nothing is a record, and inventing one is how the
    real record stops meaning anything.
    """
    today = today or date.today()
    denied = set(denied_modules or ())
    if restaurant is None:
        from models import get_restaurant
        restaurant = _safe(get_restaurant, restaurant_id)

    out = []
    for key in (keys or RECORD_METRICS):
        module = _module_of(key)
        if module in denied:
            continue
        if restaurant is not None and not _module_on(restaurant, module):
            continue
        info = metrics.describe(key)
        measured = []
        for start, end in _windows(key, today, LOOKBACK_PERIODS):
            value, detail = metrics.measure(restaurant_id, key, start.isoformat(),
                                            end.isoformat(), db_path)
            # Rule 3: an unmeasurable window is skipped, never zeroed.
            if value is None:
                continue
            measured.append({"value": value, "start": start.isoformat(),
                             "end": end.isoformat(), "detail": detail})

        current = measured[0] if measured else None
        prior = measured[1:]
        # Rule 1: no history, no record.
        if not current or len(prior) < MIN_PRIOR_BY_METRIC.get(key, MIN_PRIOR_PERIODS):
            continue

        better = (lambda a, b: a < b) if info["lower_is_better"] else (lambda a, b: a > b)
        if not all(better(current["value"], p["value"]) for p in prior):
            continue
        runner_up = min(prior, key=lambda p: abs(p["value"] - current["value"]))
        # Rule 2: beating the runner-up by less than the metric's own noise
        # band is the same number twice. metrics.compare owns that band.
        verdict = metrics.compare(key, runner_up["value"], current["value"])
        if verdict["verdict"] != "improved":
            continue

        out.append({
            "kind": "record",
            "key": f"record:{key}:{current['end']}",
            "metric": key,
            "label": info["label"],
            "unit": info["unit"],
            "module": module,
            "value": current["value"],
            "previous_best": runner_up["value"],
            "window": {"start": current["start"], "end": current["end"]},
            "periods_compared": len(measured),
            "delta": verdict["delta"],
            "dollars_monthly": metrics.monthly_dollars(restaurant_id, key,
                                                       verdict["delta"], db_path),
            "detail": current["detail"],
            "headline": (f"Best {info['label'].lower()} in "
                         f"{len(measured)} periods: {_fmt(current['value'], info['unit'])}"),
            "summary": (f"{info['label']} came in at {_fmt(current['value'], info['unit'])} "
                        f"over the {current['detail']} ending {current['end']} — better than "
                        f"any of the {len(prior)} periods before it "
                        f"(previous best {_fmt(runner_up['value'], info['unit'])})."),
            "caveat": CAVEAT,
        })
    out.sort(key=lambda r: abs(r.get("dollars_monthly") or 0), reverse=True)
    return out


# ── streaks ──────────────────────────────────────────────────────────────────

def _week_bounds(day):
    monday = day - timedelta(days=day.weekday())
    return monday, monday + timedelta(days=6)


def streaks(restaurant_id, today=None, db_path=DB_PATH, restaurant=None,
            denied_modules=None):
    """Consecutive completed weeks meeting a target the owner set.

    The current week is excluded — a Tuesday is not a week, and counting it
    makes the streak break every Monday.
    """
    today = today or date.today()
    denied = set(denied_modules or ())
    if restaurant is None:
        from models import get_restaurant
        restaurant = _safe(get_restaurant, restaurant_id)
    if restaurant is None:
        return []

    out = []
    for key, column, direction in STREAK_TARGETS:
        module = _module_of(key)
        if module in denied or not _module_on(restaurant, module):
            continue
        target = getattr(restaurant, column, None)
        try:
            target = float(target)
        except (TypeError, ValueError):
            continue
        if not target:
            continue

        info = metrics.describe(key)
        # Last completed week back, one week at a time.
        cursor = _week_bounds(today)[0] - timedelta(days=1)
        weeks, best = 0, None
        for _ in range(MAX_STREAK_WEEKS):
            start, end = _week_bounds(cursor)
            value, _detail = metrics.measure(restaurant_id, key, start.isoformat(),
                                             end.isoformat(), db_path)
            # Rule 3 again: an unmeasurable week does not extend a streak and
            # does not break one either — it ends the count, because a streak
            # with a hole in it is not a streak.
            if value is None:
                break
            met = value <= target if direction == "under" else value >= target
            if not met:
                break
            weeks += 1
            if best is None or (value < best if direction == "under" else value > best):
                best = value
            cursor = start - timedelta(days=1)

        if weeks < MIN_STREAK_WEEKS:
            continue
        out.append({
            "kind": "streak",
            "key": f"streak:{key}:{weeks}",
            "metric": key,
            "label": info["label"],
            "unit": info["unit"],
            "module": module,
            "weeks": weeks,
            "target": target,
            "best": best,
            "headline": f"{weeks} weeks running under your {info['label'].lower()} target",
            "summary": (f"{info['label']} has come in at or under your "
                        f"{_fmt(target, info['unit'])} target for {weeks} weeks straight — "
                        f"best week {_fmt(best, info['unit'])}."),
            "caveat": CAVEAT,
        })
    out.sort(key=lambda s: s["weeks"], reverse=True)
    return out


# ── things that stopped happening ────────────────────────────────────────────

def stopped(restaurant_id, today=None, db_path=DB_PATH, restaurant=None,
            denied_modules=None):
    """Complaint categories that were a real share of reviews and now aren't.

    This is the other half of `outcomes.py`. Outcomes measures what the owner
    deliberately committed to; this measures what got better without anyone
    pressing a button — which, for a restaurant that fixed something because
    they read it in a brief, is most of it.
    """
    today = today or date.today()
    denied = set(denied_modules or ())
    if "reviews" in denied:
        return []
    if restaurant is None:
        from models import get_restaurant
        restaurant = _safe(get_restaurant, restaurant_id)
    if restaurant is not None and not _module_on(restaurant, "reviews"):
        return []

    window = metrics.describe("complaints")["default_window_days"]
    now_start = today - timedelta(days=window - 1)
    was_end = now_start - timedelta(days=1)
    was_start = was_end - timedelta(days=window - 1)

    out = []
    for category in COMPLAINT_CATEGORIES:
        key = f"complaints:{category}"
        now_v, now_detail = metrics.measure(restaurant_id, key, now_start.isoformat(),
                                            today.isoformat(), db_path)
        was_v, was_detail = metrics.measure(restaurant_id, key, was_start.isoformat(),
                                            was_end.isoformat(), db_path)
        # Rule 3: either window unmeasurable — under five reviews — says
        # nothing about whether anything stopped.
        if now_v is None or was_v is None:
            continue
        if was_v < COMPLAINT_WAS_SHARE or now_v > COMPLAINT_NOW_SHARE:
            continue
        verdict = metrics.compare(key, was_v, now_v)
        if verdict["verdict"] != "improved":
            continue
        label = category_label(category)
        out.append({
            "kind": "stopped",
            "key": f"stopped:{category}:{today.isoformat()}",
            "metric": key,
            "category": category,
            "label": label,
            "module": "reviews",
            "was": was_v,
            "now": now_v,
            "headline": f"Guests stopped complaining about {label}",
            "summary": (f"{label.capitalize()} was in {was_v:g}% of reviews "
                        f"({was_detail}) and is in {now_v:g}% now ({now_detail})."),
            "caveat": CAVEAT,
        })
    out.sort(key=lambda s: s["was"] - s["now"], reverse=True)
    return out


# ── module gating ────────────────────────────────────────────────────────────

_METRIC_MODULE = {
    "labor_pct": "labor", "sales": "labor", "weekday_sales": "labor",
    "food_cost_pct": "inventory", "weekly_waste": "inventory",
    "avg_rating": "reviews", "complaints": "reviews",
    "comp_rate": "labor", "void_rate": "labor",
}

_MODULE_FLAG = {"labor": "module_labor", "inventory": "module_inventory",
                "reviews": "module_reviews", "marketing": "module_marketing"}


def _module_of(key):
    base, _ = metrics.parse(key)
    return _METRIC_MODULE.get(base)


def _module_on(restaurant, module):
    flag = _MODULE_FLAG.get(module)
    return True if not flag else bool(getattr(restaurant, flag, 0))


# ── the one call everything else makes ───────────────────────────────────────

# One full pass is ~170 short-lived database reads: five metrics over twelve
# windows, two metrics over up to twenty-six weeks, and eight complaint
# categories over two windows each. That is fine once. It is not fine once
# per recipient — morning_brief.deliver() rebuilds the whole brief for every
# login that gets it, because a manager's brief is built from what THEY may
# see, so a restaurant with three logins recomputed this three times, and
# the scheduler does that for the whole fleet every morning.
#
# Keyed on what actually changes the answer: the restaurant, the day, and
# the modules this viewer is denied. The denied set is part of the key on
# purpose — without it a manager would read the owner's cached answer,
# which is the whole permission boundary defeated by a dictionary.
#
# A short TTL rather than a day-long one so /api/good-news still reflects a
# fresh sync within a couple of minutes.
#
# This is process-local, and deliberately NOT in the same category as the
# three dicts CLAUDE.md names as blocking `--workers > 1`
# (auth_routes._login_attempts, client_api._order_send_last,
# ai_utils._ai_call_log). Those are LIMITS: a second worker gets its own
# copy and silently doubles a rate limit, and the first is a security
# control. This is a pure cache — a second worker would start cold and
# compute the same answer. It makes that constraint no worse.
_NEWS_CACHE = {}
_NEWS_TTL = 120
_NEWS_MAX = 400


def all_good_news(restaurant_id, today=None, db_path=DB_PATH, restaurant=None,
                  denied_modules=None, limit=None):
    """Every positive signal, best first. Records outrank streaks outrank
    stopped-complaints, because a record is news and a streak is a state."""
    import time as _time
    key = (restaurant_id, str(today or date.today()), db_path,
           frozenset(denied_modules or ()))
    hit = _NEWS_CACHE.get(key)
    if hit and _time.time() - hit[0] < _NEWS_TTL:
        items = hit[1]
        return items[:limit] if limit else list(items)

    kwargs = {"today": today, "db_path": db_path, "restaurant": restaurant,
              "denied_modules": denied_modules}
    items = ((_safe(records, restaurant_id, **kwargs) or [])
             + (_safe(streaks, restaurant_id, **kwargs) or [])
             + (_safe(stopped, restaurant_id, **kwargs) or []))
    if len(_NEWS_CACHE) >= _NEWS_MAX:
        _NEWS_CACHE.clear()
    _NEWS_CACHE[key] = (_time.time(), list(items))
    # A copy either way. Handing back the cached list itself means a caller
    # that sorts or trims it in place corrupts what the next reader sees.
    return items[:limit] if limit else list(items)

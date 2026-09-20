"""
outcomes.py — did the recommendation work?

Every module in this product recommends things. Nothing ever checked whether
the owner did one and whether the number it was aimed at moved. That left the
product unable to say the one sentence that justifies it — "you trimmed Monday
lunch, labor moved 1.8 points, that's about $410 a month" — and left the
assistant unable to learn which of its suggestions work for this restaurant.

The shape is deliberately simple:

  record()    the owner commits to a recommendation. The metric it targets is
              measured over the window BEFORE today and stored.
  evaluate()  once the same length of window has passed AFTER, re-measure,
              compare with metrics.compare (which respects each metric's
              noise band), and store the verdict.

Two honesty rules, both enforced here rather than left to wording:

  BEFORE-AND-AFTER IS NOT CAUSE. A verdict says the number moved while the
  change was in place. It never says the change moved it — a busy month, a
  menu change or a new hire all move the same numbers. Every verdict carries
  that caveat to whatever renders it.

  UNKNOWN STAYS UNKNOWN. If either window cannot be measured the verdict is
  "unknown", not a zero delta and not a failure.
"""
from datetime import date, timedelta

import metrics
from models import get_conn, DB_PATH

CAUSATION_CAVEAT = ("Measured before and after, not proven cause: other changes in "
                    "the same weeks move the same number.")


def _row(r):
    d = dict(r)
    info = metrics.describe(d["metric"]) if metrics.known(d["metric"]) else {}
    d["metric_label"] = info.get("label", d["metric"])
    d["unit"] = info.get("unit")
    return d


def record(restaurant_id, source, source_key, title, metric, user_id=None,
           window_days=None, db_path=DB_PATH, today=None):
    """Start tracking one recommendation the owner has committed to.

    Idempotent on (restaurant, source_key) while tracking — committing to the
    same fix twice returns the existing tracker rather than resetting its
    baseline, which would quietly erase the improvement already made.
    """
    if not metrics.known(metric):
        raise ValueError(f"unknown metric {metric}")
    today = today or date.today()
    info = metrics.describe(metric)
    window = int(window_days or info["default_window_days"])

    conn = get_conn(db_path)
    try:
        existing = conn.execute(
            "SELECT * FROM recommendation_outcomes WHERE restaurant_id=? AND source_key=? "
            "AND status='tracking'", (restaurant_id, source_key)).fetchone()
        if existing:
            return _row(existing)
    finally:
        conn.close()

    # Baseline ends YESTERDAY: today is part of the "after", and a baseline
    # that includes the day the change started is contaminated by it.
    base_end = today - timedelta(days=1)
    base_start = base_end - timedelta(days=window - 1)
    value, detail = metrics.measure(restaurant_id, metric, base_start.isoformat(),
                                    base_end.isoformat(), db_path)
    evaluate_on = today + timedelta(days=window)

    conn = get_conn(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO recommendation_outcomes (restaurant_id, source, source_key, title, metric, "
            "baseline_value, baseline_start, baseline_end, baseline_detail, started_on, evaluate_on, "
            "status, created_by) VALUES (?,?,?,?,?,?,?,?,?,?,?, 'tracking', ?)",
            (restaurant_id, source, source_key, title[:200], metric, value,
             base_start.isoformat(), base_end.isoformat(), detail, today.isoformat(),
             evaluate_on.isoformat(), user_id))
        conn.commit()
        row = conn.execute("SELECT * FROM recommendation_outcomes WHERE id=?",
                           (cur.lastrowid,)).fetchone()
    finally:
        conn.close()
    return _row(row)


def evaluate(outcome_id, db_path=DB_PATH, today=None):
    """Re-measure one tracker whose window has closed and store the verdict."""
    today = today or date.today()
    conn = get_conn(db_path)
    try:
        r = conn.execute("SELECT * FROM recommendation_outcomes WHERE id=?", (outcome_id,)).fetchone()
    finally:
        conn.close()
    if not r or r["status"] != "tracking":
        return None
    started = date.fromisoformat(r["started_on"])
    ev = date.fromisoformat(r["evaluate_on"])
    if today < ev:
        return None
    after_start, after_end = started, ev - timedelta(days=1)
    after, _detail = metrics.measure(r["restaurant_id"], r["metric"], after_start.isoformat(),
                                     after_end.isoformat(), db_path)
    cmp = metrics.compare(r["metric"], r["baseline_value"], after)
    dollars = (metrics.monthly_dollars(r["restaurant_id"], r["metric"], cmp["delta"], db_path)
               if cmp["verdict"] in ("improved", "worsened") else None)
    conn = get_conn(db_path)
    try:
        conn.execute(
            "UPDATE recommendation_outcomes SET after_value=?, after_start=?, after_end=?, verdict=?, "
            "delta=?, dollars_monthly=?, status='evaluated' WHERE id=? AND status='tracking'",
            (after, after_start.isoformat(), after_end.isoformat(), cmp["verdict"], cmp["delta"],
             dollars, outcome_id))
        conn.commit()
        row = conn.execute("SELECT * FROM recommendation_outcomes WHERE id=?", (outcome_id,)).fetchone()
    finally:
        conn.close()
    return _row(row)


def evaluate_due(restaurant_id=None, db_path=DB_PATH, today=None):
    """Evaluate every tracker whose window has closed. Scheduler entry point."""
    today = today or date.today()
    conn = get_conn(db_path)
    try:
        sql = ("SELECT id FROM recommendation_outcomes WHERE status='tracking' AND evaluate_on<=?")
        args = [today.isoformat()]
        if restaurant_id is not None:
            sql += " AND restaurant_id=?"
            args.append(restaurant_id)
        ids = [r["id"] for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()
    done = []
    for oid in ids:
        try:
            out = evaluate(oid, db_path=db_path, today=today)
            if out:
                done.append(out)
        except Exception as e:
            try:
                import ops
                ops.capture(e, job="evaluate_outcomes", context=f"outcome_id={oid}")
            except Exception:
                pass
    return done


def progress(restaurant_id, db_path=DB_PATH, today=None):
    """Interim reading for trackers still running: the metric since the start
    date, against the baseline. Labelled as partial by callers — a window that
    is a week old is a hint, not a result."""
    today = today or date.today()
    out = []
    for r in list_outcomes(restaurant_id, status="tracking", db_path=db_path):
        started = date.fromisoformat(r["started_on"])
        end = today - timedelta(days=1)
        if end < started:
            r["interim"] = None
        else:
            value, _ = metrics.measure(restaurant_id, r["metric"], started.isoformat(),
                                       end.isoformat(), db_path)
            r["interim"] = {"value": value, "days": (end - started).days + 1,
                            **metrics.compare(r["metric"], r["baseline_value"], value)}
        out.append(r)
    return out


def list_outcomes(restaurant_id, status=None, limit=50, db_path=DB_PATH):
    conn = get_conn(db_path)
    try:
        sql = "SELECT * FROM recommendation_outcomes WHERE restaurant_id=?"
        args = [restaurant_id]
        if status:
            sql += " AND status=?"
            args.append(status)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(int(limit))
        return [_row(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def abandon(restaurant_id, outcome_id, db_path=DB_PATH):
    """Stop tracking — the owner reversed the change or it no longer applies."""
    conn = get_conn(db_path)
    try:
        cur = conn.execute("UPDATE recommendation_outcomes SET status='abandoned' "
                           "WHERE id=? AND restaurant_id=? AND status='tracking'",
                           (outcome_id, restaurant_id))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def recent_results(restaurant_id, days=7, db_path=DB_PATH, today=None):
    """Verdicts that landed in the last `days` — what the morning brief and
    the weekly review lead with."""
    today = today or date.today()
    since = (today - timedelta(days=days)).isoformat()
    return [r for r in list_outcomes(restaurant_id, status="evaluated", db_path=db_path)
            if (r.get("evaluate_on") or "") >= since]


# A module key for every metric, so a realised result can be attributed back
# to the module that earned it. Owners buy modules one at a time; "which of
# the four I pay for is paying for itself" had no answer without this.
METRIC_MODULE = {
    "labor_pct": "labor", "sales": "labor", "weekday_sales": "marketing",
    "food_cost_pct": "inventory", "weekly_waste": "inventory",
    "avg_rating": "reviews", "complaints": "reviews",
    "comp_rate": "labor", "void_rate": "labor",
}


def module_of(metric) -> str:
    """Which module a metric belongs to, for value attribution."""
    base, _ = metrics.parse(metric)
    return METRIC_MODULE.get(base, "other")


def realised(restaurant_id, db_path=DB_PATH, since=None, denied_modules=None):
    """Every evaluated tracker that actually IMPROVED and carries dollars.

    This is the one honest basis for "what has Cavnar AI been worth": each
    row was measured over a named window before the change and the same
    window after, and cleared its metric's own noise band to be called an
    improvement at all. `since` is an ISO date filtering on evaluate_on.

    `denied_modules` drops whole modules before anything is summed. It has
    to happen HERE rather than on the way out: filtering a breakdown while
    leaving the total intact hands a manager the food-cost dollars back by
    subtraction.
    """
    denied = set(denied_modules or ())
    rows = []
    for r in list_outcomes(restaurant_id, status="evaluated", limit=500, db_path=db_path):
        if r.get("verdict") != "improved" or not r.get("dollars_monthly"):
            continue
        if since and (r.get("evaluate_on") or "") < str(since)[:10]:
            continue
        if denied and module_of(r["metric"]) in denied:
            continue
        rows.append(r)
    return rows


def total_value(restaurant_id, db_path=DB_PATH, since=None, denied_modules=None):
    """What measured improvements are worth, per month, with the honest
    denominator alongside.

    Deliberately NOT one bare number. `monthly` is the sum of the improved
    trackers' monthly dollars; `tracked` and `unmeasurable` say how much of
    what the owner committed to could be read at all, because a total of
    $400 from two results means something different when eight other
    trackers came back unknown.

    Summing here is legitimate where business_intelligence.money_at_stake
    refuses to: these are all the same claim kind (measured, before/after,
    per month) over the same restaurant, not a measured cost added to an
    elasticity forecast.
    """
    denied = set(denied_modules or ())

    def _visible(r):
        return not denied or module_of(r["metric"]) not in denied

    wins = realised(restaurant_id, db_path=db_path, since=since, denied_modules=denied)
    evaluated = [r for r in list_outcomes(restaurant_id, status="evaluated", limit=500,
                                          db_path=db_path)
                 if (not since or (r.get("evaluate_on") or "") >= str(since)[:10]) and _visible(r)]
    tracking = [r for r in list_outcomes(restaurant_id, status="tracking", limit=500,
                                         db_path=db_path) if _visible(r)]
    by_module = {}
    for r in wins:
        m = module_of(r["metric"])
        by_module[m] = round(by_module.get(m, 0.0) + abs(float(r["dollars_monthly"])), 2)
    return {
        "monthly": round(sum(abs(float(r["dollars_monthly"])) for r in wins), 2),
        "annual": round(sum(abs(float(r["dollars_monthly"])) for r in wins) * 12, 2),
        "wins": len(wins),
        "evaluated": len(evaluated),
        "in_flight": len(tracking),
        "unmeasurable": len([r for r in evaluated if r.get("verdict") in (None, "unknown")]),
        "no_clear_change": len([r for r in evaluated if r.get("verdict") == "no_clear_change"]),
        "by_module": by_module,
        "caveat": CAUSATION_CAVEAT,
    }


def best_ever(restaurant_id, db_path=DB_PATH, denied_modules=None):
    """The single biggest measured win, all time — the answer to "which
    recommendation created the biggest impact".

    strategy_jobs picks the biggest of ONE daily pass to notify on; that is
    a different question and deliberately stays where it is. This one has
    no time window and no dollar floor.
    """
    wins = realised(restaurant_id, db_path=db_path, denied_modules=denied_modules)
    if not wins:
        return None
    return max(wins, key=lambda r: abs(float(r["dollars_monthly"])))


def summarise(r) -> str:
    """One honest sentence for a finished tracker."""
    label = r.get("metric_label") or r["metric"]
    unit = r.get("unit") or ""
    if r.get("verdict") in (None, "unknown"):
        return f"{r['title']}: couldn't measure {label.lower()} before and after."
    fmt = (lambda v: f"${v:,.0f}") if unit == "$" else (lambda v: f"{v:g}{unit}")
    moved = f"{label} went from {fmt(r['baseline_value'])} to {fmt(r['after_value'])}"
    if r["verdict"] == "no_clear_change":
        return f"{r['title']}: {moved} — within normal week-to-week noise, so no clear change."
    money = ""
    if r.get("dollars_monthly"):
        money = f", roughly ${abs(r['dollars_monthly']):,.0f}/month"
    word = "improved" if r["verdict"] == "improved" else "got worse"
    return f"{r['title']}: {moved} — {word}{money}."

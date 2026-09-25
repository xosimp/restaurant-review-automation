"""
dsr.predictions — "How did yesterday turn out?": Cavnar grading itself.

Each night's report makes a few predictions about the NEXT night, from
Cavnar's own forecast and what it knows about tomorrow, and the next night's
report grades them against what was measured. Every prediction is a claim a
number can settle — never a vibe — so the grade is a fact, not an opinion:

  sales_range   net sales between the forecast's low and high
                (demand.forecast_day's 80% range)
  sales_budget  net sales above (or below) the night's budget, when the
                forecast's typical night says which side
  rain          rain forecast (≥ RAIN_PCT): net below a usual <weekday>
  event         an event or reservations listed: net above a usual <weekday>

A prediction is written ONCE, before its night happens (INSERT OR IGNORE on
(restaurant, for_date, key)); a re-run of the earlier night never rewrites it
after the fact. "Before" is strict (D1-7): the pipeline records only before
the predicted night's service opens (pipeline.prediction_cutoff_utc), and
created_at is when the row was really written — grade() voids any row
written at or after the night opened, so it is never graded or shown. It is
graded from the night's own Sales block — each version re-grades, so late
sales correct the grade — and a night with no sales leaves it ungraded,
never "wrong".

A prediction that rests on an owner-only fact (sales_budget names the
budget — PREDICTION_CITES) is shown only to a view allowed that fact
(review(allowed=...), dsr.access).

    record(restaurant_id, made_on, for_date, preds, db_path, made_at)
    grade(restaurant_id, for_date, facts, db_path, cutoff_utc) -> [rows]
    for_date(restaurant_id, day, db_path)                   -> [rows]
    accuracy(restaurant_id, through, days=30, db_path, exclude_keys) -> {pct, correct, graded, window_days}
"""
from datetime import date, timedelta

import dsr

RAIN_PCT = 50
ACCURACY_WINDOW_DAYS = 30
ACCURACY_MIN_GRADED = 5          # under this, the count is shown and the % is not
CORRECT, INCORRECT = "correct", "incorrect"
VOID = "void"                    # written after its night opened: not a prediction, never shown


def _db(db_path):
    from dsr import store
    return store.get_conn(db_path)


def _money(v):
    return f"${float(v):,.0f}"


def build(forecast, budget_net=None, weather=None, events=None, weekday=None) -> list:
    """The predictions for one coming night from what is known about it.
    `forecast` is demand.forecast_day's dict; `weather` forecast_for_day's
    day row; `events` demand_signals rows. Only what can be graded is
    predicted; [] when there is no forecast."""
    if not forecast or not forecast.get("available"):
        return []
    wd = weekday or forecast.get("weekday") or "night"
    typical = forecast.get("typical_sales")
    basis = f"Cavnar AI's forecast: the median of the last {forecast.get('samples')} {wd}s"
    out = []
    low, high = forecast.get("low"), forecast.get("high")
    if low is not None and high is not None:
        out.append({"key": "sales_range", "metric": "sales.net", "op": "between", "low": float(low),
                    "high": float(high), "text": f"Sales between {_money(low)} and {_money(high)}",
                    "basis": basis + " (its 80% range)"})
    if typical is not None and budget_net:
        above = float(typical) >= float(budget_net)
        out.append({"key": "sales_budget", "metric": "sales.net", "op": "gt" if above else "lt",
                    "value": float(budget_net),
                    "text": f"Sales expected {'above' if above else 'below'} budget ({_money(budget_net)})",
                    "basis": f"{basis}: {_money(typical)} against the budget"})
    rain = (weather or {}).get("precip_pct")
    if typical is not None and rain is not None and rain >= RAIN_PCT:
        out.append({"key": "rain", "metric": "sales.net", "op": "lt", "value": float(typical),
                    "text": f"Rain ({int(rain)}% chance) expected to pull sales below a usual {wd} "
                            f"({_money(typical)})",
                    "basis": "the National Weather Service forecast against Cavnar AI's typical night"})
    listed = [e for e in (events or []) if e.get("label")]
    if typical is not None and listed and not any(p["key"] == "rain" for p in out):
        names = ", ".join(str(e["label"]) for e in listed[:2])
        out.append({"key": "event", "metric": "sales.net", "op": "gt", "value": float(typical),
                    "text": f"{names} expected to lift sales above a usual {wd} ({_money(typical)})",
                    "basis": "what you listed for the date against Cavnar AI's typical night"})
    return out


# The facts a prediction rests on beyond the night's net — what dsr.access
# filters it by, like a narrative line's cites. "Sales expected above budget
# ($9,500)" names the owner's budget, which no manager view shows (D2-1).
PREDICTION_CITES = {"sales_budget": ("sales.budget_net",)}


def cites_for(key) -> tuple:
    return ("sales.net",) + tuple(PREDICTION_CITES.get(str(key), ()))


def record(restaurant_id, made_on, for_date, preds, db_path=None, made_at=None) -> int:
    """Store predictions for `for_date` made by the report of `made_on`; an
    existing one for the same night and key is kept as it was first
    written. `made_at` (naive UTC, default now) is when it was really
    written — created_at, which grade() checks against the night's open."""
    if not preds:
        return 0
    from dsr import store
    at = (made_at.strftime("%Y-%m-%d %H:%M:%S") if made_at else store._now())
    conn = _db(db_path)
    n = 0
    try:
        for p in preds:
            cur = conn.execute(
                "INSERT OR IGNORE INTO dsr_predictions (restaurant_id, for_date, made_on, key, text, metric, op, "
                "value, low, high, basis, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (restaurant_id, str(for_date)[:10], str(made_on)[:10], p["key"], p["text"], p["metric"], p["op"],
                 p.get("value"), p.get("low"), p.get("high"), p.get("basis"), at))
            n += cur.rowcount or 0
        conn.commit()
    finally:
        conn.close()
    return n


def _actual(facts, metric):
    block, _, key = str(metric).partition(".")
    b = ((facts or {}).get("blocks") or {}).get(block) or {}
    if b.get("status") != dsr.READY:
        return None
    v = (b.get("metrics") or {}).get(key)
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def outcome(row, actual):
    """CORRECT / INCORRECT for one prediction against the measured value."""
    op = row["op"]
    if op == "between":
        ok = float(row["low"]) <= actual <= float(row["high"])
    elif op == "gt":
        ok = actual > float(row["value"])
    elif op == "lt":
        ok = actual < float(row["value"])
    else:
        return None
    return CORRECT if ok else INCORRECT


def grade(restaurant_id, for_date, facts, db_path=None, cutoff_utc=None) -> list:
    """Grade the predictions made about `for_date` from that night's facts.
    Unmeasured → left ungraded (outcome NULL), never wrong. One written at
    or after `cutoff_utc` (naive UTC — the night's open,
    pipeline.prediction_cutoff_utc) was written while the night was already
    under way: it is VOID, never graded, never shown (D1-7)."""
    from dsr import store
    cut = cutoff_utc.strftime("%Y-%m-%d %H:%M:%S") if cutoff_utc else None
    conn = _db(db_path)
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM dsr_predictions WHERE restaurant_id=? AND for_date=?",
            (restaurant_id, str(for_date)[:10])).fetchall()]
        for r in rows:
            if cut and str(r.get("created_at") or "")[:19] >= cut:
                conn.execute("UPDATE dsr_predictions SET outcome=?, actual=NULL, graded_at=NULL WHERE id=?",
                             (VOID, r["id"]))
                r.update(outcome=VOID, actual=None)
                continue
            actual = _actual(facts, r["metric"])
            res = outcome(r, actual) if actual is not None else None
            conn.execute("UPDATE dsr_predictions SET outcome=?, actual=?, graded_at=? WHERE id=?",
                         (res, actual, store._now() if res else None, r["id"]))
            r.update(outcome=res, actual=actual)
        conn.commit()
    finally:
        conn.close()
    return rows


def for_date(restaurant_id, day, db_path=None) -> list:
    """The predictions about one night, VOID ones left out."""
    conn = _db(db_path)
    try:
        return [dict(r) for r in conn.execute(
            "SELECT key, text, metric, op, value, low, high, basis, outcome, actual, made_on FROM dsr_predictions "
            "WHERE restaurant_id=? AND for_date=? AND COALESCE(outcome, '') != ? ORDER BY id",
            (restaurant_id, str(day)[:10], VOID)).fetchall()]
    finally:
        conn.close()


def accuracy(restaurant_id, through, days=ACCURACY_WINDOW_DAYS, db_path=None, exclude_keys=()) -> dict:
    """Graded predictions over the window ending `through`: the share that
    came true, with the counts it rests on. `pct` is None under
    ACCURACY_MIN_GRADED graded (the count still shows). `exclude_keys`
    leaves out the predictions a view may not see (D2-1), so a manager's
    count is of what they were shown."""
    end = date.fromisoformat(str(through)[:10])
    start = end - timedelta(days=days - 1)
    skip = tuple(exclude_keys or ())
    extra = f" AND key NOT IN ({','.join('?' for _ in skip)})" if skip else ""
    conn = _db(db_path)
    try:
        rows = conn.execute(
            "SELECT outcome, COUNT(*) n FROM dsr_predictions WHERE restaurant_id=? AND for_date BETWEEN ? AND ? "
            "AND outcome IN (?, ?)" + extra + " GROUP BY outcome",
            (restaurant_id, start.isoformat(), end.isoformat(), CORRECT, INCORRECT, *skip)).fetchall()
    finally:
        conn.close()
    by = {r["outcome"]: int(r["n"]) for r in rows}
    graded = by.get(CORRECT, 0) + by.get(INCORRECT, 0)
    return {"pct": (round(by.get(CORRECT, 0) / graded * 100) if graded >= ACCURACY_MIN_GRADED else None),
            "correct": by.get(CORRECT, 0), "graded": graded, "window_days": days,
            "min_graded": ACCURACY_MIN_GRADED}


def review(restaurant_id, day, db_path=None, allowed=None) -> dict | None:
    """The report's "How did yesterday turn out?": what the previous night's
    report predicted about `day`, each graded, plus the running accuracy.
    None when nothing was predicted about the night. `allowed(cite)` is the
    view's rule (dsr.access): a prediction resting on a fact the view
    withholds is left out, and the accuracy counts only the kinds it may
    see (D2-1)."""
    rows = for_date(restaurant_id, day, db_path=db_path)
    hidden = {k for k in PREDICTION_CITES if allowed is not None and not all(allowed(c) for c in cites_for(k))}
    rows = [r for r in rows if r["key"] not in hidden]
    if not rows:
        return None
    items = []
    for r in rows:
        items.append({"key": r["key"], "text": r["text"], "outcome": r["outcome"], "basis": r["basis"],
                      "actual": r["actual"],
                      "actual_text": (f"Net sales {_money(r['actual'])}" if r["actual"] is not None else None)})
    return {"items": items, "accuracy": accuracy(restaurant_id, day, db_path=db_path, exclude_keys=sorted(hidden))}

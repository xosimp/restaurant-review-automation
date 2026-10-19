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
  rain          rain forecast (≥ RAIN_PCT): net below — or above — a usual
                <weekday>, only in the direction THIS restaurant's own rain
                nights measured (event_memory, past its sample floor)
  event         a listed event: net above — or below — a usual <weekday>,
                only in the direction that event's own past nights measured

A direction is never assumed (memory audit 9/29/26, event_memory): "rain
lowers sales" and "events lift it" were hard-coded, graded against the
median — about 50% true by construction — and a delivery-heavy restaurant
was told on every rainy night that rain would lower sales. A kind with no
measured effect here is not predicted; "a usual <weekday>" is the plain
weekday median (the forecast before its measured effects).

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
    accuracy(restaurant_id, through, days=90, db_path, exclude_keys) -> {pct, correct, graded, window_days,
                                                                      by_kind, calibration, detail}

How likely (10/19/26): every call carries `prob`, the chance it comes true
from the forecast's own spread — the same weekday's nights on the log
scale, t with n − 1 degrees of freedom and sqrt(1 + 1/n) for estimating it
(demand.prediction_range's interval, read as a probability). A call under
CALL_MIN_PROB is a coin flip and is not made: "Sales expected below budget"
on a night forecast $40 under it was right about half the time by
construction. A forecast with fewer than PROB_MIN_SAMPLES nights states no
probability and makes the budget call the old way. accuracy() counts the
range and the calls apart and sets what was said against what came true.
"""
from datetime import date, timedelta

import dsr

RAIN_PCT = 50
ACCURACY_WINDOW_DAYS = 90
PROB_MIN_SAMPLES = 5             # nights of spread before a probability is stated
CALL_MIN_PROB = 0.65             # under this a call is a coin flip and is not made
ACCURACY_MIN_GRADED = 5          # under this, the count is shown and the % is not
CORRECT, INCORRECT = "correct", "incorrect"
VOID = "void"                    # written after its night opened: not a prediction, never shown


def _db(db_path):
    from dsr import store
    return store.get_conn(db_path)


def _money(v):
    return f"${float(v):,.0f}"


def _pct(p) -> str:
    return f"{int(round(p * 100))}% likely"


def p_above(forecast, center, line):
    """P(the night's net > `line`) when the night centres on `center`, from
    the forecast's log spread (log_sd over sd_n nights); None without
    PROB_MIN_SAMPLES of them. Pure."""
    import math
    import metrics
    sd, n = (forecast or {}).get("log_sd"), (forecast or {}).get("sd_n")
    if not sd or not n or n < PROB_MIN_SAMPLES or not center or not line or center <= 0 or line <= 0:
        return None
    scale = float(sd) * (1.0 + 1.0 / n) ** 0.5
    return metrics.t_cdf((math.log(float(center)) - math.log(float(line))) / scale, n - 1)


def build(forecast, budget_net=None, weather=None, events=None, weekday=None, effects=None) -> list:
    """The predictions for one coming night from what is known about it.
    `forecast` is demand.forecast_net's dict (the report's own basis);
    `weather` forecast_for_day's day row; `events` demand_signals rows;
    `effects` {"rain": event_memory.measured_effect(..), "events": {listed
    label: effect}}. Only what can be graded is predicted; [] when there is
    no forecast."""
    if not forecast or not forecast.get("available"):
        return []
    wd = weekday or forecast.get("weekday") or "night"
    typical = forecast.get("typical_sales")
    usual = forecast.get("base_sales") or typical
    basis = f"Cavnar AI's forecast: the median of the last {forecast.get('samples')} {wd}s"
    if forecast.get("effects"):
        basis += " with this restaurant's measured effects of " + ", ".join(
            str(e.get("display") or e.get("label")) for e in forecast["effects"])
    out = []
    low, high = forecast.get("low"), forecast.get("high")
    if low is not None and high is not None:
        cov = (forecast.get("range_coverage_pct") or 80) / 100.0
        out.append({"key": "sales_range", "metric": "sales.net", "op": "between", "low": float(low),
                    "high": float(high), "prob": cov,
                    "text": f"Sales between {_money(low)} and {_money(high)} · {_pct(cov)}",
                    "basis": basis + f" (its {int(round(cov * 100))}% range)"})
    if typical is not None and budget_net:
        p = p_above(forecast, typical, budget_net)
        above = (p >= 0.5) if p is not None else float(typical) >= float(budget_net)
        prob = (p if above else 1.0 - p) if p is not None else None
        if prob is None or prob >= CALL_MIN_PROB:
            out.append({"key": "sales_budget", "metric": "sales.net", "op": "gt" if above else "lt",
                        "value": float(budget_net), "prob": prob,
                        "text": (f"Sales expected {'above' if above else 'below'} budget ({_money(budget_net)})"
                                 + (f" · {_pct(prob)}" if prob is not None else "")),
                        "basis": f"{basis}: {_money(typical)} against the budget"})
    effects = effects or {}
    rain = (weather or {}).get("precip_pct")
    reff = effects.get("rain")
    rprob = _effect_prob(forecast, usual, reff)
    if (usual is not None and rain is not None and rain >= RAIN_PCT and _directed(reff)
            and (rprob is None or rprob >= CALL_MIN_PROB)):
        below = reff["direction"] == "down"
        out.append({"key": "rain", "metric": "sales.net", "op": "lt" if below else "gt", "value": float(usual),
                    "prob": rprob,
                    "text": (f"Rain ({int(rain)}% chance) expected to pull sales {'below' if below else 'above'} a "
                             f"usual {wd} ({_money(usual)})" + (f" · {_pct(rprob)}" if rprob is not None else "")),
                    "basis": (f"the National Weather Service forecast; rainy nights here ran a median "
                              f"{abs(reff['median_lift_pct']):.0f}% {'below' if below else 'above'} a usual night "
                              f"(measured {reff['n']} times)")})
    listed = [e for e in (events or []) if e.get("label") and e.get("kind", "event") == "event"]
    measured = [(e, (effects.get("events") or {}).get(str(e["label"]))) for e in listed]
    measured = [(e, m) for e, m in measured if _directed(m)]
    measured = [(e, m, _effect_prob(forecast, usual, m)) for e, m in measured]
    measured = [(e, m, p) for e, m, p in measured if p is None or p >= CALL_MIN_PROB]
    if usual is not None and measured and not any(p["key"] == "rain" for p in out):
        e, m, eprob = max(measured, key=lambda emp: emp[1]["n"])
        up = m["direction"] == "up"
        out.append({"key": "event", "metric": "sales.net", "op": "gt" if up else "lt", "value": float(usual),
                    "prob": eprob,
                    "text": (f"{e['label']} expected to {'lift sales above' if up else 'pull sales below'} a usual "
                             f"{wd} ({_money(usual)})" + (f" · {_pct(eprob)}" if eprob is not None else "")),
                    "basis": (f"what you listed for the date; nights like it here ran a median "
                              f"{abs(m['median_lift_pct']):.0f}% {'above' if up else 'below'} a usual night "
                              f"(measured {m['n']} times)")})
    return out


def _effect_prob(forecast, usual, effect):
    """How likely a measured effect's direction holds tonight: the night
    centred on a usual one moved by the effect's median lift, against the
    usual night. None without a spread or a directed effect."""
    if usual is None or not _directed(effect) or effect.get("median_lift_pct") is None:
        return None
    lift = abs(float(effect["median_lift_pct"])) / 100.0
    up = effect["direction"] == "up"
    center = float(usual) * (1.0 + lift if up else max(0.01, 1.0 - lift))
    p = p_above(forecast, center, usual)
    return None if p is None else (p if up else 1.0 - p)


def _directed(effect) -> bool:
    """A measured effect clear enough to predict with: past its sample floor
    (event_memory `applies`) and a direction (up / down) past its floor."""
    return bool(effect and effect.get("applies") and effect.get("direction") in ("up", "down"))


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
                "value, low, high, basis, prob, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (restaurant_id, str(for_date)[:10], str(made_on)[:10], p["key"], p["text"], p["metric"], p["op"],
                 p.get("value"), p.get("low"), p.get("high"), p.get("basis"), p.get("prob"), at))
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
            "SELECT key, text, metric, op, value, low, high, basis, outcome, actual, made_on, prob FROM dsr_predictions "
            "WHERE restaurant_id=? AND for_date=? AND COALESCE(outcome, '') != ? ORDER BY id",
            (restaurant_id, str(day)[:10], VOID)).fetchall()]
    finally:
        conn.close()


def accuracy(restaurant_id, through, days=ACCURACY_WINDOW_DAYS, db_path=None, exclude_keys=()) -> dict:
    """Graded predictions over the window ending `through`: the share that
    came true, with the counts it rests on. `pct` is None under
    ACCURACY_MIN_GRADED graded (the count still shows). `exclude_keys`
    leaves out the predictions a view may not see (D2-1), so a manager's
    count is of what they were shown.

    `by_kind`: the range ("range") and the directional calls ("calls")
    counted apart — a range stated at 80% and a call at 70% are different
    claims. `calibration`: over the graded rows that carried a probability,
    the average said against the share that came true, once there are
    ACCURACY_MIN_GRADED of them. `detail` is the line both apps show."""
    end = date.fromisoformat(str(through)[:10])
    start = end - timedelta(days=days - 1)
    skip = tuple(exclude_keys or ())
    extra = f" AND key NOT IN ({','.join('?' for _ in skip)})" if skip else ""
    conn = _db(db_path)
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT key, outcome, prob FROM dsr_predictions WHERE restaurant_id=? AND for_date BETWEEN ? AND ? "
            "AND outcome IN (?, ?)" + extra,
            (restaurant_id, start.isoformat(), end.isoformat(), CORRECT, INCORRECT, *skip)).fetchall()]
    finally:
        conn.close()
    correct = sum(1 for r in rows if r["outcome"] == CORRECT)
    graded = len(rows)
    by_kind = {}
    for kind, keys in (("range", ("sales_range",)), ("calls", ("sales_budget", "rain", "event"))):
        k = [r for r in rows if r["key"] in keys]
        by_kind[kind] = {"correct": sum(1 for r in k if r["outcome"] == CORRECT), "graded": len(k)}
    probd = [r for r in rows if r.get("prob") is not None]
    calibration = None
    if len(probd) >= ACCURACY_MIN_GRADED:
        calibration = {"said_pct": round(sum(float(r["prob"]) for r in probd) / len(probd) * 100),
                       "came_true_pct": round(sum(1 for r in probd if r["outcome"] == CORRECT) / len(probd) * 100),
                       "n": len(probd)}
    parts = []
    if by_kind["range"]["graded"]:
        parts.append(f"Ranges held {by_kind['range']['correct']} of {by_kind['range']['graded']}")
    if by_kind["calls"]["graded"]:
        parts.append(f"calls {by_kind['calls']['correct']} of {by_kind['calls']['graded']}")
    if calibration:
        parts.append(f"said {calibration['said_pct']}%, came true {calibration['came_true_pct']}%")
    detail = " · ".join(parts)
    return {"pct": (round(correct / graded * 100) if graded >= ACCURACY_MIN_GRADED else None),
            "correct": correct, "graded": graded, "window_days": days,
            "min_graded": ACCURACY_MIN_GRADED, "by_kind": by_kind, "calibration": calibration,
            "detail": (detail[0].upper() + detail[1:]) if detail else None}


def forecast_error(restaurant_id, day, db_path=None):
    """How far the nightly forecast has missed, out of sample
    (demand.demand_accuracy through `day`): {"mean_miss_pct", "n_nights"},
    or None before it has enough scored nights. Never raises."""
    try:
        import demand
        kw = {"db_path": db_path} if db_path else {}
        a = demand.demand_accuracy(restaurant_id, today=date.fromisoformat(str(day)[:10]) + timedelta(days=1), **kw)
    except Exception as e:
        print(f"[dsr.predictions] forecast error unavailable for {restaurant_id}: {e}")
        return None
    if not a.get("available") or a.get("mean_error_pct") is None:
        return None
    return {"mean_miss_pct": float(a["mean_error_pct"]), "n_nights": int(a.get("n_nights") or 0)}


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
                      "prob": r.get("prob"),
                      "actual": r["actual"],
                      "actual_text": (f"Net sales {_money(r['actual'])}" if r["actual"] is not None else None)})
    acc = accuracy(restaurant_id, day, db_path=db_path, exclude_keys=sorted(hidden))
    err = forecast_error(restaurant_id, day, db_path=db_path)
    if err:
        acc["forecast_error"] = err
        line = f"forecast off {err['mean_miss_pct']:.0f}% on an average night ({err['n_nights']} nights)"
        acc["detail"] = (acc["detail"] + " · " + line) if acc.get("detail") else line[0].upper() + line[1:]
    return {"items": items, "accuracy": acc}

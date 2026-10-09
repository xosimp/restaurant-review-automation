"""The weekly budget, planned (owner, 10/9/26): the owner's own figures per
night beside Cavnar AI's suggestion for each, by day or by the week.

Rules this module keeps:

  * A suggestion is never a budget. Nothing here writes a night unless the
    owner saved it (save_week, from the Budget tab's Save).
  * Every suggested figure says what it rests on (`basis`) and how close the
    forecast it comes from has landed here (`confidence_pct`, measured over
    the nights scored against it; None below the sample floor - shown as
    "—", never as a word).
  * A night with nothing to go on is left blank and says why, never 0.
  * Weekly mode is one figure for the week, split across the nights by this
    restaurant's own weekday mix (its measured nets over recent weeks), so
    each night's report can still say "vs budget". The split is shown, and
    the nights the restaurant is closed get nothing.

The net suggestion is demand.forecast_net - the forecast on the nightly
report's own basis, the one a report's net is set beside. Gross is the
net raised by this restaurant's own gross-to-net ratio over recent nights
with both measured; withheld until GROSS_MIN_NIGHTS such nights exist.
"""
from datetime import date, timedelta

from models import DB_PATH

DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
MODES = ("day", "week")
GROSS_MIN_NIGHTS = 5          # nights with gross and net both measured before a gross is suggested
GROSS_WINDOW_DAYS = 42
ACCURACY_WINDOW_DAYS = 56
ACCURACY_MIN_NIGHTS = 8       # nights scored against the forecast before a confidence % is stated
MIX_WEEKS = 8                 # weeks of measured nets the weekly split reads
MIX_MIN_WEEKS = 3             # a weekday needs this many measured nights to carry its own share


def _d(x):
    return x if isinstance(x, date) else date.fromisoformat(str(x)[:10])


def _mdy(d):
    from time_utils import mdy
    return mdy(_d(d))


def week_dates(start):
    s = _d(start)
    return [s + timedelta(days=i) for i in range(7)]


def mode_of(restaurant_id, db_path=DB_PATH):
    """'day' or 'week' (restaurants.dsr_budget_mode; unset reads as day)."""
    from models import get_conn
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT dsr_budget_mode FROM restaurants WHERE id=?", (restaurant_id,)).fetchone()
    except Exception:
        row = None
    finally:
        conn.close()
    m = row[0] if row else None
    return m if m in MODES else "day"


def gross_ratio(restaurant_id, before, db_path=DB_PATH):
    """(ratio, nights): this restaurant's gross ÷ net over the recent nights
    with both measured, or (None, n) below GROSS_MIN_NIGHTS."""
    from dsr.store import _measured
    ratios = []
    for i in range(1, GROSS_WINDOW_DAYS + 1):
        m = _measured(restaurant_id, before - timedelta(days=i), db_path)
        if m.get("gross") and m.get("net") and m["net"] > 0 and m["gross"] >= m["net"]:
            ratios.append(m["gross"] / m["net"])
    if len(ratios) < GROSS_MIN_NIGHTS:
        return None, len(ratios)
    ratios.sort()
    mid = len(ratios) // 2
    med = ratios[mid] if len(ratios) % 2 else (ratios[mid - 1] + ratios[mid]) / 2
    return med, len(ratios)


def forecast_accuracy(restaurant_id, before, db_path=DB_PATH):
    """(confidence_pct, nights): 100 minus the forecast's mean miss over the
    nights scored against it in the last ACCURACY_WINDOW_DAYS, or (None, n)
    below ACCURACY_MIN_NIGHTS. The same nights the forecast's own
    calibration reads (demand._scored_nights, same basis only)."""
    try:
        import demand
        nights, _other = demand._scored_nights(restaurant_id, (before - timedelta(days=ACCURACY_WINDOW_DAYS)).isoformat(),
                                               (before - timedelta(days=1)).isoformat(), db_path=db_path)
    except Exception:
        return None, 0
    misses = []
    for n in nights.values():
        actual, fc = n.get("sales.net"), n.get("sales.forecast_net")
        if actual and actual > 0 and fc and fc > 0:
            misses.append(abs(actual / fc - 1.0) * 100.0)
    if len(misses) < ACCURACY_MIN_NIGHTS:
        return None, len(misses)
    return max(0, min(100, int(round(100 - sum(misses) / len(misses))))), len(misses)


def suggest_night(restaurant_id, day, ratio=None, conf=None, db_path=DB_PATH):
    """{net, gross, basis, confidence_pct, low, high} for one night, or
    {"why": reason} when the forecast is withheld."""
    import demand
    day = _d(day)
    try:
        fc = demand.forecast_net(restaurant_id, day, db_path=db_path)
    except Exception as e:
        return {"why": f"the forecast couldn't be read ({type(e).__name__})"}
    if not fc or not fc.get("available"):
        return {"why": (fc or {}).get("reason") or "not enough past nights to forecast this one yet"}
    net = round(float(fc["typical_sales"]), -1)
    basis = f"the last {fc.get('samples')} {day.strftime('%A')}s here"
    if fc.get("effect_basis"):
        basis += f"; {fc['effect_basis']}"
    out = {"net": net, "gross": round(net * ratio, -1) if ratio else None, "basis": basis,
           "confidence_pct": conf, "low": fc.get("low"), "high": fc.get("high")}
    return out


def weekday_mix(restaurant_id, before, db_path=DB_PATH):
    """({weekday: share}, basis): each weekday's share of a typical week from
    its measured nets over the last MIX_WEEKS weeks (the median of each
    weekday), or ({}, reason) when fewer than MIX_MIN_WEEKS of most days."""
    import canonical_facts as cf
    rows = cf.final_days(restaurant_id, (before - timedelta(weeks=MIX_WEEKS)).isoformat(),
                         (before - timedelta(days=1)).isoformat(), db_path=db_path)
    by = {}
    for r in rows:
        s = r.get("sales")
        if s and s > 0:
            by.setdefault(_d(r["date"]).strftime("%A"), []).append(float(s))
    med = {}
    for wd, xs in by.items():
        if len(xs) >= MIX_MIN_WEEKS:
            xs = sorted(xs)
            m = len(xs) // 2
            med[wd] = xs[m] if len(xs) % 2 else (xs[m - 1] + xs[m]) / 2
    if len(med) < 3:
        return {}, f"fewer than {MIX_MIN_WEEKS} measured weeks for most days"
    total = sum(med.values())
    return ({wd: med[wd] / total for wd in DAYS if wd in med},
            f"each weekday's usual share of the week, from the last {MIX_WEEKS} weeks of nights")


def split_week(total, dates, mix):
    """{iso date: figure}: `total` across `dates` by `mix` (shares of the
    weekdays present, renormalised), rounded to $10 with the last open night
    taking the rounding so the nights add to the total. Nights whose weekday
    has no share get nothing."""
    if total is None:
        return {}
    present = [(d, mix.get(d.strftime("%A"), 0.0)) for d in dates]
    s = sum(w for _d0, w in present)
    if s <= 0:
        return {}
    out, run, last = {}, 0.0, None
    for d, w in present:
        if w <= 0:
            continue
        v = round(total * w / s, -1)
        out[d.isoformat()] = v
        run += v
        last = d.isoformat()
    if last is not None:
        out[last] = round(out[last] + (total - run), 2)
    return out


def week_view(restaurant_id, start, today=None, db_path=DB_PATH):
    """The Budget tab's week: each night's saved budget beside Cavnar AI's
    suggestion, the week's totals, the mode and (weekly) the split."""
    from dsr.store import budgets_for
    dates = week_dates(start)
    today = _d(today) if today else date.today()
    saved = budgets_for(restaurant_id, dates[0], dates[-1], db_path=db_path)
    ratio, gross_n = gross_ratio(restaurant_id, today, db_path)
    conf, conf_n = forecast_accuracy(restaurant_id, today, db_path)
    days, s_net, s_gross, b_net, b_gross = [], 0.0, 0.0, 0.0, 0.0
    any_s_net = any_s_gross = any_b_net = any_b_gross = False
    for d in dates:
        b = saved.get(d.isoformat()) or {}
        sg = suggest_night(restaurant_id, d, ratio, conf, db_path)
        if sg.get("net") is not None:
            s_net += sg["net"]
            any_s_net = True
        if sg.get("gross") is not None:
            s_gross += sg["gross"]
            any_s_gross = True
        if b.get("net") is not None:
            b_net += b["net"]
            any_b_net = True
        if b.get("gross") is not None:
            b_gross += b["gross"]
            any_b_gross = True
        days.append({"date": d.isoformat(), "weekday": d.strftime("%A"), "label": _mdy(d),
                     "budget_net": b.get("net"), "budget_gross": b.get("gross"),
                     "suggest": None if "why" in sg else sg, "why_none": sg.get("why")})
    mix, mix_basis = weekday_mix(restaurant_id, today, db_path)
    if not mix:
        # No measured weekday mix yet: the suggestion's own shape, where it has one.
        fc_mix = {x["weekday"]: x["suggest"]["net"] for x in days if x["suggest"]}
        tot = sum(fc_mix.values())
        if len(fc_mix) >= 3 and tot > 0:
            mix, mix_basis = {k: v / tot for k, v in fc_mix.items()}, "the shape of Cavnar AI's forecast for this week"
    return {
        "start": dates[0].isoformat(), "end": dates[-1].isoformat(),
        "label": f"{_mdy(dates[0])} – {_mdy(dates[-1])}",
        "mode": mode_of(restaurant_id, db_path),
        "days": days,
        "budget": {"net": round(b_net, 2) if any_b_net else None, "gross": round(b_gross, 2) if any_b_gross else None},
        "suggest": {"net": round(s_net, 2) if any_s_net else None, "gross": round(s_gross, 2) if any_s_gross else None,
                    "confidence_pct": conf, "nights_scored": conf_n,
                    "confidence_basis": (f"how close the forecast has landed here over the last {conf_n} nights scored"
                                         if conf is not None else
                                         f"{conf_n} nights scored against the forecast so far — a % needs "
                                         f"{ACCURACY_MIN_NIGHTS}"),
                    "gross_basis": (f"your own gross-to-net over the last {gross_n} nights with both measured"
                                    if ratio else f"gross needs {GROSS_MIN_NIGHTS} nights with gross and net "
                                                  f"measured ({gross_n} so far)")},
        "mix": {k: round(v, 4) for k, v in mix.items()}, "mix_basis": mix_basis,
    }


def save_week(restaurant_id, start, net=None, gross=None, updated_by=None, today=None, db_path=DB_PATH):
    """Weekly mode's Save: one net and/or gross for the week, split across
    its nights by the weekday mix and written as each night's budget. A
    blank figure clears that column for the week. Returns the nights."""
    from dsr.store import set_budget
    dates = week_dates(start)
    today = _d(today) if today else date.today()
    mix, _basis = weekday_mix(restaurant_id, today, db_path)
    if not mix:
        view = week_view(restaurant_id, start, today, db_path)
        mix = view["mix"]
    if not mix:
        raise ValueError("There isn't a weekday pattern to split a week by yet — enter the budget by day for now.")
    nets, grosses = split_week(net, dates, mix), split_week(gross, dates, mix)
    out = []
    for d in dates:
        k = d.isoformat()
        n, g = nets.get(k), grosses.get(k)
        set_budget(restaurant_id, d, gross=g, net=n, updated_by=updated_by, db_path=db_path)
        out.append({"date": k, "label": _mdy(d), "net": n, "gross": g})
    return out

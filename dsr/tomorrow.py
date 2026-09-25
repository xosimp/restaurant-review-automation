"""
dsr.tomorrow — the next night, as a night's report goes out: what to prep
for, Cavnar's forecast, the confidence in it and what it rests on, and the
predictions tomorrow's report will grade (dsr.predictions).

Built once by the pipeline when a night's blocks are in and stored on the
report (facts["tomorrow"], store.save_section), so the email, the app and a
later read all show the same thing. Deterministic — no model call — and
every item is something Cavnar holds: time off on the books, the National
Weather Service forecast, events and reservations the owner listed, stock
the Food block called critically low, the published schedule and Cavnar's
own forecast. Nothing is shown that isn't measured or listed.

    snap = tomorrow.build(restaurant, business_date, facts, db_path=None)
    # {"date", "label", "items": [{"text", "kind", "tone"}], "scheduled",
    #  "forecast": {...}|None, "confidence": {...}|None, "predictions": [...]}

THE CONFIDENCE (a percentage, never a word) is the forecast's own measured
record, and only that (D1-6): the share of recent nights whose net landed
inside the range the forecast stated for them (demand.demand_accuracy's
inside_range_pct — out of sample, each night's range built only from the
nights before it). The forecast is the median of the same weekday's recent
nights; tonight's sales, the schedule, events and the weather are not in it,
so they are listed as things to watch (`watch`), never counted as
confidence. With no range yet (fewer than FULL_HISTORY_FOR_RANGE weekdays)
or fewer than TRACK_MIN ranged nights scored, there is no % — "—" and the
count it is waiting for. No forecast → None.
"""
from datetime import date, timedelta

import dsr

TRACK_MIN = 10
FULL_HISTORY_FOR_RANGE = 8        # demand.RANGE_MIN_SAMPLES: the forecast states a range from here
RAIN_PCT = 40
HOT_F, COLD_F = 92, 25


def _money(v):
    return f"${float(v):,.0f}"


def _time_off(rid, day, db_path):
    from dsr import store
    conn = store.get_conn(db_path)
    try:
        rows = conn.execute("SELECT employee_name, status FROM staff_time_off WHERE restaurant_id=? "
                            "AND status IN ('approved','pending') AND start_date<=? AND end_date>=?",
                            (rid, day.isoformat(), day.isoformat())).fetchall()
    except Exception:
        rows = []
    finally:
        conn.close()
    return [dict(r) for r in rows]


def _events(rid, day, db_path):
    try:
        import demand_signals
        return demand_signals.upcoming(rid, day.isoformat(), day.isoformat(), db_path=db_path) if db_path \
            else demand_signals.upcoming(rid, day.isoformat(), day.isoformat())
    except Exception:
        return []


def _keeps_events(rid, db_path):
    """Whether this restaurant keeps its events/reservations in Cavnar at
    all — an empty day then means "nothing listed", not "unknown"."""
    from dsr import store
    conn = store.get_conn(db_path)
    try:
        r = conn.execute("SELECT 1 FROM demand_signals WHERE restaurant_id=? LIMIT 1", (rid,)).fetchone()
        return bool(r)
    except Exception:
        return False
    finally:
        conn.close()


def _weather(restaurant, day, db_path):
    try:
        import weather
        fc = weather.forecast_for_day(restaurant, day, db_path=db_path) if db_path \
            else weather.forecast_for_day(restaurant, day)
        return (fc or {}).get("day")
    except Exception:
        return None


def _forecast(rid, day, db_path):
    try:
        import demand
        return demand.forecast_day(rid, day, db_path=db_path) if db_path else demand.forecast_day(rid, day)
    except Exception:
        return None


def _scheduled(rid, day, db_path):
    try:
        import intraday
        rows = intraday.published_rows(rid, day, db_path=db_path) if db_path else intraday.published_rows(rid, day)
    except Exception:
        return None
    names = {" ".join(str(r.get("employee") or "").lower().split()) for r in rows or []}
    names.discard("")
    return len(names) if names else None


def _track(rid, tmr, db_path):
    """demand.demand_accuracy through the night before `tmr` — how often the
    forecast's range held, each night scored against the range it had then."""
    try:
        import demand
        return demand.demand_accuracy(rid, today=tmr, db_path=db_path) if db_path \
            else demand.demand_accuracy(rid, today=tmr)
    except Exception:
        return None


def _budget(rid, day, db_path):
    try:
        from dsr import store
        b = store.budgets_for(rid, day, day, db_path=db_path) if db_path else store.budgets_for(rid, day, day)
        return (b.get(day.isoformat()) or {}).get("net")
    except Exception:
        return None


def confidence(forecast, track, wx=None, scheduled=None, keeps_events=False) -> dict | None:
    """The confidence in Cavnar's forecast for tomorrow (module docstring):
    how often its stated range has held, out of sample, over the last
    `track["window_days"]` — nothing else.

    `forecast` is demand.forecast_day's dict; `track` is
    demand.demand_accuracy's (inside_range_pct over n_ranged nights, each
    night's range taken only from the nights before it). The forecast uses
    only the weekday's own sales history, so only that and its measured
    record are what the % rests on (D1-6): tonight's sales, the schedule,
    events and the weather are not inputs to it, and are listed as things to
    watch, never as confidence. No range (under demand.RANGE_MIN_SAMPLES
    weekdays) or fewer than TRACK_MIN ranged nights → no % ("—"), with the
    count it is waiting for — never a figure from how many inputs exist."""
    if not forecast or not forecast.get("available"):
        return None
    samples = int(forecast.get("samples") or 0)
    wd = forecast.get("weekday") or "night"
    history = f"{samples} {wd}{'s' if samples != 1 else ''} of sales history"
    watch = []
    if wx and not wx.get("stale"):
        watch.append("the weather forecast")
    if scheduled:
        watch.append("tomorrow's schedule")
    if keeps_events:
        watch.append("your events and reservations")
    t = track or {}
    ranged = int(t.get("n_ranged") or 0)
    inside = t.get("inside_range_pct")
    has_range = forecast.get("low") is not None and forecast.get("high") is not None
    pct = None
    if not has_range:
        track_line = (f"Only {samples} {wd}{'s' if samples != 1 else ''} on file — the forecast states a range, "
                      f"and a confidence in it, from {FULL_HISTORY_FOR_RANGE}")
    elif ranged >= TRACK_MIN and isinstance(inside, (int, float)):
        pct = int(round(inside))
        held = int(round(inside * ranged / 100.0))
        track_line = (f"Cavnar's range held on {held} of the last {ranged} nights "
                      f"({t.get('window_days')}-day record)")
    else:
        track_line = (f"{ranged} night{'s' if ranged != 1 else ''} scored against Cavnar's range so far — "
                      f"a confidence % shows at {TRACK_MIN}")
    return {"pct": pct, "based_on": [history] + (["its measured record"] if pct is not None else []),
            "missing": [], "watch": watch, "track": track_line,
            "label": f"{pct}%" if pct is not None else "—",
            "meaning": "How often Cavnar's forecast range has held — not a promise"}


def build(restaurant, business_date, facts=None, db_path=None) -> dict:
    """The next night's snapshot for the report of `business_date`."""
    from dsr import predictions
    day = business_date if isinstance(business_date, date) else date.fromisoformat(str(business_date)[:10])
    tmr = day + timedelta(days=1)
    rid = restaurant.id
    wd = tmr.strftime("%A")
    blocks = (facts or {}).get("blocks") or {}
    items = []

    off = _time_off(rid, tmr, db_path)
    approved = [o for o in off if o["status"] == "approved"]
    pending = [o for o in off if o["status"] == "pending"]
    if approved:
        n = len(approved)
        items.append({"kind": "time_off", "tone": "warn",
                      "text": f"{n} employee{'s' if n != 1 else ''} off ({', '.join(o['employee_name'] for o in approved[:3])}"
                              f"{'…' if n > 3 else ''})"})
    if pending:
        n = len(pending)
        items.append({"kind": "time_off_pending", "tone": "warn",
                      "text": f"{n} time-off request{'s' if n != 1 else ''} still waiting for an answer"})

    wx = _weather(restaurant, tmr, db_path)
    if wx and not wx.get("stale"):
        rain, hi = wx.get("precip_pct"), wx.get("high_f")
        short = (wx.get("short_forecast") or "").strip()
        if rain is not None and rain >= RAIN_PCT:
            items.append({"kind": "weather", "tone": "warn",
                          "text": f"Rain forecast ({int(rain)}% chance{f', high {hi}°' if hi is not None else ''})"})
        elif hi is not None and (hi >= HOT_F or hi <= COLD_F):
            items.append({"kind": "weather", "tone": "warn", "text": f"{'Hot' if hi >= HOT_F else 'Cold'} day — high {hi}°"})
        elif short:
            items.append({"kind": "weather", "tone": None,
                          "text": f"{short}{f', high {hi}°' if hi is not None else ''}"})

    events = _events(rid, tmr, db_path)
    for e in events[:3]:
        if e.get("kind") == "reservations" and e.get("covers"):
            items.append({"kind": "reservations", "tone": None, "text": f"{e['covers']} covers on the books"})
        else:
            items.append({"kind": "event", "tone": "warn", "text": str(e.get("label"))})

    food = blocks.get("food") or {}
    crit = (((food.get("detail") or {}).get("stock") or {}).get("critical") or []) \
        if food.get("status") == dsr.READY else []
    for x in crit[:2]:
        if x.get("item"):
            left = x.get("days_remaining")
            items.append({"kind": "stock", "tone": "warn",
                          "text": f"{x['item']} low" + (f" ({left:g} day{'s' if left != 1 else ''} left)"
                                                         if isinstance(left, (int, float)) else "")})

    scheduled = _scheduled(rid, tmr, db_path)
    fc = _forecast(rid, tmr, db_path)
    forecast = None
    if fc and fc.get("available"):
        forecast = {"typical": fc.get("typical_sales"), "low": fc.get("low"), "high": fc.get("high"),
                    "samples": fc.get("samples"), "weekday": wd,
                    "text": (f"{_money(fc['low'])}–{_money(fc['high'])}" if fc.get("low") is not None
                             and fc.get("high") is not None else _money(fc["typical_sales"])),
                    "basis": f"the median of the last {fc.get('samples')} {wd}s"}
    conf = confidence(fc, _track(rid, tmr, db_path), wx=wx, scheduled=scheduled,
                      keeps_events=_keeps_events(rid, db_path))
    preds = predictions.build(fc, budget_net=_budget(rid, tmr, db_path), weather=wx, events=events, weekday=wd)
    return {"date": tmr.isoformat(), "weekday": wd, "items": items, "scheduled": scheduled,
            "forecast": forecast, "confidence": conf,
            "predictions": [{"key": p["key"], "text": p["text"]} for p in preds], "_preds": preds}

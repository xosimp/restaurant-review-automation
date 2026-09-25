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

THE CONFIDENCE (a percentage, never a word) is how much Cavnar's view of
tomorrow rests on — each input weighted, and the running prediction
accuracy folded in once there is a track record:

    sales history   35   the weekday's history, full at FULL_HISTORY nights
    tonight's sales 20   the Sales block is measured
    weather         15   a current (not stale) NWS forecast for the date
    schedule        15   a published schedule covers the date
    events          15   events/reservations are kept for this restaurant

With fewer than TRACK_MIN graded predictions it is held at NO_TRACK_CAP
(the confidence rule: no track record, no higher than 70); after that it is
the mean of the inputs' coverage and the accuracy. No forecast → None.
"""
from datetime import date, timedelta

import dsr

INPUT_WEIGHTS = (("history", 35), ("sales", 20), ("weather", 15), ("schedule", 15), ("events", 15))
FULL_HISTORY = 12
TRACK_MIN = 10
NO_TRACK_CAP = 70
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


def _budget(rid, day, db_path):
    try:
        from dsr import store
        b = store.budgets_for(rid, day, day, db_path=db_path) if db_path else store.budgets_for(rid, day, day)
        return (b.get(day.isoformat()) or {}).get("net")
    except Exception:
        return None


def confidence(forecast, sales_measured, wx, scheduled, keeps_events, track) -> dict | None:
    """The confidence in Cavnar's view of tomorrow (module docstring)."""
    if not forecast or not forecast.get("available"):
        return None
    samples = int(forecast.get("samples") or 0)
    wd = forecast.get("weekday") or "night"
    have = {
        "history": min(1.0, samples / FULL_HISTORY),
        "sales": 1.0 if sales_measured else 0.0,
        "weather": 1.0 if (wx and not wx.get("stale")) else 0.0,
        "schedule": 1.0 if scheduled else 0.0,
        "events": 1.0 if keeps_events else 0.0,
    }
    coverage = sum(w * have[k] for k, w in INPUT_WEIGHTS) / sum(w for _k, w in INPUT_WEIGHTS)
    weeks = samples
    history = (f"{weeks // 52} year{'s' if weeks // 52 != 1 else ''} of {wd} sales history" if weeks >= 52
               else f"{weeks} {wd}{'s' if weeks != 1 else ''} of sales history")
    labels = {"history": history, "sales": "tonight's sales", "weather": "the weather forecast",
              "schedule": "tomorrow's schedule", "events": "your events and reservations"}
    based = [labels[k] for k, _w in INPUT_WEIGHTS if have[k] > 0]
    missing = [labels[k] for k, _w in INPUT_WEIGHTS if have[k] == 0]
    graded = int((track or {}).get("graded") or 0)
    if graded >= TRACK_MIN and (track or {}).get("pct") is not None:
        pct = round(100 * (coverage + track["pct"] / 100.0) / 2)
        track_line = f"{track['correct']} of {graded} predictions right in the last {track['window_days']} days"
    else:
        pct = min(round(100 * coverage), NO_TRACK_CAP)
        track_line = (f"{graded} prediction{'s' if graded != 1 else ''} graded so far — held at "
                      f"{NO_TRACK_CAP}% until there are {TRACK_MIN}")
    return {"pct": int(pct), "based_on": based, "missing": missing, "track": track_line,
            "label": f"{int(pct)}%", "meaning": "How much Cavnar's view of tomorrow rests on — not a promise"}


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
    sales_ok = (blocks.get("sales") or {}).get("status") == dsr.READY
    track = predictions.accuracy(rid, day, db_path=db_path)
    conf = confidence(fc, sales_ok, wx, scheduled, _keeps_events(rid, db_path), track)
    preds = predictions.build(fc, budget_net=_budget(rid, tmr, db_path), weather=wx, events=events, weekday=wd)
    return {"date": tmr.isoformat(), "weekday": wd, "items": items, "scheduled": scheduled,
            "forecast": forecast, "confidence": conf,
            "predictions": [{"key": p["key"], "text": p["text"]} for p in preds], "_preds": preds}

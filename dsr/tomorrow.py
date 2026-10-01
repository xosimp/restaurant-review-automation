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
nights on the report's own basis (demand.forecast_net), with this
restaurant's MEASURED effects of what is listed for the date applied
(event_memory, behind its sample floor — `forecast.effects` names them);
tonight's sales, the schedule and the weather are not in it, so they are
listed as things to watch (`watch`), never counted as confidence. With no range yet (fewer than FULL_HISTORY_FOR_RANGE weekdays)
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


def _catalog_context(rid, signal, db_path):
    """event_intel.engine.context_by_ref for a demand_signals row the
    catalog wrote, else None."""
    try:
        from event_intel import engine
        return engine.context_by_ref(rid, signal.get("ref"), db_path=db_path) if db_path \
            else engine.context_by_ref(rid, signal.get("ref"))
    except Exception:
        return None


def _game_staffing(rid, event, db_path):
    """event_intel.playbook.staffing for a catalog game, else None."""
    if not event or event.get("category") != "sports":
        return None
    try:
        from event_intel import playbook
        return playbook.staffing(rid, event, db_path=db_path) if db_path else playbook.staffing(rid, event)
    except Exception:
        return None


def _game_prep(rid, event, db_path):
    """{"text", "tone", "basis"} for the day after's prep line, or None."""
    if not event or event.get("category") != "sports":
        return None
    try:
        from event_intel import gameday
        kw = {"db_path": db_path} if db_path else {}
        mix = gameday.item_mix(rid, event, **kw)
        prep = gameday.prep_lines(rid, event, mix=mix, **kw) if mix else []
        if prep:
            return {"text": "Prep for " + "; ".join(p["text"] for p in prep[:3]), "tone": "warn",
                    "basis": mix["basis"]}
        if mix and mix.get("text"):
            return {"text": mix["text"] + " One game — not yet a pattern to prep on.", "tone": None,
                    "basis": mix["basis"]}
    except Exception:
        return None
    return None


def _keeps_events(rid, db_path):
    """Whether this restaurant keeps its events/reservations in Cavnar at
    all — an empty day then means "nothing listed", not "unknown"."""
    from dsr import store
    conn = store.get_conn(db_path)
    try:
        # The owner's own entries only: a game the event catalog copied in
        # (event_intel, source "events") is not the owner keeping a list
        # (audit 10/1/26).
        r = conn.execute("SELECT 1 FROM demand_signals WHERE restaurant_id=? AND source<>'events' LIMIT 1",
                         (rid,)).fetchone()
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
    """demand.forecast_net — the forecast on the report's own basis, with
    this restaurant's measured event effects applied: tomorrow's report
    grades the predictions built on it against its own net (memory audit
    9/29/26, net_basis)."""
    try:
        import demand
        return demand.forecast_net(rid, day, db_path=db_path) if db_path else demand.forecast_net(rid, day)
    except Exception:
        return None


def _measured(rid, events, db_path):
    """This restaurant's measured effects for what a prediction may name:
    {"rain": effect|None, "events": {label: effect}} (event_memory
    .measured_effect) — predictions state a rain or event effect only in the
    direction measured here (memory audit 9/29/26, event_memory)."""
    out = {"rain": None, "events": {}}
    try:
        import event_memory
        out["rain"] = event_memory.measured_effect(rid, event_memory.RAIN_LABEL, db_path=db_path)
        for e in events or []:
            if e.get("kind") == "event" and e.get("label"):
                for lab in event_memory.split_labels(e["label"]):
                    eff = event_memory.measured_effect(rid, lab, db_path=db_path)
                    if eff:
                        out["events"][str(e["label"])] = eff
                        break
    except Exception:
        pass
    return out


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
    """The night's net target: the owner's budget, else their nightly-sales
    goal (store.night_budget)."""
    try:
        from dsr import store
        b = store.night_budget(rid, day, db_path=db_path) if db_path else store.night_budget(rid, day)
        return b.get("net")
    except Exception:
        return None


# ── tomorrow's labor, this week's overtime, a 7th day in a row ─────────────
#
# Forward-looking (owner, 9/30/26): the lines an owner can still change.
# Every figure is the PUBLISHED schedule priced on Labor's own rate chain
# (schedule_economics.priced_cost with labor.person_rate_book), set against
# Cavnar AI's forecast; hours already worked come from the synced punches.

ROOM_MIN_HOURS = 4.0          # a teammate "has room" for at least a short shift
REST_RUN_DAYS = 6             # worked this many days straight → tomorrow is the 7th


def _worked_shifts(rid, db_path):
    """Every synced/uploaded shift row (labor.load_shifts), or []."""
    try:
        from models import get_client_data
        from labor import load_shifts
        data = (get_client_data(rid, db_path=db_path) if db_path else get_client_data(rid)) or {}
        raw = data.get("shifts_csv") or ""
        return load_shifts(csv_string=raw) if raw.strip() else []
    except Exception:
        return []


def _published(rid, day, db_path):
    try:
        import intraday
        rows = intraday.published_rows(rid, day, db_path=db_path) if db_path else intraday.published_rows(rid, day)
    except Exception:
        return []
    return [dict(r, date=day.isoformat()) for r in rows or []]


def _salaried(restaurant):
    try:
        from models import salaried_staff, salaried_name_key
        return {salaried_name_key(s["name"]) for s in salaried_staff(restaurant)}
    except Exception:
        return set()


def _rates(restaurant, rid, db_path):
    from labor import rate_book
    try:
        from models import get_role_rates, person_rates
        roles = (get_role_rates(rid, db_path=db_path) if db_path else get_role_rates(rid)) or {}
        people, typical = rate_book(_worked_shifts(rid, db_path), person_rates(restaurant))
    except Exception:
        roles, people, typical = {}, {}, {}
    blended = float(roles.get("_default") or getattr(restaurant, "hourly_rate", None) or 0) or 0.0
    return roles, blended, people, typical


def _week_bounds(restaurant, day):
    """(first, last) date of the payroll week holding `day`."""
    wsd = int(getattr(restaurant, "week_start_day", 0) or 0)
    first = day - timedelta(days=(day.weekday() - wsd) % 7)
    return first, first + timedelta(days=6)


def _hours_worked(shifts, start, end, salaried):
    """{name key: hours worked} between two dates, salaried left out."""
    from labor import _name_key, _shift_hours
    out = {}
    for s in shifts:
        d = str(s.get("date") or "")[:10]
        k = _name_key(s.get("employee"))
        if not k or k in salaried or not (start.isoformat() <= d <= end.isoformat()):
            continue
        out[k] = out.get(k, 0.0) + _shift_hours(s)
    return out


def labor_plan(restaurant, day, forecast_typical, db_path=None) -> dict | None:
    """Tomorrow's published schedule priced against the forecast: hours,
    hourly dollars (overtime priced with this payroll week's hours so far),
    and — for the owner, whose labor % is all-in — the salaried day share.
    None without a published schedule for tomorrow."""
    import schedule_economics as econ
    from labor import OVERTIME_THRESHOLD_HOURS
    rid = restaurant.id
    tmr = day + timedelta(days=1)
    rows = _published(rid, tmr, db_path)
    if not rows:
        return None
    sal = _salaried(restaurant)
    roles, blended, people, typical = _rates(restaurant, rid, db_path)
    first, _last = _week_bounds(restaurant, tmr)
    base = _hours_worked(_worked_shifts(rid, db_path), first, day, sal) if first <= day else {}
    priced = econ.priced_cost(rows, roles, blended, ceiling=OVERTIME_THRESHOLD_HOURS,
                              base_hours=base, salaried=sal, person_rates=people, role_typical=typical)
    hours = round(sum(float(r.get("scheduled_hours") or 0) for r in rows
                      if " ".join(str(r.get("employee") or "").lower().split()) not in sal), 1)
    cost = float(priced.get("total") or 0)
    if not hours or cost <= 0:
        return None
    net = float(forecast_typical) if isinstance(forecast_typical, (int, float)) and forecast_typical > 0 else None
    out = {"hours": hours, "hourly_cost": round(cost, 0), "overtime_hours": priced.get("overtime_hours"),
           "hourly_pct": round(cost / net * 100.0, 1) if net else None, "forecast_net": net,
           "people": len({r["employee"] for r in rows if r.get("employee")}),
           "basis": "tomorrow's published schedule at each person's pay, over Cavnar AI's forecast"}
    try:
        from models import salaried_day_share
        share = salaried_day_share(restaurant)
    except Exception:
        share = None
    if share and net:
        out["salaried_cost"] = round(share, 0)
        out["salaried_total_cost"] = round(cost + share, 0)
        out["salaried_total_pct"] = round((cost + share) / net * 100.0, 1)
    try:
        import thresholds
        tgt = thresholds.target_for(restaurant, "labor")
        out["target_pct"], out["target_source"] = tgt.get("pct"), tgt.get("source")
    except Exception:
        out["target_pct"] = None
    return out


def overtime_outlook(restaurant, day, db_path=None) -> dict | None:
    """Who crosses 40 hours this payroll week if the published schedule
    holds: hours worked through the report's night plus every published
    shift still ahead in the week. Each person over names the overtime
    hours, its extra cost (the half on top of their own rate) and teammates
    in the same role with room for a shift. None without a published shift
    ahead in the week."""
    from labor import OVERTIME_THRESHOLD_HOURS, OVERTIME_MULTIPLIER, _name_key
    rid = restaurant.id
    tmr = day + timedelta(days=1)
    first, last = _week_bounds(restaurant, tmr)
    sal = _salaried(restaurant)
    ahead = []
    d = tmr
    while d <= last:
        ahead += _published(rid, d, db_path)
        d += timedelta(days=1)
    if not ahead:
        return None
    worked = _hours_worked(_worked_shifts(rid, db_path), first, day, sal) if first <= day else {}
    _roles, blended, people, typical = _rates(restaurant, rid, db_path)
    proj, role_of, names = dict(worked), {}, {}
    for r in ahead:
        k = _name_key(r.get("employee"))
        if not k or k in sal:
            continue
        try:
            proj[k] = proj.get(k, 0.0) + float(r.get("scheduled_hours") or 0)
        except (TypeError, ValueError):
            continue
        role_of.setdefault(k, (r.get("role") or "").strip())
        names[k] = r.get("employee")
    limit = OVERTIME_THRESHOLD_HOURS
    over = []
    for k, h in sorted(proj.items(), key=lambda kv: -kv[1]):
        if h <= limit or k not in names:
            continue
        role = role_of.get(k) or ""
        rate = people.get(k) or typical.get(role.lower()) or blended or None
        ot = round(h - limit, 1)
        room = [names[o] for o, oh in sorted(proj.items(), key=lambda kv: kv[1])
                if o != k and o in names and (role_of.get(o) or "").lower() == role.lower()
                and limit - oh >= ROOM_MIN_HOURS][:2]
        over.append({"employee": names[k], "role": role, "projected_hours": round(h, 1), "overtime_hours": ot,
                     "extra_cost": round(ot * rate * (OVERTIME_MULTIPLIER - 1.0), 0) if rate else None,
                     "room": room})
    return {"week_start": first.isoformat(), "week_end": last.isoformat(), "people": over[:6],
            "over_count": len(over),
            "extra_cost": round(sum(p["extra_cost"] or 0 for p in over), 0) if over else 0,
            "basis": ("hours worked this payroll week plus every published shift still ahead in it; the extra "
                      "cost is the half on top of each person's own rate")}


def rest_day_items(restaurant, day, db_path=None) -> list:
    """Tomorrow's scheduled people who have worked REST_RUN_DAYS days in a
    row through the report's night — tomorrow would be their 7th straight
    day (Will, 9/30/26: the one break-and-rest rule measured)."""
    from labor import _name_key
    rid = restaurant.id
    tmr = day + timedelta(days=1)
    sal = _salaried(restaurant)
    sched = {_name_key(r.get("employee")): r.get("employee") for r in _published(rid, tmr, db_path)}
    sched = {k: v for k, v in sched.items() if k and k not in sal}
    if not sched:
        return []
    days = {}
    for s in _worked_shifts(rid, db_path):
        k = _name_key(s.get("employee"))
        if k in sched:
            days.setdefault(k, set()).add(str(s.get("date") or "")[:10])
    out = []
    for k, name in sched.items():
        run = 0
        while (day - timedelta(days=run)).isoformat() in days.get(k, ()):
            run += 1
        if run >= REST_RUN_DAYS:
            n = run + 1
            nth = f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"
            out.append({"kind": "rest_day", "tone": "warn", "text": f"{name} would work a {nth} day in a row"})
    return out


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
        track_line = (f"Cavnar AI's range held on {held} of the last {ranged} nights "
                      f"({t.get('window_days')}-day record)")
    else:
        track_line = (f"{ranged} night{'s' if ranged != 1 else ''} scored against Cavnar AI's range so far — "
                      f"a confidence % shows at {TRACK_MIN}")
    return {"pct": pct, "based_on": [history] + (["its measured record"] if pct is not None else []),
            "missing": [], "watch": watch, "track": track_line,
            "label": f"{pct}%" if pct is not None else "—",
            "meaning": "How often Cavnar AI's forecast range has held — not a promise"}


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
            # A catalog event (event_intel: a Bears game) reads as the game —
            # who, when, on what — with what games like it did here, measured.
            ctx = _catalog_context(rid, e, db_path)
            text = ctx["describe_short"] if ctx else str(e.get("label"))
            if ctx and ctx.get("effect"):
                text += f". {ctx['effect']['basis']}"
            items.append({"kind": "event", "tone": "warn", "text": text,
                          "event_id": (ctx or {}).get("event", {}).get("id")})
            # Who worked games like it, by role (event_intel.playbook,
            # phase 2): a plan where measured, else what the last one
            # staffed. The Labor view's (access.tomorrow_for).
            st = _game_staffing(rid, (ctx or {}).get("event"), db_path)
            if st and st.get("text"):
                items.append({"kind": "game_staffing", "tone": "warn" if st.get("recommend") else None,
                              "text": st["text"], "basis": st.get("basis"),
                              "event_id": (ctx or {}).get("event", {}).get("id")})
            # What games like it sold (event_intel.gameday, phase 3): a prep
            # plan past the floor, the last one as a fact below it.
            prep = _game_prep(rid, (ctx or {}).get("event"), db_path)
            if prep:
                items.append(dict(prep, kind="game_prep", event_id=(ctx or {}).get("event", {}).get("id")))

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
        basis = f"the median of the last {fc.get('samples')} {wd}s"
        if fc.get("effects"):
            basis += ", " + ", ".join(
                f"{e.get('display') or e.get('label')} {e['lift_pct']:+.0f}% (measured {e['n']} "
                f"time{'s' if e['n'] != 1 else ''} here)" for e in fc["effects"])
        forecast = {"typical": fc.get("typical_sales"), "low": fc.get("low"), "high": fc.get("high"),
                    "samples": fc.get("samples"), "weekday": wd,
                    "base": fc.get("base_sales"), "effect_pct": fc.get("effect_pct"),
                    "effects": fc.get("effects") or [],
                    "text": (f"{_money(fc['low'])}–{_money(fc['high'])}" if fc.get("low") is not None
                             and fc.get("high") is not None else _money(fc["typical_sales"])),
                    "basis": basis}
    conf = confidence(fc, _track(rid, tmr, db_path), wx=wx, scheduled=scheduled,
                      keeps_events=_keeps_events(rid, db_path))
    preds = predictions.build(fc, budget_net=_budget(rid, tmr, db_path), weather=wx, events=events, weekday=wd,
                              effects=_measured(rid, events, db_path))
    try:
        items += rest_day_items(restaurant, day, db_path)
    except Exception:
        pass
    try:
        labor = labor_plan(restaurant, day, (forecast or {}).get("typical"), db_path)
    except Exception:
        labor = None
    try:
        ot = overtime_outlook(restaurant, day, db_path)
    except Exception:
        ot = None
    return {"date": tmr.isoformat(), "weekday": wd, "items": items, "scheduled": scheduled,
            "forecast": forecast, "confidence": conf, "labor": labor, "overtime": ot,
            "predictions": [{"key": p["key"], "text": p["text"]} for p in preds], "_preds": preds}

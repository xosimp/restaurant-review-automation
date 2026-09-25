"""
dsr.block_intel — the world outside the restaurant on one night: Erik's
Weather and "Event or Sport" columns, competitor moves, and traffic where
it was measured.

Sources (no model call):
  weather      weather.forecast_for_day — the National Weather Service
               FORECAST for the date (the daytime period, else "Tonight").
               Cavnar has no source for observed weather, so this is always
               labelled a forecast, never presented as what happened.
               `summary` is the short line for Erik's column ("Chance Rain ·
               84°"); the numbers ride alongside.
  events       demand_signals — what the owner entered for the date (events,
               and reservations booked), plus a holiday on the date from
               marketing.get_upcoming_holidays. "Nothing listed" when there
               is none: it is the owner's list, so an empty one is an answer.
  competitors  competitor_snapshots: moves are only SEEN on a night a
               competitor check ran (weekly). On that night the moves are
               notify.competitor_changes — the same rule the Monday alert
               uses. Any other night the count is None, not 0.
  traffic      only where measured: the night's covers (covers.by_date, what
               the owner recorded) against the median of the same weekday
               over the 8 weeks before (demand_signals.typical_covers). No
               cover count, no traffic section — an event's expected lift is
               the owner's guess, not a measurement, and is not shown as one.

Statuses: ready whenever any part could be read (an empty events list and a
missing forecast are answers, said in detail); unavailable when nothing
could be read at all.
"""
from datetime import datetime, timedelta

import dsr
from dsr import common


def _rows(ctx, sql, args):
    from dsr.store import get_conn
    conn = get_conn(ctx.db_path)
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def _weather(ctx):
    import weather
    fc = weather.forecast_for_day(ctx.restaurant, ctx.day, db_path=ctx.db_path) or {}
    day, night = fc.get("day"), fc.get("night")
    use = day or night
    if not use:
        return None, {"basis": "forecast", "summary": None,
                      "note": "No forecast on file for this date — Cavnar AI has no observed weather to fall back on."}
    temp = use.get("high_f") if day else use.get("low_f")
    bits = [str(use.get("short_forecast") or "").strip()]
    if temp is not None:
        bits.append(f"{'high' if day else 'low'} {int(temp)}°")
    if use.get("precip_pct"):
        bits.append(f"{int(use['precip_pct'])}% rain")
    return use, {
        "basis": "forecast",
        "source": "National Weather Service forecast",
        "period": "day" if day else "night",
        "summary": " · ".join(b for b in bits if b),
        "short_forecast": use.get("short_forecast") or None,
        "high_f": day.get("high_f") if day else None,
        "low_f": night.get("low_f") if night else None,
        "precip_pct": use.get("precip_pct"),
        "note": "Forecast, not observed — Cavnar AI has no source for the weather that actually happened.",
    }


def _holiday(ctx):
    from marketing import get_upcoming_holidays
    # marketing.get_upcoming_holidays stamps each as "Name (Oct 31)", day
    # zero-padded — matched the way morning_brief matches today's.
    stamp = ctx.business_date.strftime("(%b %d)")
    raw = get_upcoming_holidays(datetime.combine(ctx.business_date, datetime.min.time())) or ""
    hits = [h.replace(stamp, "").strip() for h in raw.split(", ") if stamp in h]
    return hits[0].split(" — ")[0] if hits else None


def _events(ctx, gaps):
    import demand_signals
    rows = common.guard(ctx, "intel", "events", lambda: demand_signals.upcoming(
        ctx.restaurant_id, ctx.day, ctx.day, db_path=ctx.db_path), gaps)
    holiday = common.guard(ctx, "intel", "holiday", lambda: _holiday(ctx), gaps)
    if rows is None:
        # The owner's list couldn't be read: "Nothing listed" would be a
        # claim about a list nobody saw.
        return None
    items = [{"label": s["label"], "kind": "event", "covers": s.get("covers"), "source": s.get("source")}
             for s in rows if s.get("kind") == "event"]
    if holiday:
        items.append({"label": holiday, "kind": "holiday", "covers": None, "source": "calendar"})
    booked = [s["covers"] for s in rows if s.get("kind") == "reservations" and s.get("covers") is not None]
    return {
        "summary": "; ".join(i["label"] for i in items) if items else "Nothing listed",
        "items": items,
        "reservations_covers": max(booked) if booked else None,
        "basis": "events and reservations you entered for the date, and the holiday calendar",
    }


def _competitors(ctx):
    import notify
    from time_utils import mdy
    u0, u1 = common.utc_bounds(ctx.restaurant, ctx.business_date)
    seen = _rows(ctx, "SELECT COUNT(*) AS n, MAX(captured_at) AS last FROM competitor_snapshots WHERE restaurant_id=?",
                 (ctx.restaurant_id,))[0]
    if not seen["n"]:
        return None, {"tracked": False, "note": "Competitors aren't tracked for this location yet."}
    that_night = _rows(ctx, "SELECT COUNT(*) AS n FROM competitor_snapshots WHERE restaurant_id=? "
                            "AND datetime(captured_at) >= ? AND datetime(captured_at) < ?",
                       (ctx.restaurant_id, u0, u1))[0]["n"]
    if not that_night:
        last = common.parse_utc(seen["last"])
        return None, {"tracked": True, "checked": False,
                      "last_check": mdy(common.to_local(last, ctx.restaurant)) if last else None,
                      "note": "Competitors are checked weekly; no check ran this night."}
    moves = notify.competitor_changes(ctx.restaurant_id, db_path=ctx.db_path)
    return len(moves), {"tracked": True, "checked": True,
                        "moves": [{"name": m["name"], "kind": m["kind"], "line": m["line"],
                                   "evidence": m["evidence"]} for m in moves[:5]],
                        "basis": ("Google's public ratings on this night's competitor check; a move on a "
                                  "handful of reviews can be noise — the review counts say how much is behind it")}


def _traffic(ctx):
    import covers
    import demand_signals
    got = covers.by_date(ctx.restaurant_id, ctx.day, ctx.day, db_path=ctx.db_path).get(ctx.day)
    if got is None:
        return None
    weekday = ctx.business_date.strftime("%A")
    typical = demand_signals.typical_covers(ctx.restaurant_id, db_path=ctx.db_path,
                                            before=ctx.business_date).get(weekday)
    if not typical:
        return None
    return {"covers": int(got), "typical_covers": int(typical),
            "vs_typical_pct": round((got - typical) / typical * 100, 1),
            "basis": (f"covers you recorded, against the median {weekday} of the 8 weeks before — "
                      "a comparison, not a cause")}


def collect(ctx):
    gaps = []
    wx = common.guard(ctx, "intel", "weather", lambda: _weather(ctx), gaps)
    events = _events(ctx, gaps)
    comp = common.guard(ctx, "intel", "competitors", lambda: _competitors(ctx), gaps)
    traffic = common.guard(ctx, "intel", "traffic", lambda: _traffic(ctx), gaps)
    if wx is None and events is None and comp is None:
        return dsr.block(dsr.UNAVAILABLE, source="cavnar", block_name="intel",
                         detail={"unavailable_parts": gaps})

    period = wx[0] if wx else None
    wdetail = wx[1] if wx else {"basis": "forecast", "summary": None, "note": "The forecast couldn't be read."}
    metrics = {
        "weather_high_f": wdetail.get("high_f") if period else None,
        "weather_low_f": wdetail.get("low_f") if period else None,
        "weather_precip_pct": wdetail.get("precip_pct") if period else None,
        "events_listed": len(events["items"]) if events else None,
        "reservations_covers": events["reservations_covers"] if events else None,
        "competitor_moves": comp[0] if comp else None,
    }
    detail = {
        "weather": wdetail,
        "events": events or {"summary": None, "items": [], "note": "Events couldn't be read."},
        "competitors": comp[1] if comp else {"note": "Competitor data couldn't be read."},
        "unavailable_parts": gaps,
    }
    if traffic:
        # Only where measured; otherwise the section is omitted, not zeroed.
        metrics["covers"] = traffic["covers"]
        metrics["covers_vs_typical_pct"] = traffic["vs_typical_pct"]
        detail["traffic"] = traffic
    return dsr.block(dsr.READY, source="cavnar", metrics=metrics, detail=detail)

"""
preshift.py — what the floor should know before the doors open.

Every review insight this product produces ended at the owner's screen. The
people who can actually act on "Friday dinner service is slow" are the ones
working Friday dinner, and nothing ever reached them. This is the briefing a
good GM gives at lineup, assembled from data the product already holds:

  * how busy today should be, relative to a normal day
  * what guests have been complaining about on this weekday or daypart
  * what the kitchen is running low on
  * a holiday or event, and the weather

Two rules, because this is shown to staff rather than to the owner:

  NO MONEY. Sales, labour cost and margins are the owner's business. Busyness
  is expressed relative to a normal day, never in dollars.

  NO INDIVIDUALS. Complaint themes are shared, never attributed to anyone —
  a lineup briefing that names who caused last Friday's bad review is a
  disciplinary conversation in front of the team, not a briefing.
"""
from datetime import date

from models import DB_PATH


def _safe(fn, *a, **k):
    try:
        return fn(*a, **k)
    except Exception:
        return None


def build(restaurant_id, day=None, db_path=DB_PATH):
    """{"day", "weekday", "items": [{"kind", "text"}]} — staff-safe by design."""
    from models import get_restaurant
    r = get_restaurant(restaurant_id)
    if day is None:
        # The restaurant's own date — the server's is UTC, and a 7pm Chicago
        # lineup is already "tomorrow" there.
        from time_utils import restaurant_now
        day = restaurant_now(r, naive=True).date() if r else date.today()
    weekday = day.strftime("%A")
    items = []

    # ── busyness, relative only ──
    import labor
    fc = _safe(labor.build_demand_forecast, restaurant_id) or {}
    today = next((d for d in fc.get("days", []) if d["day"] == weekday), None)
    if today and today.get("samples", 0) >= 3:
        pct = today["vs_average_pct"]
        if pct >= 15:
            text = f"Expect a busy {weekday} — typically about {pct}% busier than an average day."
        elif pct <= -15:
            text = f"Expect a quieter {weekday} — typically about {abs(pct)}% under an average day."
        else:
            text = f"A typical {weekday} in volume."
        items.append({"kind": "volume", "text": text})

    # ── what guests have been saying about this day or shift ──
    if r and getattr(r, "module_reviews", 0):
        import review_intelligence as ri
        for c in (_safe(ri.complaint_clusters, restaurant_id) or [])[:5]:
            days = []
            if c.get("weekday_pair"):
                days = c["weekday_pair"]["days"]
            elif c.get("weekday"):
                days = [c["weekday"]["value"]]
            if weekday not in days:
                continue
            said = [x["complaint"] for x in (c.get("complaints") or []) if x.get("complaint")][:2]
            quote = f" — guests mentioned: {'; '.join(said)}" if said else ""
            items.append({"kind": "watch",
                          "text": f"Watch {c['category'].replace('_', ' ')} tonight: it has come up in "
                                  f"{c['mentions']} recent reviews, mostly on {' and '.join(days)}s{quote}."})
            break

    # ── running low ──
    if r and getattr(r, "module_inventory", 0):
        from inventory import load_inventory_for_restaurant, analysis_for
        loaded = _safe(load_inventory_for_restaurant, restaurant_id)
        if loaded and loaded[1]:
            a = (_safe(analysis_for, restaurant_id, items=loaded[0], is_live=True) or (None, None, {}))[2]
            low = [x["item"] for x in (a.get("critical_low") or [])][:5]
            if low:
                items.append({"kind": "stock",
                              "text": f"Running low: {', '.join(low)}. Check with the kitchen before "
                                      f"recommending dishes that use them."})

    # ── the day itself ──
    from marketing import get_upcoming_holidays
    from datetime import datetime
    upcoming = _safe(get_upcoming_holidays, datetime.combine(day, datetime.min.time())) or ""
    # marketing.get_upcoming_holidays formats each as "Christmas Day (Dec 25)",
    # day zero-padded ("(Jan 01)"). Matched on that exact stamp.
    stamp = day.strftime("(%b %d)")
    todays = [h.replace(stamp, "").strip() for h in upcoming.split(", ") if stamp in h]
    if todays:
        items.append({"kind": "event", "text": f"Today: {todays[0]}."})

    if r:
        import weather
        wx = _safe(weather.get_forecast_for_week, r, [day.isoformat()])
        if wx:
            w = wx[0]
            rain = f", {w['precip_pct']}% chance of rain" if w.get("precip_pct") else ""
            items.append({"kind": "weather",
                          "text": f"Weather: {w.get('short_forecast', '').lower()}, high of "
                                  f"{w.get('high_f')}°{rain}."})
    return {"day": day.isoformat(), "weekday": weekday, "items": items}

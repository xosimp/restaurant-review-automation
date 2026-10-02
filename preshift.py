"""
preshift.py — what the floor should know before the doors open.

Every review insight this product produces ended at the owner's screen. The
people who can actually act on "Friday dinner service is slow" are the ones
working Friday dinner, and nothing ever reached them. This is the briefing a
good GM gives at lineup, assembled from data the product already holds:

  * how busy today should be, relative to a normal day, and when the rush
    comes (the hourly shares, or a game's measured offset from its start)
  * what guests have been complaining about on this weekday or daypart
  * what the kitchen ran out of last night, and what it is running low on
  * a holiday or event, and the weather

Two rules, because this is shown to staff rather than to the owner:

  NO MONEY. Sales, labor cost and margins are the owner's business. Busyness
  is expressed relative to a normal day, never in dollars — and the rush is
  said from hours and offsets only: the event playbook's own rush text
  carries dollars and is never read here.

  NO INDIVIDUALS. Complaint themes are shared, never attributed to anyone —
  a lineup briefing that names who caused last Friday's bad review is a
  disciplinary conversation in front of the team, not a briefing. The
  close-out's free text (what went wrong, callouts) is never read: only the
  ingredients an 86 matched.

The day's items are the same for everyone, so they are built once per
restaurant and business day and kept BUILD_TTL_SECONDS (build_cached) —
twenty-five people opening the app before dinner used to run the forecast,
the review clustering, the food-cost analysis and sometimes a live weather
fetch twenty-five times (employee audit PERF-07). Process-local, which is
right at --workers 1 (CLAUDE.md); invalidate() drops a restaurant's copy.
"""
import threading
import time
from datetime import date, timedelta

from models import DB_PATH

BUILD_TTL_SECONDS = 15 * 60
_MEMO_MAX = 500
_memo = {}
_memo_lock = threading.Lock()

# The rush window: the hour with the largest share of a usual day's sales,
# widened by a neighbouring hour that carries at least this much of the
# peak's share — "busiest around 6–8pm", never a figure.
RUSH_NEIGHBOUR_SHARE = 0.85


def _safe(fn, *a, **k):
    try:
        return fn(*a, **k)
    except Exception:
        return None


def _game_line(restaurant_id, label, ref, db_path=DB_PATH):
    """The lineup's line for a followed game (a demand_signals row with
    source "events"), or None when it is a quiet game or unreadable."""
    try:
        import event_memory
        from event_intel import engine, store
        mem_db = None if db_path == DB_PATH else db_path
        if event_memory.quiet_catalog(restaurant_id, label, ref, db_path=mem_db):
            return None
        e = store.event_by_id(int(str(ref).split(":", 1)[1]), db_path=db_path)
    except Exception:
        return None
    if not e:
        return f"Game tonight: {label}."
    what = engine.describe(e, with_date=False, tz=engine.restaurant_clock(restaurant_id, db_path=db_path))
    return f"Game tonight: {what}." if e.get("category") == "sports" else f"Nearby tonight: {what}."


def _clock(h):
    """"6pm" for hour 18 (24 and over is after midnight on the business day)."""
    h = int(h) % 24
    return f"{(h % 12) or 12}{'am' if h < 12 else 'pm'}"


def hours_span(h1, h2):
    """"6–8pm" for the hours h1 through h2 inclusive (the window ends at the
    top of the hour after h2)."""
    a, b = _clock(h1), _clock(int(h2) + 1)
    return f"{a[:-2] if a[-2:] == b[-2:] else a}–{b}"


def _game_rush_line(restaurant_id, ref, db_path=DB_PATH):
    """"Expect the jump around 6–7pm — about an hour before kickoff." for a
    followed game whose past games of its class agree on when the jump came
    (event_intel.playbook.rush: SEGMENT_MIN_N clean games within an hour of
    each other, each a real jump). Built from pattern_offset / pattern_span
    and tonight's start hour ONLY — the rush's `text` carries dollars and is
    never read. None when the games don't agree: the playbook's own floor,
    so nothing is said (employee audit AI-02)."""
    try:
        from event_intel import engine, playbook, store
        e = store.event_by_id(int(str(ref).split(":", 1)[1]), db_path=db_path)
        if not e or e.get("category") != "sports":
            return None
        rz = playbook.rush(restaurant_id, e, db_path=db_path)
        if not rz or rz.get("pattern_offset") is None:
            return None
        lk = engine.local_kickoff(e, engine.restaurant_clock(restaurant_id, db_path=db_path))
        if not lk or not lk[1]:
            return None
        kick = int(str(lk[1])[:2])
        off, span = int(rz["pattern_offset"]), int(rz.get("pattern_span") or 0)
        word = engine.start_word(e)
    except Exception:
        return None
    n = abs(off)
    if off == 0:
        rel = f"the {word} hour"
    else:
        rel = ("about an hour" if n == 1 else f"about {('two', 'three', 'four')[n - 2] if n <= 4 else n} hours") + \
              (" before " if off < 0 else " after ") + word
    return f"Expect the jump around {hours_span(kick + off, kick + off + span)} — {rel}."


def _hourly_rush_line(restaurant_id, weekday):
    """"Busiest around 6–8pm on a usual Friday." from the hourly share
    profile (schedule_engine._hourly_profile: at least three measured
    same-weekday days), or None. Hours only — a share is never said."""
    try:
        import schedule_engine
        curve = (schedule_engine._safe_hourly_profile(restaurant_id) or {}).get(weekday) or {}
        curve = {int(h): float(v or 0) for h, v in curve.items()}
    except Exception:
        return None
    if not curve:
        return None
    peak = max(curve, key=lambda h: (curve[h], -h))
    if curve[peak] <= 0:
        return None
    lo = hi = peak
    left, right = curve.get(peak - 1, 0.0), curve.get(peak + 1, 0.0)
    if max(left, right) >= RUSH_NEIGHBOUR_SHARE * curve[peak]:
        if right >= left:
            hi = peak + 1
        else:
            lo = peak - 1
    return f"Busiest around {hours_span(lo, hi)} on a usual {weekday}."


def _eighty_sixed_line(restaurant_id, day, db_path=DB_PATH):
    """("86'd last night: Salmon. …", [names]) from last night's close-out —
    only the ingredients its 86 field MATCHED (closeout._match_ingredient,
    the same match that zeroed them), named as the ingredient list names
    them. The manager's free text is never repeated: it can name people
    and blame (employee audit AI-05). (None, []) otherwise."""
    try:
        import closeout
        from models import get_conn
        prev = (day - timedelta(days=1)).isoformat()
        conn = get_conn(db_path) if db_path != DB_PATH else get_conn()
        try:
            row = conn.execute("SELECT eighty_sixed FROM close_outs WHERE restaurant_id=? AND business_date=?",
                               (restaurant_id, prev)).fetchone()
            if not row or not (row["eighty_sixed"] or "").strip():
                return None, []
            ings = [dict(r) for r in conn.execute(
                "SELECT id, name FROM ingredients WHERE restaurant_id=? AND is_active=1", (restaurant_id,)).fetchall()]
        finally:
            conn.close()
        names = []
        for phrase in closeout._phrases(row["eighty_sixed"]):
            hit = closeout._match_ingredient(phrase, ings)
            if hit and hit["name"] not in names:
                names.append(hit["name"])
    except Exception:
        return None, []
    if not names:
        return None, []
    return (f"86'd last night: {', '.join(names[:5])}. Check with the kitchen before recommending dishes "
            f"that use {'it' if len(names) == 1 else 'them'}."), names


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
    # The same demand levels the Shift Quality scorer staffs the day by
    # (thresholds.demand_level: +25 peak / +8 high / -15 low) on the same
    # reading floor — this line used its own ±15%, so a day the schedule
    # was built as "high" was "a typical Friday" at lineup.
    import thresholds
    if today and today.get("samples", 0) >= thresholds.DEMAND_LEVEL_MIN_READINGS:
        pct = today["vs_average_pct"]
        level = thresholds.demand_level(pct)
        if level == "peak":
            text = f"Expect a busy {weekday} — typically about {pct}% busier than an average day."
        elif level == "high":
            text = f"Expect a busier-than-average {weekday} — typically about {pct}% above an average day."
        elif level == "low":
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
            # The theme only — never the guests' own words. A complaint as
            # written ("our server Jake ignored us") can name someone on the
            # team, and read out at lineup that is a reprimand in public.
            items.append({"kind": "watch",
                          "text": f"Watch {c['category'].replace('_', ' ')} tonight: it has come up in "
                                  f"{c['mentions']} recent reviews, mostly on {' and '.join(days)}s."})
            break

    # ── what ran out last night (AI-05), then what is running low ──
    out86 = []
    if r and getattr(r, "module_inventory", 0):
        line86, out86 = _eighty_sixed_line(restaurant_id, day, db_path)
        if line86:
            items.append({"kind": "eighty_sixed", "text": line86})
        from inventory import load_inventory_for_restaurant, analysis_for
        loaded = _safe(load_inventory_for_restaurant, restaurant_id)
        if loaded and loaded[1]:
            a = (_safe(analysis_for, restaurant_id, items=loaded[0], is_live=True) or (None, None, {}))[2]
            said = {n.lower() for n in out86}
            low = [x["item"] for x in (a.get("critical_low") or []) if str(x["item"]).lower() not in said][:5]
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

    # ── what the owner is doing about tonight (memory audit 9/29/26,
    # mkt_to_staffing): an event or a party on file, a text sent to fill the
    # night, a post about a dish — the floor learned about them from the
    # guests. Staff-safe: the label only; no money, no guest counts from the
    # text list, no individuals.
    import demand_signals
    game_rush = None
    for sig in (_safe(demand_signals.upcoming, restaurant_id, day.isoformat(), day.isoformat()) or []):
        label = str(sig.get("label") or "").strip()
        if not label or sig.get("kind") == "reservations":
            continue
        if sig.get("source") == "campaign":
            text = "Guests were texted an invitation for tonight — expect some to mention it."
        elif sig.get("source") == "post":
            text = f"Promoted today: {label.split(': ', 1)[-1]}. Guests may ask for it."
        elif sig.get("source") == "events":
            # A followed game from the catalog — not something the owner
            # booked. A quiet one (a frequent series not measured to matter
            # here, event_memory's one quiet test) earns no line at lineup;
            # the rest are said as games (event re-audit P1-08, P4-02).
            text = _game_line(restaurant_id, label, sig.get("ref"), db_path)
            if not text:
                continue
            if game_rush is None:
                game_rush = _game_rush_line(restaurant_id, sig.get("ref"), db_path)
        else:
            text = f"On the books tonight: {label}" + (f" ({sig['covers']} covers)" if sig.get("covers") else "") + "."
        items.append({"kind": "promotion" if sig.get("source") in ("campaign", "post") else "event", "text": text})

    # ── when the rush comes (AI-02): a game's measured jump first, else the
    # usual weekday's busiest hours. Hours and offsets only. Placed right
    # after the volume line, where a lineup says it.
    rush = game_rush or _hourly_rush_line(restaurant_id, weekday)
    if rush:
        at = next((i + 1 for i, it in enumerate(items) if it["kind"] == "volume"), 0)
        items.insert(at, {"kind": "rush", "text": rush})

    if r:
        import weather
        wx = _safe(weather.get_forecast_for_week, r, [day.isoformat()])
        # A stale copy (past weather.FORECAST_STALE_HOURS, or of unknown
        # age) is never told as today's weather (re-audit B3#6).
        if wx and not wx[0].get("stale"):
            w = wx[0]
            rain = f", {w['precip_pct']}% chance of rain" if w.get("precip_pct") else ""
            items.append({"kind": "weather",
                          "text": f"Weather: {w.get('short_forecast', '').lower()}, high of "
                                  f"{w.get('high_f')}°{rain}."})
    return {"day": day.isoformat(), "weekday": weekday, "items": items}


def business_day(restaurant_id):
    """The restaurant's own calendar date (never the server's UTC one)."""
    try:
        from time_utils import restaurant_now_by_id
        return restaurant_now_by_id(restaurant_id, naive=True).date()
    except Exception:
        return date.today()


def build_cached(restaurant_id, day=None, db_path=DB_PATH, now=None):
    """build(), kept BUILD_TTL_SECONDS per (restaurant, business day). The
    items are the same for every employee, so they are built once — not per
    employee per request (PERF-07). A failed build raises and is not kept.
    `now` (monotonic seconds) is for tests."""
    day = day or business_day(restaurant_id)
    key = (db_path, int(restaurant_id), day.isoformat())
    t = time.monotonic() if now is None else now
    with _memo_lock:
        hit = _memo.get(key)
        if hit and hit[0] > t:
            return hit[1]
    value = build(restaurant_id, day=day, db_path=db_path)
    with _memo_lock:
        if len(_memo) >= _MEMO_MAX:
            stale = [k for k, (exp, _v) in _memo.items() if exp <= t] or list(_memo)[:_MEMO_MAX // 4]
            for k in stale:
                _memo.pop(k, None)
        _memo[key] = (t + BUILD_TTL_SECONDS, value)
    return value


def invalidate(restaurant_id=None):
    """Drop the kept builds for one restaurant (or every one)."""
    with _memo_lock:
        for k in list(_memo):
            if restaurant_id is None or k[1] == int(restaurant_id):
                _memo.pop(k, None)
